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
from .baselines import fetch_baselines

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

# === Gerbang anti-salah-beli (docs/RISET-SIGNAL-QUALITY-10-10.md bab 6) ===
# SHADOW MODE dulu (bab 6: 3+ bucket data sebelum aktif): gate hanya MENCATAT
# gate_flags — skor & ranking TIDAK diubah. Setelah review data, balik
# SHADOW_GATES=False utk benar-benar memblokir/menurunkan.
SHADOW_GATES = True
GATE_MIN_LIQ_USD = 50_000.0  # G1 (#5): pool likuiditas < $50K tak layak beli
GATE_HONEYPOT_SELLS = 0.0    # G3 (#2; Chainalysis 2025: 94% PnD kena rug)
                             # pool TANPA satu pun sell 24 jam = honeypot klasik
GATE_LIQ_FDV = 0.07          # G6 (#4): low-float/high-FDV trap
GATE_DD30_PCT = 40.0         # #1: wallet DD30 ekstrem — catat di shadow
                             # (HYPE/SMARTESTMONEY sendiri DD30 47,7% — biarkan
                             # data terkumpul yang memutuskan, bukan asumsi)

CASHTAG_RE = re.compile(r"\$([A-Z]{1,10})\b")
IDX_SLUG_RE = re.compile(r"/news/stock_n/([a-zA-Z0-9]{4})-")

# Stablecoin bukan objek sinyal "akan naik" — harganya dipatok. Buang dari ranking.
STABLECOINS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS", "BUSD",
    "FRAX", "USDD", "CRVUSD", "SUSD", "USDS", "RLUSD", "USD1", "USDF",
}
# Entitas bukan-kripto yang sering muncul dari news saham/tokenized stock.
STOCK_BLOCKLIST = {
    "MSTR", "OPENAI", "ANTHROPIC", "COIN", "HOOD", "TSLA", "NVDA", "AAPL",
    "META", "GOOG", "AMZN", "PLTR", "CRCL", "SPACEX", "SPX", "NDX",
}
# Simbol yang namanya sama dengan kata bahasa Inggris umum. Tagging wire
# (suggestions Tree News) kerap menangkap kata prosa dari isi tweet sebagai
# "coin" ("…Firm Being Based in Singapore" → BASED; akun media @Stable →
# STABLE — keduanya terverifikasi di ranking live 2026-10-07). Untuk simbol
# ini, atribusi via suggestions HANYAH sah bila judul memuat cashtag eksplisit
# $SYM; bukti lain (posisi exchange hyperliquid, cashtag) tetap sah.
# NEAR/SAFE/LINK sengaja TIDAK masuk — token volume-tinggi yang kehilangan
# atribusi wire lebih mahal daripada noise prosanya. Tumbuhkan set ini
# saat polusi baru terlihat di ranking.
AMBIGUOUS_PROSE_SYMBOLS = {
    "BASED", "STABLE", "MORE", "GOOD", "BEST", "WELL", "JUST", "ONLY",
    "VERY", "MOST", "GREAT", "MAJOR", "FIRST", "NEXT", "REAL", "TRUE",
    "FAST", "SMART", "DEEP", "HIGH", "LOW", "BIG", "TOP", "NEW", "MOON",
    "PUMP", "GAME", "PLAY", "SWAP", "FARM", "SPACE", "TIME", "WORLD",
    "GOLD", "LIVE", "OPEN", "LONG", "SHORT",
}

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
        if sym in STABLECOINS or sym in STOCK_BLOCKLIST:
            return  # stablecoin dipatok / entitas saham — bukan objek "akan naik"
        seen.add(sym)
        out.append((sym, venue, conf))

    source = item.get("source", "")
    title = item.get("title") or ""

    if source == "hyperliquid" and extra.get("coin"):
        add(str(extra["coin"]), "crypto", 1.0)  # posisi exchange — otoritatif
    for c in extra.get("coins") or []:
        sym = str(c).strip().upper()
        # Simbol kata-prosa dari tagging wire: buang kecuali judul memuat
        # cashtag eksplisit ($SYM) — bukan sekadar kata "Based/Stable" di kalimat.
        if sym in AMBIGUOUS_PROSE_SYMBOLS and f"${sym}" not in title.upper():
            continue
        add(sym, "crypto", 0.95)
    for m in CASHTAG_RE.findall(title):
        add(m, "crypto", 0.70)
    # IQPlus slug carries the IDX ticker (ekom-... → EKOM)
    slug = IDX_SLUG_RE.search(item.get("url") or "")
    if slug:
        add(slug.group(1), "idx", 0.80)
    return out


def _evaluate_gates(sources, gctx: Dict[str, Any]) -> List[str]:
    """Gerbang anti-salah-beli — SHADOW MODE: hanya mengembalikan daftar flag
    (skor tidak disentuh). Dipanggil per sinyal jadi; konteks gctx dikumpulkan
    per-simbol saat loop item (liq/sells/liq-fdv dari GeckoTerminal, DD30 dari
    kualitas wallet Hyperliquid)."""
    flags: List[str] = []
    if "geckoterminal" in sources:
        liq = gctx.get("liq_usd")
        if liq is not None and liq < GATE_MIN_LIQ_USD:
            flags.append(f"gate: likuiditas ${liq/1e3:.0f}K < $50K")
        sells = gctx.get("sells_24h")
        if sells is not None and sells <= GATE_HONEYPOT_SELLS:
            flags.append("gate: honeypot? 0 sell 24 jam")
        lf = gctx.get("lf")
        if lf is not None and lf < GATE_LIQ_FDV:
            flags.append(f"gate: liq/FDV {lf:.2f} < 0,07")
    if "hyperliquid" in sources:
        dd = gctx.get("dd30")
        if dd is not None and dd > GATE_DD30_PCT:
            flags.append(f"gate: DD30 wallet {dd:.0f}% > 40%")
    return flags


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
    # Baseline (BTC/IHSG) utk label excess-return di signal_log — sekali per
    # siklus; None bila fetch gagal (pipeline tak boleh mati karena baseline).
    baselines = await fetch_baselines()
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
                    # Katalis TERKUAT yang hadir — min() membuat MTO + stock split
                    # jadi lebih lemah dari MTO sendiri; komposit harus menguatkan.
                    mult = max(w for w, _ in matched)
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
            # Hanya LISTING yang punya pola dump terdokumentasi (Empirica: −37,64%
            # di 6 bulan). "Launch" Tree News sering = launch PRODUK (vault live,
            # release) — netral, jangan didiskon.
            if kind == "listing":
                news_mult = 0.6
                news_reason = "listing: pola dump pasca-announcement (−37% 6 bln) — beri diskon"
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
            # Konteks utk shadow gates — kumpulkan per simbol dari item ini
            # (liq/FDV/sells dari GeckoTerminal; DD30 maksimum antar wallet).
            gctx = entry.setdefault("gctx", {
                "liq_usd": None, "lf": None, "sells_24h": None, "dd30": None})
            if it.get("source") == "geckoterminal":
                try:
                    _gliq = float(extra.get("liquidity_usd") or 0)
                    _gfdv = float(extra.get("fdv_usd") or 0)
                    if _gliq > 0:
                        gctx["liq_usd"] = _gliq
                        if _gfdv > 0:
                            gctx["lf"] = _gliq / _gfdv
                    _gsells = (extra.get("tx_24h") or {}).get("sells")
                    if _gsells is not None:
                        gctx["sells_24h"] = float(_gsells)
                except (TypeError, ValueError):
                    pass
            if is_whale and not whale_hedge:
                _dd = (extra.get("quality") or {}).get("max_dd_30d_pct")
                if _dd is not None:
                    prev = gctx["dd30"] or 0.0
                    gctx["dd30"] = max(prev, float(_dd))
            contrib = w * decay * imp_boost * conf
            # Diminishing returns: artikel ke-N tentang simbol yang sama menambah
            # informasi sub-linear (1/√N). Tanpa ini mega-cap (BTC 38 item) selalu
            # memuncaki purely karena volume berita baseline-nya tinggi.
            contrib *= 1.0 / math.sqrt(max(1, entry["item_count"] + 1))
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
        if len(e["sources"]) >= 2:
            reasons.append(f"konsensus {len(e['sources'])} sumber")
        elif e["item_count"] >= 2:
            # ≥2 item dari 1 sumber = pengulangan wire, BUKAN konsensus.
            reasons.append(f"repeat {e['item_count']} item dari 1 sumber")
        if not reasons:
            continue  # one weak mention alone is noise — filter it out
        # Shadow gates — catat saja; skor & ranking tidak berubah (bab 6).
        gate_flags = _evaluate_gates(e["sources"], e.get("gctx") or {})
        signals.append({
            "symbol": sym,
            "venue": venue,
            "score": round(score, 4),
            "item_count": e["item_count"],
            "fresh_2h": e["fresh_2h"],
            "source_count": len(e["sources"]),
            "sources": sorted(e["sources"]),
            "reason_codes": reasons,
            "gate_flags": gate_flags,
            "titles": e["top_titles"],
        })

    signals.sort(key=lambda s: s["score"], reverse=True)

    # Log untuk pengukuran akurasi (hit-rate 4h/24h + EXCESS vs baseline) —
    # 1 baris per simbol per 2 jam. Gate flags ikut dicatat (prefiks GATE)
    # supaya review shadow-mode bisa menghitung "berapa banyak yang akan
    # diblokir". Baseline px disimpan SAAT sinyal dibuat (bebas look-ahead).
    bucket = now_ms // 7_200_000
    try:
        await storage.log_signal([
            {
                "bucket": bucket, "symbol": s["symbol"], "venue": s["venue"],
                "ts": now_ms, "score": s["score"],
                "reasons": json.dumps(
                    s["reason_codes"] + [f"GATE {g}" for g in s.get("gate_flags", [])],
                    ensure_ascii=False),
                "btc_px": baselines.get("BTC"),
                "ihsg_px": baselines.get("IHSG"),
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
