"""CoinMarketCap API fetchers — Basic plan compatible endpoints.

Available on Basic plan:
- /v3/cryptocurrency/listings/latest   — top cryptos by market cap (returns 250+)
  Use: sort=percent_change_24h&sort_dir=desc for top gainers
  Use: sort=market_cap&sort_dir=desc for top ranked

NOT available on Basic (requires Startup+ / Growth+):
- /v1/cryptocurrency/trending/latest       (403)
- /v1/cryptocurrency/trending/gainers-losers (403)
- /v1/community/trending/token             (403)
- /v1/community/trending/topic             (403)
- WebSocket channels                       (requires Startup+)

API key configured in backend/.env as CMC_API_KEY
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..core.config import settings
from .base import BaseFetcher, epoch_ms

log = logging.getLogger(__name__)

CMC_BASE = "https://pro-api.coinmarketcap.com"


class CoinMarketCapListingsFetcher(BaseFetcher):
    """Top cryptocurrencies by market cap from CMC (Basic plan endpoint).

    Returns top 50 by market cap with full quote data.
    """

    CATEGORY = "crypto.trending"
    SOURCE = "coinmarketcap"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        if not settings.cmc_api_key:
            log.warning("CMC_API_KEY not set — skipping CMC listings")
            return []

        headers = {"X-CMC_PRO_API_KEY": settings.cmc_api_key}
        url = f"{CMC_BASE}/v3/cryptocurrency/listings/latest"

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                url, headers=headers, params={"limit": 50, "sort": "market_cap", "sort_dir": "desc"}
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("data", [])
        out: List[Dict[str, Any]] = []
        for i, item in enumerate(items):
            quotes = item.get("quote", [])
            usd = next((q for q in quotes if isinstance(q, dict) and q.get("symbol") == "USD"), {})
            pct_24h = usd.get("percent_change_24h", 0) or 0
            price = usd.get("price")
            out.append(
                self.record(
                    id=f"cmc-listing-{item.get('id')}",
                    rank=item.get("cmc_rank", i + 1),
                    title=item.get("name", ""),
                    symbol=item.get("symbol", ""),
                    slug=item.get("slug", ""),
                    score=item.get("cmc_rank"),
                    price_usd=round(price, 6) if price else None,
                    pct_change_24h=round(pct_24h, 2) if pct_24h else None,
                    market_cap=usd.get("market_cap"),
                    volume_24h=usd.get("volume_24h"),
                    url=f"https://coinmarketcap.com/currencies/{item.get('slug', '')}",
                    extra={
                        "cmc_id": item.get("id"),
                        "cmc_rank": item.get("cmc_rank"),
                        "tags": item.get("tags", []),
                        "price_usd": price,
                        "pct_change_24h": pct_24h,
                        "market_cap": usd.get("market_cap"),
                        "volume_24h": usd.get("volume_24h"),
                        "market_cap_dominance": usd.get("market_cap_dominance"),
                    },
                )
            )
        return out


class CoinMarketCapGainersFetcher(BaseFetcher):
    """Top gainers by 24h change from CMC listings (Basic plan compatible).

    Sorts listings by percent_change_24h descending to find biggest gainers.
    """

    CATEGORY = "crypto.gainers"
    SOURCE = "coinmarketcap"

    async def fetch(self) -> List[Dict[str, Any]]:
        import httpx

        if not settings.cmc_api_key:
            log.warning("CMC_API_KEY not set — skipping CMC gainers")
            return []

        headers = {"X-CMC_PRO_API_KEY": settings.cmc_api_key}
        url = f"{CMC_BASE}/v3/cryptocurrency/listings/latest"

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                url, headers=headers, params={"limit": 100, "sort": "percent_change_24h", "sort_dir": "desc"}
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("data", [])
        out: List[Dict[str, Any]] = []
        for i, item in enumerate(items):
            quotes = item.get("quote", [])
            usd = next((q for q in quotes if isinstance(q, dict) and q.get("symbol") == "USD"), {})
            pct_24h = usd.get("percent_change_24h", 0) or 0
            price = usd.get("price")
            out.append(
                self.record(
                    id=f"cmc-gainer-{item.get('id')}",
                    rank=i + 1,
                    title=item.get("name", ""),
                    symbol=item.get("symbol", ""),
                    slug=item.get("slug", ""),
                    price_usd=round(price, 6) if price else None,
                    pct_change_24h=round(pct_24h, 2) if pct_24h else None,
                    url=f"https://coinmarketcap.com/currencies/{item.get('slug', '')}",
                    extra={
                        "cmc_id": item.get("id"),
                        "cmc_rank": item.get("cmc_rank"),
                        "price_usd": price,
                        "pct_change_24h": pct_24h,
                        "market_cap": usd.get("market_cap"),
                        "volume_24h": usd.get("volume_24h"),
                    },
                )
            )
        return out
