"""Reddit public JSON fallback fetcher.

When PRAW credentials are missing, Reddit still exposes public JSON endpoints:
https://www.reddit.com/r/<sub>/hot.json?limit=25
This fetcher uses curl_cffi Stealth to avoid blocks and returns minimal records.

Category: sentiment.reddit
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..core.http import Stealth
from .base import BaseFetcher

log = logging.getLogger(__name__)

CRYPTO_SUBREDDITS = [
    "CryptoCurrency",
    "CryptoAirdrops",
    "AirdropAlert",
    "airdrops",
    "satoshistreetbets",
    "defi",
    "altcoin",
]

IDX_SUBREDDITS = [
    "IndonesianInvestments",
    "indonesia",
    "StockMarket",
    "stocks",
]


class RedditPublicHotFetcher(BaseFetcher):
    CATEGORY = "sentiment.reddit"
    SOURCE = "reddit-public-hot"

    def __init__(self, subreddits: Optional[List[str]] = None, limit_per_sub: int = 15) -> None:
        self.subreddits = subreddits or CRYPTO_SUBREDDITS
        self.limit_per_sub = limit_per_sub

    async def fetch(self) -> List[Dict[str, Any]]:
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_sync)

    def _fetch_sync(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        stealth = Stealth()
        try:
            for sub in self.subreddits:
                url = f"https://www.reddit.com/r/{sub}/hot.json?limit={self.limit_per_sub}"
                try:
                    resp = stealth.get(url, timeout=15, headers={"Accept": "application/json"})
                    if resp.status_code != 200:
                        log.debug("Reddit public %s status %d", sub, resp.status_code)
                        continue
                    data = resp.json()
                    children = data.get("data", {}).get("children", [])
                    for child in children:
                        d = child.get("data", {})
                        if d.get("distinguished") or d.get("stickied"):
                            continue
                        out.append(
                            self.record(
                                id=f"rd-pub-{d.get('id')}",
                                subreddit=sub,
                                post_id=d.get("id"),
                                title=d.get("title", ""),
                                score=d.get("score", 0),
                                num_comments=int(d.get("num_comments", 0) or 0),
                                created_utc=int(d.get("created_utc", 0)),
                                author=str(d.get("author") or ""),
                                url=d.get("url", ""),
                                permalink=f"https://reddit.com{d.get('permalink','')}",
                                kind="hot-public",
                            )
                        )
                except Exception as e:  # noqa: BLE001
                    log.debug("Reddit public %s failed: %s", sub, e)
                    continue
        finally:
            stealth.close()
        return out


class RedditPublicIdxFetcher(RedditPublicHotFetcher):
    CATEGORY = "sentiment.reddit_idx"
    SOURCE = "reddit-public-idx"

    def __init__(self) -> None:
        super().__init__(subreddits=IDX_SUBREDDITS, limit_per_sub=10)
