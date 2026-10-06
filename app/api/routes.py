"""Main REST router — exposes data to the frontend."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from ..db import storage
from ..fetchers.registry import FETCHER_GROUPS, all_fetchers
from ..scheduler import get_last_run, refresh_now
from .. import signals

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["trending"])


# === Generic item listing ===
@router.get("/items")
async def list_items_endpoint(
    category: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(100, ge=1, le=2000),
):
    """List items (optionally filtered by category/source) ordered by last_seen."""
    rows = await storage.list_items(category=category, source=source, limit=limit)
    return {"count": len(rows), "items": rows}


@router.get("/category-stats")
async def category_stats_endpoint():
    """Return per-category counts + last fetched timestamps."""
    cats = await storage.list_categories()
    return {"categories": cats, "groups": FETCHER_GROUPS}


# === Trending — grouped fetch ===
@router.get("/trending/crypto")
async def trending_crypto_endpoint(limit: int = Query(50, le=500)):
    cg_trend = await storage.list_items(category="crypto.trending", limit=limit)
    cg_trend_coingecko = [r for r in cg_trend if r.get("source") == "coingecko"]
    cg_trend_dex = [r for r in cg_trend if r.get("source") in ("dexscreener",)]
    cg_trend_dex_vol = await storage.list_items(category="crypto.trending_volume", limit=limit)
    gainers = await storage.list_items(category="crypto.gainers", limit=limit)
    return {
        "coingecko_trending": cg_trend_coingecko,
        "dexscreener_trending": cg_trend_dex + cg_trend_dex_vol,
        "coingecko_gainers_24h": gainers,
    }


@router.get("/airdrops")
async def airdrops_endpoint(limit: int = Query(80, le=500)):
    rows = await storage.list_items(category="airdrop", limit=limit)
    return {"count": len(rows), "items": rows}


@router.get("/bounties")
async def bounties_endpoint(limit: int = Query(50, le=500)):
    rows = await storage.list_items(category="bounty", limit=limit)
    return {"count": len(rows), "items": rows}


@router.get("/sentiment/reddit")
async def sentiment_reddit_endpoint(limit: int = Query(100, le=500)):
    hot = await storage.list_items(category="sentiment.reddit", limit=limit)
    top24 = await storage.list_items(category="sentiment.reddit", limit=0) if False else []
    # Hot already contains kind=hot and top-day in one category, so we filter client-side.
    idx_reddit = await storage.list_items(category="sentiment.reddit_idx", limit=limit)
    return {"reddit_crypto_hot": [r for r in hot if r.get("kind") == "hot"], "reddit_crypto_top24h": [r for r in hot if r.get("kind") == "top-day"], "reddit_idx": idx_reddit}


@router.get("/sentiment/x")
async def sentiment_x_endpoint(limit: int = Query(80, le=500)):
    rows = await storage.list_items(category="sentiment.x", limit=limit)
    return {"count": len(rows), "items": rows}


@router.get("/emerging-chains")
async def emerging_chains_endpoint():
    status_rows = await storage.list_items(category="emerging_chain.status", limit=20)
    news_rows = await storage.list_items(category="emerging_chain.news", limit=60)
    social_rows = await storage.list_items(category="emerging_chain.social", limit=60)
    return {"status": status_rows, "news": news_rows, "social": social_rows}


@router.get("/idx/news")
async def idx_news_endpoint(limit: int = Query(60, le=500)):
    rows = await storage.list_items(category="idx.news", limit=limit)
    return {"count": len(rows), "items": rows}


@router.get("/idx/announcements")
async def idx_announcements_endpoint(limit: int = Query(60, le=500)):
    rows = await storage.list_items(category="idx.announcement", limit=limit)
    return {"count": len(rows), "items": rows}


# === Signal Filter (ranked symbols) ===
@router.get("/signals")
async def signals_endpoint(
    limit: int = Query(50, ge=1, le=100),
    venue: Optional[str] = Query(None, pattern="^(crypto|idx)$"),
):
    """Ranked per-symbol signals — heuristic v0 (see app/signals.py).

    venue=crypto|idx filters the ranked list; raw items live in
    signal.news / signal.onchain categories (fetchable via /api/items).
    """
    payload = await signals.compute_signals()
    sigs = payload["signals"]
    if venue:
        sigs = [s for s in sigs if s["venue"] == venue]
    payload["signals"] = sigs[:limit]
    payload["count"] = len(payload["signals"])
    return payload


@router.get("/signals/accuracy")
async def signals_accuracy_endpoint():
    """Hit-rate sinyal terlog: apakah harga 4h/24h setelah sinyal naik?

    Sinyal kita implisit bullish, jadi "hit" = harga lebih tinggi dari harga saat
    sinyal. Harga dari Binance public mirror (data-api.binance.vision), per jam.
    Baru bermakna setelah signal_log terisi ≥ beberapa jam oleh scheduler.
    """
    import time as _t

    import httpx

    now_ms = int(_t.time() * 1000)
    hour = 3_600_000
    rows = await storage.list_signal_log(limit=1000)
    crypto_rows = [r for r in rows if r["venue"] == "crypto"]
    symbols = sorted({r["symbol"] for r in crypto_rows})

    closes_map: dict = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for sym in symbols[:60]:  # batasi biar cepat
            try:
                resp = await client.get(
                    "https://data-api.binance.vision/api/v3/klines",
                    params={"symbol": f"{sym}USDT", "interval": "1h", "limit": 500},
                )
                if resp.status_code != 200:
                    closes_map[sym] = {}
                    continue
                out = {}
                for k in resp.json():
                    try:
                        out[int(k[0])] = float(k[4])  # open time → close
                    except (TypeError, ValueError, IndexError):
                        continue
                closes_map[sym] = out
            except Exception:  # noqa: BLE001
                closes_map[sym] = {}

    def price_at(closes: dict, ts: int, search: int = 2) -> Optional[float]:
        """Bar jam terdekat (cari ±search jam) dari ts."""
        base = ts - ts % hour
        for off in [0] + [s * hour for s in range(1, search + 1)]:
            for t in ([base - off] if off == 0 else [base - off, base + off]):
                if t in closes:
                    return closes[t]
        return None

    n4 = h4 = n24 = h24 = 0
    rets4: List[float] = []
    rets24: List[float] = []
    for r in crypto_rows:
        closes = closes_map.get(r["symbol"]) or {}
        if not closes:
            continue
        p0 = price_at(closes, r["ts"])
        if p0 is None or p0 <= 0:
            continue
        if r["ts"] <= now_ms - 4 * hour:
            p4 = price_at(closes, r["ts"] + 4 * hour)
            if p4 is not None:
                n4 += 1
                rets4.append((p4 - p0) / p0 * 100)
                if p4 > p0:
                    h4 += 1
        if r["ts"] <= now_ms - 24 * hour:
            p24 = price_at(closes, r["ts"] + 24 * hour)
            if p24 is not None:
                n24 += 1
                rets24.append((p24 - p0) / p0 * 100)
                if p24 > p0:
                    h24 += 1

    def pct(n: int, d: int) -> Optional[float]:
        return round(n / d * 100, 1) if d else None

    return {
        "generated_at": now_ms,
        "logged_signals": len(rows),
        "crypto_symbols_tracked": len(symbols),
        "evaluated_4h": n4,
        "hit_rate_4h_pct": pct(h4, n4),
        "avg_ret_4h_pct": round(sum(rets4) / len(rets4), 3) if rets4 else None,
        "evaluated_24h": n24,
        "hit_rate_24h_pct": pct(h24, n24),
        "avg_ret_24h_pct": round(sum(rets24) / len(rets24), 3) if rets24 else None,
        "note": (
            "Sinyal implisit bullish (filter berita/smart-money). Hit-rate <50% "
            "bukan otomatis rugi — R:R menentukan. Butuh ±hari agar signal_log "
            "terisi cukup untuk bermakna."
        ),
    }


# === Lifecycle ===
@router.post("/refresh")
async def refresh_endpoint():
    """Trigger an immediate refresh of all data sources."""
    summary = await refresh_now()
    return {"summary": summary, "last_run": get_last_run()}


@router.get("/health")
async def health_endpoint():
    """Health endpoint — lists every fetcher + last run summary + recent runs."""
    fetcher_info = [
        {"category": f.CATEGORY, "source": f.SOURCE, "cls": type(f).__name__}
        for f in all_fetchers()
    ]
    runs = await storage.latest_runs(limit=10)
    return {
        "fetchers": fetcher_info,
        "last_run": get_last_run(),
        "recent_runs": runs,
        "groups": FETCHER_GROUPS,
    }
