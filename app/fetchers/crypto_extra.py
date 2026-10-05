"""Additional crypto trending / market-sentiment fetchers beyond CoinGecko.

Sources (free, no API key in 2026 verify):
- DEXScreener v2 — `metas/trending/v1` (trending metas) + `token-boosts/top/v1`
  (the OLD `/token-boosts/top-in-24h` and `/tokens/v1/trending` endpoints are dead).
- CryptoRank — `api.cryptorank.io/v0/coins` free JSON, lists coins with rank +
  growth metrics, great for "early-stage alpha" achievement.
- alternative.me Fear & Greed Index — market sentiment bucket (0-100).
- CryptoCompare top volume — `min-api.cryptocompare.com/data/top/totalvolfull`
  (host shifted — re-resolve on use; falls back gracefully).

Docs:
  https://docs.dexscreener.com/api/reference
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import BaseFetcher

log = logging.getLogger(__name__)


class DEXScreenerTrendingMetasFetcher(BaseFetcher):
    """DEXScreener new endpoint `/metas/trending/v1` (1 page = ~18 'metas').

    Each 'meta' is a category/story that's accelerating in on-chain volume
    (e.g. 'memecoins', 'AI tokens', 'real-world-asset'). Best surface for
    alpha-style early discovery.
    """

    CATEGORY = "crypto.trending"
    SOURCE = "dexscreener-meta"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        url = "https://api.dexscreener.com/metas/trending/v1"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                log.info("DexScreener metas/trending status=%d", resp.status_code)
                return []
            data = resp.json()

        out: List[Dict[str, Any]] = []
        for i, m in enumerate(data if isinstance(data, list) else []):
            mid = m.get("slug") or m.get("name") or f"meta-{i}"
            out.append(
                self.record(
                    id=f"dx-meta-{mid}",
                    rank=i + 1,
                    title=m.get("name", ""),
                    slug=m.get("slug"),
                    description=(m.get("description") or "")[:280],
                    market_cap_usd=m.get("marketCap"),
                    liquidity_usd=m.get("liquidity"),
                    volume_24h_usd=m.get("volume"),
                    token_count=m.get("tokenCount"),
                    market_cap_change_24h=m.get("marketCapChange"),
                    icon_url=(m.get("icon") or {}).get("value") if isinstance(m.get("icon"), dict) else None,
                    url=f"https://dexscreener.com/trending/{m.get('slug', '')}" if m.get("slug") else "https://dexscreener.com/trending",
                )
            )
        return out


class DEXScreenerBoostedTokensFetcher(BaseFetcher):
    """DEXScreener `/token-boosts/top/v1` — most community-boosted tokens last 24h.

    These are tokens that paid DEXScreener to be featured but have real recent
    on-chain activity (volume + liquidity). Pragmatic alpha signal.
    """

    CATEGORY = "crypto.trending"
    SOURCE = "dexscreener-boosted"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        url = "https://api.dexscreener.com/token-boosts/top/v1"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                log.info("DexScreener token-boosts/top status=%d", resp.status_code)
                return []
            data = resp.json()

        out: List[Dict[str, Any]] = []
        for i, item in enumerate(data if isinstance(data, list) else []):
            addr = item.get("tokenAddress") or item.get("pairAddress") or f"b-{i}"
            out.append(
                self.record(
                    id=f"dx-boost-{item.get('chainId','')}-{addr}",
                    rank=i + 1,
                    title=item.get("description") or item.get("name") or addr[:10],
                    symbol=(item.get("symbol") or "").upper(),
                    chain_id=item.get("chainId"),
                    token_address=item.get("tokenAddress"),
                    pair_address=item.get("pairAddress"),
                    boosts_24h=item.get("totalAmountBoosts24h") or item.get("nBoosts24h"),
                    url=item.get("url") or f"https://dexscreener.com/{item.get('chainId','')}/{item.get('pairAddress','')}",
                )
            )
        return out


class CryptoRankTrendingFetcher(BaseFetcher):
    """CryptoRank free `/v0/coins` endpoint — list of all coins with market cap +
    recent growth data. We surface the top "rising" entries based on
    `change` percentage over 24h.
    """

    CATEGORY = "crypto.trending"
    SOURCE = "cryptorank"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        url = "https://api.cryptorank.io/v0/coins"
        params = {"limit": 100}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                log.info("CryptoRank status=%d", resp.status_code)
                return []
            data = resp.json()

        # CryptoRank returns {data:[...], meta:{...}}
        coins = data if isinstance(data, list) else (data.get("data") or data.get("coins") or [])
        scored: List[Dict[str, Any]] = []
        for c in coins:
            change_24h = (c.get("change") or {}).get("24h") if isinstance(c.get("change"), dict) else c.get("changePercent24h") or c.get("change24h") or 0
            try:
                change_24h = float(change_24h)
            except (TypeError, ValueError):
                change_24h = 0.0
            scored.append({"row": c, "change_24h": change_24h})
        scored.sort(key=lambda x: x["change_24h"], reverse=True)

        out: List[Dict[str, Any]] = []
        for i, entry in enumerate(scored[:30]):
            c = entry["row"]
            cid = str(c.get("id") or c.get("key") or "")
            slug = c.get("slug") or c.get("symbol") or cid
            out.append(
                self.record(
                    id=f"cr-trend-{cid}",
                    rank=i + 1,
                    title=c.get("name", "") or c.get("symbol", ""),
                    symbol=str(c.get("symbol", "")).upper(),
                    slug=slug,
                    price_usd=c.get("price") or c.get("priceUsd"),
                    market_cap_usd=c.get("marketCap") or c.get("marketCapUsd"),
                    volume_24h_usd=c.get("volume24hBase") or c.get("volume24h"),
                    change_pct_24h=entry["change_24h"],
                    thumb=c.get("image", {}).get("large") if isinstance(c.get("image"), dict) else None,
                    url=f"https://cryptorank.io/coins/{slug}" if slug else "https://cryptorank.io/",
                )
            )
        return out


class FearGreedIndexFetcher(BaseFetcher):
    """alternative.me Fear & Greed Index — single number 0-100 (extreme fear →
    extreme greed). Excellent at-a-glance market sentiment. We surface it as a
    single special-categorized 'crypto.trending' record so the UI shows it.
    """

    CATEGORY = "crypto.trending"
    SOURCE = "fear-greed"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        url = "https://api.alternative.me/fng/"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()

        items = data.get("data", [])
        if not items:
            return []
        latest = items[0]
        val = int(latest.get("value", 0))
        out: List[Dict[str, Any]] = [
            self.record(
                id="fng-today",
                rank=0,
                title=f"Fear & Greed Index: {latest.get('value_classification', '?')} ({val}/100)",
                value=val,
                classification=latest.get("value_classification"),
                timestamp=latest.get("timestamp"),
                time_until_update=latest.get("time_until_update"),
                url="https://alternative.me/crypto/fear-and-greed-index/",
            )
        ]
        return out
