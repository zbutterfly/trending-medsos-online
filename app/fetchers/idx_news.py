"""IDX (Indonesia Stock Exchange) news + announcements fetchers.

Sources:
- idx.co.id announcements (Pengumuman) — HTML page; archive by date.
- Bisnis.com market RSS feed
- Kontan.co.id market RSS
- CNN Indonesia market RSS
- RTI Business (rti.id) free ticker news (optional; structured)
- Investing.com Indonesia RSS (Bali crypto exchange news)

We use feedparser for RSS and curl_cffi for the HTML pages.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from selectolax.parser import HTMLParser

from ..core.http import Stealth
from .base import BaseFetcher, epoch_ms

log = logging.getLogger(__name__)


class IdxAnnouncementsFetcher(BaseFetcher):
    """Scrape idx.co.id announcements (pengumuman listing & suspension).

    Page: https://www.idx.co.id/StaticData/Announcement/EmitenAnnouncement
    JSON-backed table endpoint at https://www.idx.co.id/primary/Announcement/...
    Returns recent corporate-action announcments (split, dividend, delisting).
    """

    CATEGORY = "idx.announcement"
    SOURCE = "idx.co.id"

    async def fetch(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            # IDX official JSON endpoint for corporate announcements.
            url = "https://www.idx.co.id/primary/Announcement/GetAnnouncement"
            # No params returns latest; we put a small page size.
            resp = stealth.get(
                url,
                params={"length": 30, "start": 0},
                headers={"Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code != 200:
                log.info("IDX announcement status=%d", resp.status_code)
                return []
            try:
                data = resp.json()
            except Exception:
                return []
        finally:
            stealth.close()

        items = data.get("data") or data.get("results") or []
        out: List[Dict[str, Any]] = []
        for i, item in enumerate(items[:40]):
            ann_id = item.get("ID") or item.get("id") or f"idx-ann-{i}"
            # Some records use Bahasa keys.
            title = item.get("Title") or item.get("title") or item.get("Name") or item.get("name")
            emiten = item.get("EmitenCode") or item.get(" kode") or item.get("ticker") or ""
            file_url = item.get("FileURL") or item.get("fileUrl")
            ann_date = item.get("AnnouncementDate") or item.get("date")
            content = (item.get("Content") or item.get("content") or "")[:300]
            out.append(
                self.record(
                    id=f"idx-ann-{ann_id}",
                    rank=i + 1,
                    title=title or "IDX Announcement",
                    emiten=emiten,
                    announcement_date=ann_date,
                    file_url=file_url,
                    content=content,
                    announcement_type=item.get("type") or item.get("Type"),
                    url=file_url or "https://www.idx.co.id/id/data-pasar/bursa/umuman-pasar",
                )
            )
        return out


class _RssMixin(BaseFetcher):
    """Common RSS fetch helper. Subclasses provide FEED_URL."""

    FEED_URL: str = ""

    async def fetch(self) -> List[Dict[str, Any]]:
        """Subclasses inherit this; they may override `_parse_sync` for custom parsing."""
        return await self._fetch_rss()

    async def _fetch_rss(self) -> List[Dict[str, Any]]:
        import feedparser

        # feedparser is sync; run in executor.
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._parse_sync)

    def _parse_sync(self) -> List[Dict[str, Any]]:
        import feedparser

        stealth = Stealth()
        try:
            resp = stealth.get(self.FEED_URL, timeout=15)
            if resp.status_code != 200:
                log.info("%s RSS status=%d", self.SOURCE, resp.status_code)
                return []
            parsed = feedparser.parse(resp.content)
        finally:
            stealth.close()

        out: List[Dict[str, Any]] = []
        for i, entry in enumerate(parsed.entries[:30]):
            link = entry.get("link") or ""
            published = entry.get("published") or entry.get("updated")
            out.append(
                self.record(
                    id=f"{self.SOURCE}-{i}-{link[-32:]}" if link else f"{self.SOURCE}-{i}",
                    rank=i + 1,
                    title=entry.get("title", ""),
                    summary=(entry.get("summary") or "")[:300],
                    published=published,
                    url=link,
                )
            )
        return out


class BisnisMarketRSSFetcher(_RssMixin):
    """Bisnis.com Saham RSS feed (free, no key)."""

    CATEGORY = "idx.news"
    SOURCE = "bisnis.com"
    FEED_URL = "https://www.bisnis.com/rss/feed/rss/saham"


class KontanMarketRSSFetcher(_RssMixin):
    """Kontan.co.id market RSS (free)."""

    CATEGORY = "idx.news"
    SOURCE = "kontan.co.id"
    FEED_URL = "https://www.kontan.co.id/rss"
    # Kontan splits multiple feeds; choose market section
    # https://www.kontan.co.id/rss/?index=1 (market)
    # https://www.kontan.co.id/rss/?channel=investasi

    def _parse_sync(self) -> List[Dict[str, Any]]:
        from selectolax.parser import HTMLParser  # noqa: F401

        import feedparser

        stealth = Stealth()
        try:
            # Kontan lists multiple subfeeds; we pull 'investasi' (stocks).
            resp = stealth.get("https://www.kontan.co.id/rss/?channel=investasi", timeout=15)
            if resp.status_code != 200:
                return []
            parsed = feedparser.parse(resp.content)
        finally:
            stealth.close()

        out: List[Dict[str, Any]] = []
        for i, entry in enumerate(parsed.entries[:30]):
            link = entry.get("link") or ""
            out.append(
                self.record(
                    id=f"kontan-{i}-{link[-24:]}",
                    rank=i + 1,
                    title=entry.get("title", ""),
                    summary=(entry.get("summary") or "")[:300],
                    published=entry.get("published") or entry.get("updated"),
                    url=link,
                )
            )
        return out


class CNNIndonesiaMarketRSSFetcher(_RssMixin):
    """CNN Indonesia market RSS (free)."""

    CATEGORY = "idx.news"
    SOURCE = "cnnindonesia.com"
    FEED_URL = "https://www.cnnindonesia.com/ekonomi/feed"
    # PasarSaham subsection is sometimes:
    # https://www.cnnindonesia.com/ekonomi/pasar-saham/rss


class InvestingIdRSSFetcher(_RssMixin):
    """Investing.com Indonesia news RSS (free)."""

    CATEGORY = "idx.news"
    SOURCE = "investing.com"
    FEED_URL = "https://id.investing.com/rss/news_301.rss"

    async def fetch(self) -> List[Dict[str, Any]]:
        # Investing.com often blocks; redirect via curl_cffi.
        return await self._fetch_rss()
