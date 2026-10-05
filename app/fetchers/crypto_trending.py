"""Crypto trending fetchers — CoinGecko `/search/trending` + DEXScreener.

Both are free, no API key needed. CoinGecko has rate limits; we cache + retry.

Docs:
- CoinGecko Trending: https://docs.coingecko.com/reference/trending-endpoint
- DEXScreener: https://docs.dexscreener.com/api

Note: CoinGecko free tier since 2024 requires demo key via `x-cg-demo-api-key`
header. Without it the public endpoints work but heavily rate-limited.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..core.config import settings
from .base import BaseFetcher, epoch_ms

log = logging.getLogger(__name__)


class CoinGeckoTrendingFetcher(BaseFetcher):
    CATEGORY = "crypto.trending"
    SOURCE = "coingecko"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        headers = {}
        if settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
        url = "https://api.coingecko.com/api/v3/search/trending"

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        out: List[Dict[str, Any]] = []
        coins = data.get("coins", [])
        for i, entry in enumerate(coins):
            item = entry.get("item", {})
            coin_id = item.get("id")
            slug = item.get("api_symbol") or item.get("id") or ""
            data_dict = item.get("data", {}) or {}
            price_btc = data_dict.get("price_btc")
            price_usd = None
            if isinstance(data_dict.get("price"), (int, float)):
                price_usd = float(data_dict["price"])
            out.append(
                self.record(
                    id=f"cg-trend-{coin_id}",
                    rank=item.get("market_cap_rank") or i + 1,
                    title=item.get("name", ""),
                    symbol=item.get("symbol", ""),
                    slug=slug,
                    thumb=item.get("small") or item.get("large") or "",
                    score=item.get("score"),
                    price_btc=price_btc,
                    price_usd=price_usd,
                    url=f"https://www.coingecko.com/en/coins/{slug}" if slug else "",
                )
            )
        return out


class CoinGeckoGainersFetcher(BaseFetcher):
    """Top gainers in last 24h — uses `/coins/markets` with `price_change_percentage=24h`.

    Coins/markets is free; heavy rate limit (10-30/min) without key.
    """

    CATEGORY = "crypto.gainers"
    SOURCE = "coingecko"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        headers = {}
        if settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Filter top gainers only (% change > 10), then sort desc.
        gainers = [c for c in data if (c.get("price_change_percentage_24h") or 0) > 0.0]
        gainers.sort(
            key=lambda c: c.get("price_change_percentage_24h") or 0.0,
            reverse=True,
        )

        out: List[Dict[str, Any]] = []
        for i, c in enumerate(gainers[:30]):
            out.append(
                self.record(
                    id=f"cg-gain-{c.get('id')}",
                    rank=i + 1,
                    title=c.get("name", ""),
                    symbol=c.get("symbol", "").upper(),
                    price_usd=c.get("current_price"),
                    market_cap_usd=c.get("market_cap"),
                    volume_24h_usd=c.get("total_volume"),
                    change_pct_24h=c.get("price_change_percentage_24h"),
                    thumb=c.get("image", ""),
                    url=f"https://www.coingecko.com/en/coins/{c.get('id')}",
                )
            )
        return out


class DEXScreenerTrendingFetcher(BaseFetcher):
    """DEXScreener: JSON list of trending tokens across all chains.

    Endpoint: https://api.dexscreener.com/tokens/v1/{specific} — but the public
    "trending" path doesn't document a single endpoint. Use the newer
    `token-boosted-top-in-24h` endpoint that ranks by FTM boost votes.

    Docs: https://docs.dexscreener.com/api/reference
    """

    CATEGORY = "crypto.trending"
    SOURCE = "dexscreener"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        # /token-boosts/top-in-24h returns most upvoted tokens in last 24h.
        # /latest/dex/search returns by query; we use top-boosts since trending.
        url = "https://api.dexscreener.com/token-boosts/top-in-24h"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        # Take only strategic amount — that endpoint returns only boosted ones.
        # For broader trending, augment with most-actively-traded tokens too.
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for i, item in enumerate(data[:30] if isinstance(data, list) else []):
            token_id = item.get("tokenAddress") or item.get("pairAddress") or f"t-{i}"
            if token_id in seen:
                continue
            seen.add(token_id)
            out.append(
                self.record(
                    id=f"dx-trend-{token_id}",
                    rank=i + 1,
                    title=item.get("description") or item.get("name") or item.get("tokenAddress", "")[:10],
                    symbol=(item.get("symbol") or "").upper(),
                    chain_id=item.get("chainId"),
                    pair_address=item.get("pairAddress"),
                    token_address=item.get("tokenAddress"),
                    price_usd=float(item.get("priceNative") or 0) or None,
                    boosts=item.get("totalAmountBoosts24h") or item.get("n artificially"),
                    url=item.get("url") or f"https://dexscreener.com/{item.get('chainId')}/{item.get('pairAddress')}",
                )
            )

        return out


class DEXScreenerTrendingVolumeFetcher(BaseFetcher):
    """Augment DEXScreener with `/tokens/v1/trending` which by volume — newer endpoint.

    Note: `/tokens/v1/trending` may change without notice; we tolerate 404.
    """

    CATEGORY = "crypto.trending_volume"
    SOURCE = "dexscreener"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        url = "https://api.dexscreener.com/tokens/v1/trending"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code in (404, 400):
                log.info("DexScreener /tokens/v1/trending unavailable (%d) - skipping", resp.status_code)
                return []
            resp.raise_for_status()
            data = resp.json()

        out: List[Dict[str, Any]] = []
        for i, item in enumerate(data[:30] if isinstance(data, list) else []):
            addr = item.get("tokenAddress") or item.get("pairAddress") or f"v-{i}"
            out.append(
                self.record(
                    id=f"dx-vol-{addr}",
                    rank=i + 1,
                    title=item.get("name", ""),
                    symbol=(item.get("symbol") or "").upper(),
                    chain_id=item.get("chainId"),
                    token_address=item.get("tokenAddress"),
                    volume_24h=item.get("volume", {}).get("h24") if isinstance(item.get("volume"), dict) else None,
                    txns_24h=item.get("txns", {}).get("h24") if isinstance(item.get("txns"), dict) else None,
                    price_change_24h_pct=item.get("priceChange", {}).get("h24") if isinstance(item.get("priceChange"), dict) else None,
                    url=item.get("url") or "",
                )
            )
        return out
