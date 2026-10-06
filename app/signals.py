"""Signal scoring v0 — transparent heuristic composite (NO ML yet).

Turns the raw item store into a ranked symbol list for GET /api/signals.

    score(symbol) = Σ_items [ w_source × exp(-λ·age_h) × importance_boost ]
                    × smart_money_mult
    + burst reason when ≥3 fresh items hit the same symbol in 2 h.

Design notes (docs/METRIK-FILTER.md):
- Velocity & source authority beat sentiment polarity (arXiv:2401.00603,
  2209.02911) — so we score freshness × source weight, never text sentiment.
- DEXScreener boosts are paid ads → weight 0.15 (signal DILUTION, not alpha).
- On-chain whale positions (signal.onchain) are the highest-tier input and
  also multiply everything else that mentions the same coin.
"""
from __future__ import annotations

import logging
import json
import math
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .db import storage

log = logging.getLogger(__name__)

# --- weights per storage `source` value (tune over time) ---
SOURCE_WEIGHTS: Dict[str, float] = {
    # Tier 1 — direct market/on-chain evidence
    "hyperliquid": 1.00,
    "treenews": 0.90,      # wire, ~250 ms, has its own importance score
    "iqplus": 0.80,        # IDX wire
    # Tier 2 — social with identity
    "reddit": 0.55,
    # Tier 3 — editorial media
    "coindesk": 0.50, "decrypt": 0.50, "cointelegraph": 0.50, "theblock": 0.55,
    "cryptobriefing": 0.45, "utoday": 0.40, "thedefiant": 0.45, "newsbtc": 0.40,
    "ethnews": 0.35, "coinjournal": 0.40, "cryptopotato": 0.40,
    "idxchannel": 0.55, "tempo-bisnis": 0.45, "investing.com": 0.55,
    # Tier 4 — aggregators
    "google-news-crypto-trending": 0.40, "google-news-airdrop": 0.40,
    "google-news-ihsg": 0.50, "google-news-idx-shares": 0.50,
    "coingecko": 0.40, "coinmarketcap": 0.40, "cryptorank": 0.35,
    "feargreed": 0.00,
    # Tier 5 — weak / paid / anonymous
    "dexscreener": 0.15,   # boosts are ADVERTISING slots
    "nitter": 0.20, "threads": 0.15,
    # New-pool watcher: data mentah pool baru — sinyal awareness, bukan endorsement
    "geckoterminal": 0.50,
}
DEFAULT_WEIGHT = 0.35

HALF_LIFE_H = 3.0            # news decays fast; whale rows are re-scored live
LAMBDA = math.log(2) / HALF_LIFE_H
IMPORTANCE_MAX = 10.0        # Tree News importance field is ~0..10
SMART_MONEY_MULT = 2.5
BURST_WINDOW_H = 2.0
BURST_MIN_ITEMS = 3

CASHTAG_RE = re.compile(r"\$([A-Z]{1,10})\b")
IDX_SLUG_RE = re.compile(r"/news/stock_n/([a-zA-Z0-9]{4})-")

SIGNAL_CATEGORIES = (
    "signal.news",
    "signal.onchain",
    "signal.newtoken",
    "sentiment.reddit",
    "sentiment.x",
    "sentiment.coin.news",
    "idx.news",
)


def _w(source: str) -> float:
    if source in SOURCE_WEIGHTS:
        return SOURCE_WEIGHTS[source]
    if source.startswith("google-news"):
        return 0.40
    return DEFAULT_WEIGHT


def extract_symbols(item: Dict[str, Any]) -> List[Tuple[str, str, float]]:
    """Return [(symbol, venue, confidence), ...] mentioned by an item."""
    extra = item.get("extra") or {}
    out: List[Tuple[str, str, float]] = []
    seen: set = set()

    def add(sym: str, venue: str, conf: float) -> None:
        sym = (sym or "").strip().upper()
        if len(sym) < 2 or len(sym) > 10 or sym in seen:
            return
        seen.add(sym)
        out.append((sym, venue, conf))

    source = item.get("source", "")
    title = item.get("title") or ""

    if source == "hyperliquid" and extra.get("coin"):
        add(str(extra["coin"]), "crypto", 1.0)
    for c in extra.get("coins") or []:
        add(str(c), "crypto", 0.95)
    for m in CASHTAG_RE.findall(title):
        add(m, "crypto", 0.70)
    # IQPlus slug carries the IDX ticker (ekom-... → EKOM)
    slug = IDX_SLUG_RE.search(item.get("url") or "")
    if slug:
        add(slug.group(1), "idx", 0.80)
    return out


def _item_ts(item: Dict[str, Any]) -> int:
    extra = item.get("extra") or {}
    ts = extra.get("published_ms") or item.get("fetched_at")
    try:
        return int(ts)
    except (TypeError, ValueError):
        return int(time.time() * 1000)


async def compute_signals(limit_per_category: int = 400) -> Dict[str, Any]:
    """Read recent items from SQLite, score per symbol, rank, return payload."""
    now_ms = int(time.time() * 1000)
    items: List[Dict[str, Any]] = []
    for cat in SIGNAL_CATEGORIES:
        try:
            rows = await storage.list_items(category=cat, limit=limit_per_category)
        except Exception as e:  # noqa: BLE001
            log.warning("signals: read %s failed: %s", cat, e)
            rows = []
        items.extend(rows)

    has_smart_money: Dict[str, bool] = defaultdict(bool)
    hedge_any: Dict[str, bool] = defaultdict(bool)
    whale_clean: Dict[str, bool] = defaultdict(bool)
    build_reasons: Dict[Tuple[str, str], str] = {}
    newtoken_reasons: Dict[Tuple[str, str], str] = {}
    per_symbol: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for it in items:
        age_h = max(0.0, (now_ms - _item_ts(it)) / 3_600_000)
        if age_h > 72:  # older than 3 days: out of the realtime window
            continue
        decay = math.exp(-LAMBDA * age_h)
        w = _w(it.get("source", ""))
        extra = it.get("extra") or {}
        imp = extra.get("importance")
        try:
            imp_boost = 1.0 + 0.5 * min(max(float(imp), 0.0), IMPORTANCE_MAX) / IMPORTANCE_MAX
        except (TypeError, ValueError):
            imp_boost = 1.0

        # === Smart-money enrich: hedge damping + build-pattern detection ===
        is_whale = it.get("source") == "hyperliquid"
        whale_hedge = is_whale and bool(extra.get("hedge"))
        build_reason: Optional[str] = None
        if it.get("source") == "geckoterminal" and extra.get("symbol"):
            try:
                _age = float(extra.get("age_h") or 0)
                newtoken_reasons[(str(extra["symbol"]).upper(), "crypto")] = (
                    f"token baru: usia {_age:.0f} jam di {extra.get('network', '?')}"
                )
            except (TypeError, ValueError):
                pass
        if is_whale and not whale_hedge:
            addr = str(extra.get("address") or "")
            coin = str(extra.get("coin") or "")
            if addr and coin:
                try:
                    hist = await storage.list_position_history(
                        addr, coin, now_ms - 5 * 86_400_000
                    )
                    if len(hist) >= 3:
                        sizes = [abs(float(h["szi"])) for h in hist]
                        day_keys = {h["ts"] // 86_400_000 for h in hist}
                        increases = sum(
                            1 for a, b in zip(sizes, sizes[1:]) if b > a * 1.0001
                        )
                        if increases >= 2 and len(day_keys) >= 2:
                            build_reason = (
                                f"build: {increases} penambahan dalam {len(day_keys)} hari"
                            )
                except Exception as e:  # noqa: BLE001
                    log.debug("build detect %s %s failed: %s", addr[:10], coin, e)

        for sym, venue, conf in extract_symbols(it):
            key = (sym, venue)
            if whale_hedge:
                # Short perp + spot sama = delta-neutral carry, bukan sinyal arah
                # (docs/RISET-HYPURRSCAN-WHALE-TRACKING.md §3.3) — redam keras.
                conf *= 0.15
                hedge_any[key] = True
            elif is_whale:
                whale_clean[key] = True
            entry = per_symbol.setdefault(key, {
                "symbol": sym, "venue": venue, "score": 0.0,
                "item_count": 0, "fresh_2h": 0, "sources": set(),
                "top_titles": [], "best_conf": 0.0,
            })
            contrib = w * decay * imp_boost * conf
            entry["score"] += contrib
            entry["item_count"] += 1
            entry["best_conf"] = max(entry["best_conf"], conf)
            if age_h <= BURST_WINDOW_H:
                entry["fresh_2h"] += 1
            if is_whale:
                has_smart_money[key] = True
            if build_reason and (sym, venue) not in build_reasons:
                build_reasons[(sym, venue)] = build_reason
            src = it.get("source", "?")
            if src not in entry["sources"]:
                entry["sources"].add(src)
            if len(entry["top_titles"]) < 4 and it.get("title"):
                entry["top_titles"].append({
                    "title": it["title"][:200], "source": src, "url": it.get("url"),
                })

    signals: List[Dict[str, Any]] = []
    for (sym, venue), e in per_symbol.items():
        score = e["score"]
        reasons: List[str] = []
        smart = has_smart_money[(sym, venue)]
        hedged_only = hedge_any[(sym, venue)] and not whale_clean[(sym, venue)]
        if smart and not hedged_only:
            score *= SMART_MONEY_MULT
            reasons.append(f"smart-money x{SMART_MONEY_MULT}")
        if (sym, venue) in build_reasons:
            reasons.append(build_reasons[(sym, venue)])
        if (sym, venue) in newtoken_reasons:
            reasons.append(newtoken_reasons[(sym, venue)])
        if hedged_only:
            reasons.append("hedge: whale delta-neutral (spot mengisi perp short)")
        if e["fresh_2h"] >= BURST_MIN_ITEMS:
            reasons.append(f"burst: {e['fresh_2h']} item dalam {BURST_WINDOW_H:.0f} jam")
        if e["item_count"] >= 2:
            reasons.append(f"konsensus {len(e['sources'])} sumber")
        if not reasons:
            continue  # one weak mention alone is noise — filter it out
        signals.append({
            "symbol": sym,
            "venue": venue,
            "score": round(score, 4),
            "item_count": e["item_count"],
            "fresh_2h": e["fresh_2h"],
            "source_count": len(e["sources"]),
            "sources": sorted(e["sources"]),
            "reason_codes": reasons,
            "titles": e["top_titles"],
        })

    signals.sort(key=lambda s: s["score"], reverse=True)

    # Log untuk pengukuran akurasi (hit-rate 4h/24h) — 1 baris per simbol per 2 jam.
    bucket = now_ms // 7_200_000
    try:
        await storage.log_signal([
            {
                "bucket": bucket, "symbol": s["symbol"], "venue": s["venue"],
                "ts": now_ms, "score": s["score"],
                "reasons": json.dumps(s["reason_codes"], ensure_ascii=False),
            }
            for s in signals[:30]
        ])
    except Exception as e:  # noqa: BLE001
        log.debug("signal_log write failed: %s", e)

    return {
        "generated_at": now_ms,
        "window": "72h, half-life 3h",
        "method": "heuristic v0 (w_source x exp decay x importance) [x smart-money]",
        "count": len(signals),
        "signals": signals[:100],
    }
