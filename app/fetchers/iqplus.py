"""IQPlus market news fetcher — realtime Indonesian stock news (scraped HTML).

Endpoint : https://iqplus.info/box_listnewsleft.php?csection=stock_n
Shape    : <li><b>DD/MM/YY - HH:MM</b><a href="http://.../news/stock_n/<ticker>-slug,ID.html">TITLE</a></li>

IQPlus is the news vendor wired into many Indonesian broker terminals, so this
is typically the fastest free IDX news wire available. Bonus: the URL slug
embeds the related ticker (e.g. `ekom-ojk--pinjaman...` → EKOM), which gives
us free entity extraction for the IDX side.

Verified live 2026-10-05 (plain HTML, no JS render needed).
Category : idx.news
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from ..core.http import stealth_get
from .base import BaseFetcher

log = logging.getLogger(__name__)

IQPLUS_URL = "https://iqplus.info/box_listnewsleft.php?csection=stock_n"

# <b>05/10/26 - 16:32</b><a href="...">TITLE</a>
LI_RE = re.compile(
    r"<b>(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}:\d{2})</b>\s*"
    r'<a href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
# Slug: /news/stock_n/ekom-ojk--pinjaman...,27759519.html → ticker = first token
SLUG_RE = re.compile(r"/news/stock_n/([a-zA-Z0-9]{4})-")


WIB = timezone(timedelta(hours=7))


def _parse_wib(date_ddmmyy: str, hhmm: str) -> int:
    """IQPlus timestamps are WIB (UTC+7). Return epoch ms."""
    dt = datetime.strptime(f"{date_ddmmyy} {hhmm}", "%d/%m/%y %H:%M").replace(tzinfo=WIB)
    return int(dt.timestamp() * 1000)


class IQPlusNewsFetcher(BaseFetcher):
    CATEGORY = "idx.news"
    SOURCE = "iqplus"
    FAST_LANE = True  # scheduler fast lane (60 s), see app/scheduler.py

    def fetch_sync(self) -> List[Dict[str, Any]]:
        resp = stealth_get(IQPLUS_URL, timeout=30)
        resp.raise_for_status()
        html = resp.text

        out: List[Dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        for m in LI_RE.finditer(html):
            date_s, hhmm, url, raw_title = m.groups()
            title = TAG_RE.sub("", raw_title).strip()
            if not title:
                continue
            try:
                published_ms = _parse_wib(date_s, hhmm)
            except ValueError:
                published_ms = now_ms
            # Drop obviously stale rows the box may still carry.
            if now_ms - published_ms > 7 * 24 * 3600 * 1000:
                continue

            slug = SLUG_RE.search(url or "")
            ticker = slug.group(1).upper() if slug else None

            rid = hashlib.md5((url or title).encode("utf-8")).hexdigest()[:16]
            out.append(self.record(
                id=rid,
                title=title[:300],
                url=url,
                extra={
                    "published_ms": published_ms,
                    "ticker": ticker,  # ticker from slug, may be None
                    "venue": "idx" if ticker else None,
                },
            ))
        return out

    async def fetch(self) -> List[Dict[str, Any]]:
        # curl_cffi session is sync; run in a worker thread to not block the loop.
        import asyncio

        return await asyncio.to_thread(self.fetch_sync)
