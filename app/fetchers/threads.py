"""Threads sentiment fetcher.

Threads has no official free public API in 2026. The unofficial pythreads
library requires OAuth2 with Threads App credentials + SSL certs, which is
impractical for most users. We therefore provide a best-effort fetcher that:

- Returns [] if THREADS_USERNAME / THREADS_PASSWORD are not set.
- Attempts a lightweight public scrape via curl_cffi as fallback for
  trending hashtags (best-effort, may be blocked).
- Never raises; always returns [] on failure.

Category: sentiment.threads
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.config import settings
from ..core.http import Stealth
from .base import BaseFetcher

log = logging.getLogger(__name__)


class ThreadsTrendingFetcher(BaseFetcher):
    CATEGORY = "sentiment.threads"
    SOURCE = "threads"

    DEFAULT_KEYWORDS = ["airdrop", "crypto", "MegaETH", "Monad", "Abstract chain", "trending coin"]

    def __init__(self, keywords: Optional[List[str]] = None, limit_per_keyword: int = 5) -> None:
        self.keywords = keywords or self.DEFAULT_KEYWORDS
        self.limit_per_keyword = limit_per_keyword

    async def fetch(self) -> List[Dict[str, Any]]:
        # Threads official API requires OAuth + SSL certs; most users won't have it.
        if settings.threads_enabled:
            # Future: use pythreads with credentials. For now, fall back to public scrape.
            log.info("Threads credentials present; public scrape fallback used for safety")
        # Best-effort public scrape fallback (no auth needed)
        return await self._public_fallback()

    async def _public_fallback(self) -> List[Dict[str, Any]]:
        """Best-effort public scrape of threads.net search.

        Many instances block scraping. We try once per keyword with curl_cffi stealth.
        """
        from selectolax.parser import HTMLParser

        out: List[Dict[str, Any]] = []
        for kw in self.keywords:
            try:
                stealth = Stealth()
                try:
                    # threads.net search URL pattern (may change)
                    url = f"https://www.threads.net/search?q={kw}"
                    resp = stealth.get(url, timeout=15, headers={"Accept": "text/html"})
                finally:
                    stealth.close()

                if resp.status_code != 200:
                    continue
                tree = HTMLParser(resp.text)
                # The DOM is heavily JS-driven; this is likely empty.
                # We look for any link containing /@username/post/
                items = tree.css("a[href*='/@']")[: self.limit_per_keyword]
                if not items:
                    continue
                for i, a in enumerate(items):
                    href = a.attributes.get("href", "")
                    text = a.text(strip=True)[:200]
                    if not href:
                        continue
                    full_url = f"https://www.threads.net{href}" if href.startswith("/") else href
                    out.append(
                        self.record(
                            id=f"th-{kw}-{i}-{href[-24:]}",
                            query=kw,
                            title=text or kw,
                            url=full_url,
                            kind="public-scrape",
                            extra={"raw_text": text},
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.debug("Threads public fallback failed for %s: %s", kw, e)
                continue
        return out
