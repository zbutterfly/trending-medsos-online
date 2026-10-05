"""BitcoinTalk bounty board scraper.

The original `bounties.py` has 3 fetchers (Immunefi, Gitcoin rounds, Binance
Square) which all return 0 records in 2026 — the public endpoints were
deprecated or moved behind auth. This module adds a scraper for BitcoinTalk
board 238 ("Bounties (Altcoins)"), the canonical free forum for signature
bounty campaigns in crypto.

URL structure:
  https://bitcointalk.org/index.php?board=238.0       — page 1 (most recent posts)
  https://bitcointalk.org/index.php?board=238.<offset> — pagination (40 per page)

We fetch page 1 + `MAX_PAGES`. Each post row is a <td class="windowbg">
with an anchor `<a href="https://bitcointalk.org/index.php?topic=NNNN.NNN">`
to the topic URL and title. We keep only titles starting with `[BOUNTY]` or
`[CONTEST]` (filters out the casino spam on this board).
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any, Dict, List

from .base import BaseFetcher
from ..core.http import Stealth

log = logging.getLogger(__name__)

_BOARD_URL = "https://bitcointalk.org/index.php?board=238.{offset}"
_MAX_PAGES = 3  # 3 * 40 = 120 topic slots scanned
_TITLE_FILTER = re.compile(r"^\s*\[(BOUNTY|CONTEST|CAMPAIGN)", re.IGNORECASE)
_TOPIC_LINK_RE = re.compile(
    r'<a[^>]+href="https://bitcointalk\.org/index\.php\?topic=(\d+)\.\d+"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
# second-pass for plain "index.php?topic=NNNN" inside spans (older UI variant)
_TOPIC_LINK_RE_B = re.compile(
    r'index\.php\?topic=(\d+)\.\d+[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)


class BitcoinTalkBountyFetcher(BaseFetcher):
    """Scrape BitcoinTalk board 238 (Bounties) for active bounty topics."""

    CATEGORY = "bounty"
    SOURCE = "bitcointalk"

    async def fetch(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            seen_ids: set[str] = set()
            out: List[Dict[str, Any]] = []
            for page in range(_MAX_PAGES):
                offset = page * 40
                url = _BOARD_URL.format(offset=offset)
                try:
                    resp = stealth.get(
                        url,
                        headers={"Accept": "text/html,application/xhtml+xml"},
                        timeout=25,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("bitcointalk board fetch failed page=%d err=%s", page, e)
                    continue
                if resp.status_code != 200:
                    log.info("bitcointalk status=%d url=%s", resp.status_code, url)
                    continue
                html_text = resp.text
                # collect all topic-link anchors
                matches: List[tuple[str, str]] = []
                for m in _TOPIC_LINK_RE.finditer(html_text):
                    matches.append((m.group(1), unescape(m.group(2)).strip()))
                if not matches:  # older UI
                    for m in _TOPIC_LINK_RE_B.finditer(html_text):
                        matches.append((m.group(1), unescape(m.group(2)).strip()))
                # filter for bounty / contest / campaign threads
                for tid, title in matches:
                    if not _TITLE_FILTER.search(title):
                        continue
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    # title often has em-dash ' — ' separating the [BOUNTY] tag from name
                    clean_title = re.sub(r"\s+", " ", title).strip()
                    out.append(
                        self.record(
                            id=f"btt-{tid}",
                            rank=len(out) + 1,
                            title=clean_title,
                            topic_id=tid,
                            url=f"https://bitcointalk.org/index.php?topic={tid}.0",
                            forum="bitcointalk.org :: Bounties (Altcoins)",
                            source_board=238,
                            page=page + 1,
                        )
                    )
                if len(out) >= 50:
                    break
            return out
        finally:
            stealth.close()
