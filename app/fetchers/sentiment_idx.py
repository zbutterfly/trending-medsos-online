"""Sentiment (Saham IDX) — IDX-market news RSS aggregators + Google News IHSG.

User asked: "sentiment tab jadi 2 saja = coin/token dan saham idx yang isinya
medsos reddit, x, thread, facebook, dan berita." RedditIDX + NitterIDX for
Indonesian markets already (in `sentiment.py`); Threads/Facebook NOT scrapable
in 2026 (see README). This module covers the 'berita' leg for idx markets via:

- IDX Channel RSS (https://www.idxchannel.com/rss) — 10 items, market headlines
- Tempo Bisnis RSS (https://rss.tempo.co/bisnis) — 50 items, broad business
- Google News RSS 'IHSG' (Indonesia market index) — 100 items
- Google News RSS 'IDX share price' — 100 items

CATEGORY 'sentiment.idx.news' so the dashboard can group these under the
'Sentiment Saham IDX' tab alongside 'sentiment.idx.reddit' and
'sentiment.idx.x' (those still live in `sentiment.py`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import feedparser

from ..core.http import Stealth
from .base import BaseFetcher

log = logging.getLogger(__name__)


class _IdxRssFetcher(BaseFetcher):
    """Mixin: RSS fetcher for IDX-market news sources via curl_cffi stealth."""

    CATEGORY = "sentiment.idx.news"
    FEED_URL: str = ""
    MAX_ITEMS: int = 30

    async def fetch(self) -> List[Dict[str, Any]]:
        if not self.FEED_URL:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._parse_sync)

    def _parse_sync(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            resp = stealth.get(self.FEED_URL, timeout=20)
            if resp.status_code != 200:
                log.info("%s RSS status=%d", self.SOURCE, resp.status_code)
                return []
            parsed = feedparser.parse(resp.content)
        except Exception as e:  # noqa: BLE001
            log.warning("%s RSS failed: %s", self.SOURCE, e)
            return []
        finally:
            stealth.close()

        out: List[Dict[str, Any]] = []
        for i, entry in enumerate(parsed.entries[: self.MAX_ITEMS]):
            link = entry.get("link") or ""
            out.append(
                self.record(
                    id=f"{self.SOURCE}-{i}-{link[-32:]}" if link else f"{self.SOURCE}-{i}",
                    rank=i + 1,
                    title=entry.get("title", ""),
                    summary=(entry.get("summary") or "")[:300],
                    author=entry.get("author"),
                    published=entry.get("published") or entry.get("updated"),
                    url=link,
                )
            )
        return out


class IDXChannelRssFetcher(_IdxRssFetcher):
    """IDX Channel (idxchannel.com) market RSS — 10 items of IHSG/Wall Street."""

    SOURCE = "idxchannel"
    FEED_URL = "https://www.idxchannel.com/rss"


class TempoBisnisRssFetcher(_IdxRssFetcher):
    """Tempo Bisnis RSS — 50 items of broad Indonesian business."""

    SOURCE = "tempo-bisnis"
    FEED_URL = "https://rss.tempo.co/bisnis"
    MAX_ITEMS = 40


class GoogleNewsIdxFetcher(_IdxRssFetcher):
    """Google News RSS 'IHSG' (Indonesian market index) — 100 items.

    Free, no key. Surfaces headlines from various ID-news sites ranked by
    readership. Excellent 'catch-all' for IDX-market sentiment.
    """

    SOURCE = "google-news-ihsg"
    FEED_URL = "https://news.google.com/rss/search?q=IHSG&hl=id&gl=ID&ceid=ID:id"
    MAX_ITEMS = 50


class GoogleNewsIdxSharesFetcher(_IdxRssFetcher):
    """Google News RSS 'IDX share price' — 100 items."""

    SOURCE = "google-news-idx-shares"
    FEED_URL = "https://news.google.com/rss/search?q=IDX+share+price&hl=id&gl=ID&ceid=ID:id"
    MAX_ITEMS = 50
