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

# === Katalis IDX (docs/RISET-KATALIS-IDX.md; bobot dari riset serper 2026-10-06) ===
# Bukti kunci: banyak aksi korporasi TIDAK menghasilkan abnormal return signifikan
# (Riski 2025, BEI 2020-2023) → kata kunci saja tak cukup; butuh komposit + tense
# + negative list. MTO 2.0 > indeks 1.9 > CSPA 1.8 > PSN 1.7 > PMTHMETD 1.6 > ...
IDX_CATALYSTS = [
    (re.compile(r"\bMTO\b|tender offer|mandatory tender offer|tawaran pembelian", re.I), 2.0, "MTO/tender offer"),
    (re.compile(r"masuk (?:dalam )?indeks|masuk LQ45|masuk IDX30|masuk IDX80|rebalancing|evaluasi berkala", re.I), 1.9, "masuk indeks"),
    (re.compile(r"\bCSPA\b|Conditional Share", re.I), 1.8, "CSPA"),
    (re.compile(r"Proyek Strategis Nasional|\bPSN\b", re.I), 1.7, "PSN"),
    (re.compile(r"PMTHMETD|penambahan modal tanpa|private placement|penempatan saham", re.I), 1.6, "private placement"),
    (re.compile(r"akuisisi|mengakuisisi|pengambilalihan|takeover", re.I), 1.5, "akuisisi"),
    (re.compile(r"buyback|buy back|pembelian kembali saham", re.I), 1.4, "buyback"),
    (re.compile(r"RUPSLB|RUPS Luar Biasa|RUPS LB", re.I), 1.3, "RUPSLB"),
    (re.compile(r"dividen (?:spesial|khusus|interim|luar biasa)", re.I), 1.2, "dividen spesial"),
    (re.compile(r"perubahan (?:pengendalian|susunan (?:direksi|dewan komisaris))|direksi baru", re.I), 1.1, "perubahan pengelola"),
    (re.compile(r"right issue|rights issue|\bHMETD\b|penambahan modal dengan HMETD", re.I), 1.0, "right issue"),
    (re.compile(r"stock split|pemecahan saham", re.I), 0.9, "stock split"),
]
IDX_NEGATIVE_RE = re.compile(
    r"saham gorengan|gorengan|suspensi|watch list|pemantauan khusus|transaksi tidak wajar"
    r"|ekuitas negatif|delisting|restatement|penyajian kembali|dibatalkan|\bbatal\b|ditunda|penundaan",
    re.I,
)
IDX_EARLY_RE = re.compile(
    r"\bakan\b|rencana|berencana|wacana|kajian|mengkaji|potensi|dalam pembicaraan|diprediksi", re.I
)
IDX_EXECUTED_RE = re.compile(
    r"telah|sudah|mulai|realisasi|ditandatangani|disetujui|persetujuan|efektif|selesai", re.I
)

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
    catalyst_reasons: Dict[Tuple[str, str], str] = {}
    news_reasons: Dict[Tuple[str, str], str] = {}
    pool_flags: Dict[Tuple[str, str], List[str]] = {}
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

        # Katalis IDX: keyword berbobot pada judul (hanya item idx.news; simbol
        # dari slug IQPlus). Negative list → redam; "telah/mulai" ×1,3; "akan/
        # rencana/wacana" ×0,5; ≥2 katalis bersamaan ×1,3 (komposit > tunggal).
        idx_mult = 1.0
        idx_reason: Optional[str] = None
        title_l = it.get("title") or ""
        if it.get("category") == "idx.news" and title_l:
            if IDX_NEGATIVE_RE.search(title_l):
                idx_mult = 0.05
                idx_reason = "katalis IDX NEGATIF (suspensi/gorengan/batal/delisting) — diredam"
            else:
                matched = [(w, lab) for pat, w, lab in IDX_CATALYSTS if pat.search(title_l)]
                if matched:
                    mult = min(w for w, _ in matched)
                    if len(matched) >= 2:
                        mult *= 1.3
                    if IDX_EARLY_RE.search(title_l):
                        mult *= 0.5
                    if IDX_EXECUTED_RE.search(title_l):
                        mult *= 1.3
                    idx_mult = min(mult, 3.0)
                    labels = " + ".join(lab for _, lab in matched[:3])
                    idx_reason = f"katalis IDX: {labels} (×{idx_mult:.1f})"

        # Resep crypto dari riset (docs/RISET-CRYPTO-BOOM-SIGNALS.md):
        # soft story (listing/launch) → gerakan di/jelang rilis, diskon pasca-rilis;
        # katalis fundamental (partnership/mainnet/...) → drift pasca-event.
        news_mult = 1.0
        news_reason: Optional[str] = None
        if it.get("source") == "treenews":
            kind = str(extra.get("kind") or "").lower()
            if kind in ("listing", "launch"):
                news_mult = 0.6
                news_reason = "soft story (listing/launch): gerakan di/jelang rilis — beri diskon pasca-rilis"
            elif kind in ("partnership", "mainnet", "protocol", "hardfork", "etf", "adoption", "upgrade"):
                news_mult = 1.3
                news_reason = "katalis fundamental: drift pasca-event"

        # Metrik kualitas pool baru (arXiv:2602.14860): $/transaksi tinggi =
        # akumulasi cepat; rasio beli; liq/FDV = filter low-float-high-FDV.
        pool_mult = 1.0
        pool_flags_item: List[str] = []
        if it.get("source") == "geckoterminal":
            try:
                _vol = float(extra.get("volume_24h_usd") or 0)
                _tx = extra.get("tx_24h") or {}
                _nb, _ns = float(_tx.get("buys") or 0), float(_tx.get("sells") or 0)
                _liq = float(extra.get("liquidity_usd") or 0)
                _fdv = float(extra.get("fdv_usd") or 0)
                if _vol > 0 and _nb + _ns >= 10:
                    _per_trade = _vol / (_nb + _ns)
                    if _per_trade > 500:
                        pool_flags_item.append(f"akumulasi cepat (${_per_trade:.0f}/transaksi)")
                    _ratio = _nb / (_nb + _ns)
                    if _ratio > 0.6:
                        pool_flags_item.append(f"beli-dominan {_ratio:.2f}")
                if _fdv > 0 and _liq > 0:
                    _lf = _liq / _fdv
                    if _lf >= 0.15:
                        pool_flags_item.append(f"float sehat (liq/FDV {_lf:.2f})")
                    elif _lf < 0.10:
                        pool_mult = 0.5
                        pool_flags_item.append(f"low-float/high-FDV (liq/FDV {_lf:.2f}) — waspada")
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
            if venue == "idx" and idx_reason is not None:
                conf *= idx_mult
                old = catalyst_reasons.get(key)
                if old is None or not old.startswith("katalis IDX NEGATIF"):
                    catalyst_reasons[key] = idx_reason
            if news_mult != 1.0 and news_reason is not None:
                conf *= news_mult
                news_reasons.setdefault(key, news_reason)
            if pool_mult != 1.0:
                conf *= pool_mult
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
            if pool_flags_item:
                lst = pool_flags.setdefault(key, [])
                for p in pool_flags_item:
                    if p not in lst:
                        lst.append(p)
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
        burst = e["fresh_2h"] >= BURST_MIN_ITEMS
        if smart and not hedged_only:
            score *= SMART_MONEY_MULT
            reasons.append(f"smart-money x{SMART_MONEY_MULT}")
        if smart and not hedged_only and (sym, venue) in build_reasons and burst:
            # Tier S (arXiv:2602.14860 + Lookonchain): konfluen tertinggi yang
            # datanya kami miliki — whale build + velocity bersamaan.
            score *= 1.5
            reasons.append("TIER S: whale build + burst bersamaan (×1,5)")
        if (sym, venue) in build_reasons:
            reasons.append(build_reasons[(sym, venue)])
        if (sym, venue) in newtoken_reasons:
            reasons.append(newtoken_reasons[(sym, venue)])
        if (sym, venue) in news_reasons:
            reasons.append(news_reasons[(sym, venue)])
        for p in pool_flags.get((sym, venue), []):
            reasons.append(p)
        if (sym, venue) in catalyst_reasons:
            reasons.append(catalyst_reasons[(sym, venue)])
        if hedged_only:
            reasons.append("hedge: whale delta-neutral (spot mengisi perp short)")
        if burst:
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
