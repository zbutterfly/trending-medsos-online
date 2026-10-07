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

    Implementasi di app/accuracy.py — dipakai bersama endpoint ini dan
    export signals-accuracy.json di pipeline statis (scripts/run_once.py).
    """
    from ..accuracy import compute_accuracy

    return await compute_accuracy()


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
