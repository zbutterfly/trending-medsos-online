"""Tree News fetcher — high-speed crypto/finance news wire (free, no key).

Endpoint : GET https://news.treeofalpha.com/api/news
Latency  : ~250 ms from event to API (field `importanceMs` measures this).
Bonus    : server-side dedup (`repeatOf`), importance score, symbol mapping
           (`suggestions[].coin`), so this is the primary "news spine".

Category : signal.news
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..core.http import Plain
from .base import BaseFetcher

log = logging.getLogger(__name__)

TREE_NEWS_URL = "https://news.treeofalpha.com/api/news"


class TreeNewsFetcher(BaseFetcher):
    CATEGORY = "signal.news"
    SOURCE = "treenews"
    FAST_LANE = True  # scheduler fast lane (60 s), see app/scheduler.py

    def __init__(self, limit: int = 300) -> None:
        self.limit = limit

    async def fetch(self) -> List[Dict[str, Any]]:
        client = Plain()
        try:
            resp = await client.get(TREE_NEWS_URL, params={"limit": str(self.limit)})
            resp.raise_for_status()
            rows: List[Dict[str, Any]] = resp.json()
        finally:
            await client.aclose()

        out: List[Dict[str, Any]] = []
        for row in rows:
            # `repeatOf` = server-side dedup: this item repeats an earlier one.
            if row.get("repeatOf"):
                continue
            rid = str(row.get("_id") or row.get("url") or "")
            if not rid:
                continue
            title = row.get("title") or row.get("en") or ""
            if not title:
                continue

            coins: List[str] = []
            symbols: List[str] = []
            for sug in row.get("suggestions") or []:
                coin = (sug.get("coin") or "").upper()
                if coin:
                    coins.append(coin)
                for sym in sug.get("symbols") or []:
                    s = sym.get("symbol") or ""
                    if s and s not in symbols:
                        symbols.append(s)

            out.append(self.record(
                id=rid,
                title=title[:300],
                url=row.get("url") or "",
                extra={
                    "published_ms": row.get("time"),
                    "importance": row.get("importance"),
                    "source_name": row.get("sourceName") or row.get("source"),
                    "kind": row.get("kind"),
                    "coins": coins,
                    "symbols": symbols,
                },
            ))
        log.info("treenews: kept %d/%d rows after dedup", len(out), len(rows))
        return out
