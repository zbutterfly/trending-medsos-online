"""Sentiment (Coin/Token) — crypto news RSS aggregators + Google News.

User asked: "sentiment tab jadi 2 saja = coin/token dan saham idx yang isinya
medsos reddit, x, thread, facebook, dan berita." Reddit + X (already in
`sentiment.py`); Threads/Facebook are NOT scrapable in 2026 (login walls —
see README). So this module covers the 'berita' leg for Coin/Token via:

- 10+ crypto-native RSS feeds (CoinDesk/Decrypt/CoinTelegraph/The Block/
  CryptoBriefing/U.Today/The Defiant/NewsBTC/ETHNews/CoinJournal/CryptoPotato)
- Google News RSS 'airdrop+crypto', 'bitcoin+crypto+trending'

Each fetcher has CATEGORY 'sentiment.coin.news' so the dashboard can group
them under the 'Sentiment Coin/Token' tab alongside
'sentiment.coin.reddit' and 'sentiment.coin.x' (those still live in
`sentiment.py` but will be renamed).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import feedparser

from ..core.http import Stealth
from .base import BaseFetcher

log = logging.getLogger(__name__)


class _CryptoRssFetcher(BaseFetcher):
    """Mixin: fetch an RSS feed via curl_cffi stealth, parse via feedparser."""

    CATEGORY = "sentiment.coin.news"
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


class CoinDeskRssFetcher(_CryptoRssFetcher):
    SOURCE = "coindesk"
    FEED_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"


class DecryptRssFetcher(_CryptoRssFetcher):
    SOURCE = "decrypt"
    FEED_URL = "https://decrypt.co/feed"


class CoinTelegraphRssFetcher(_CryptoRssFetcher):
    SOURCE = "cointelegraph"
    FEED_URL = "https://cointelegraph.com/rss"


class TheBlockRssFetcher(_CryptoRssFetcher):
    SOURCE = "theblock"
    FEED_URL = "https://www.theblock.co/rss.xml"


class CryptoBriefingRssFetcher(_CryptoRssFetcher):
    SOURCE = "cryptobriefing"
    FEED_URL = "https://cryptobriefing.com/feed/"


class UTodayRssFetcher(_CryptoRssFetcher):
    SOURCE = "utoday"
    FEED_URL = "https://u.today/rss"
    MAX_ITEMS = 25


class TheDefiantRssFetcher(_CryptoRssFetcher):
    SOURCE = "thedefiant"
    FEED_URL = "https://thedefiant.io/feed"


class NewsBtcRssFetcher(_CryptoRssFetcher):
    SOURCE = "newsbtc"
    FEED_URL = "https://www.newsbtc.com/feed/"


class EthNewsRssFetcher(_CryptoRssFetcher):
    SOURCE = "ethnews"
    FEED_URL = "https://www.ethnews.com/feed/"


class CoinJournalRssFetcher(_CryptoRssFetcher):
    SOURCE = "coinjournal"
    FEED_URL = "https://coinjournal.net/feed/"


class CryptoPotatoRssFetcher(_CryptoRssFetcher):
    SOURCE = "cryptopotato"
    FEED_URL = "https://cryptopotato.com/feed/"


class GoogleNewsCryptoTrendingFetcher(_CryptoRssFetcher):
    """Google News RSS 'bitcoin crypto trending' (English, US).

    Returns 100 items aggregated from various crypto news sites. Useful because
    Google News surfaces topics already ranked by readership. Free, no key.
    """

    SOURCE = "google-news-crypto-trending"
    FEED_URL = (
        "https://news.google.com/rss/search?q=bitcoin+crypto+trending&hl=en&gl=US&ceid=US:en"
    )
    MAX_ITEMS = 50


class GoogleNewsAirdropCryptoFetcher(_CryptoRssFetcher):
    """Google News RSS 'airdrop crypto' (English, US)."""

    SOURCE = "google-news-airdrop"
    FEED_URL = "https://news.google.com/rss/search?q=airdrop+crypto&hl=en&gl=US&ceid=US:en"
    MAX_ITEMS = 50
