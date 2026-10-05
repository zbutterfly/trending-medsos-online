"""Bounty hunter aggregator — crypto bug bounties + onchain tasks.

Sources:
- Immunefi (web3 bug bounty leader) — public bug bounty list, no key.
- Gitcoin Grants rounds — public API.
- Binance Square — RSS-style trending tips; we use Binance's blog via RSS feed.

Use https://dev.immunefi.com/ if documented later; for now scrape the public
bounties page via curl_cffi.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from selectolax.parser import HTMLParser

from ..core.http import Stealth
from .base import BaseFetcher

log = logging.getLogger(__name__)


class ImmunefiBountiesFetcher(BaseFetcher):
    """Immunefi: web3 bug bounty listings.

    Public page at https://immunefi.com/bug-bounties/ — JS-rendered but
    Next.js emits __NEXT_DATA__ JSON. Or scrape `https://immunefi.com/_next/*`
    payload if needed. Also kind: `https://immunefi.com/bug-bounty/{slug}`.

    If __NEXT_DATA__ missing, fallback to the GraphQL endpoint
    `https://immunefi.com/api/bounties` (optional auth). Currently the simplest
    path is their public real-time bounty widget endpoint.
    """

    CATEGORY = "bounty"
    SOURCE = "immunefi"

    async def fetch(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            resp = stealth.get(
                "https://immunefi.com/bug-bounties/",
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            if resp.status_code != 200:
                log.info("Immunefi bug-bounties status=%d", resp.status_code)
                return []
            html_text = resp.text
        finally:
            stealth.close()

        m = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html_text,
            flags=re.DOTALL,
        )
        if not m:
            log.info("Immunefi __NEXT_DATA__ missing")
            return []
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        bounties = (
            data.get("props", {})
            .get("pageProps", {})
            .get("bounties")
            or data.get("props", {}).get("pageProps", {}).get("launches")
            or []
        )

        out: List[Dict[str, Any]] = []
        for i, b in enumerate(bounties[:40]):
            bid = b.get("id") or b.get("slug") or f"imm-{i}"
            slug = b.get("slug") or b.get("project", {}).get("slug") or ""
            max_bounty = b.get("maxBounty") or b.get("maxPayout")
            if isinstance(max_bounty, str):
                # Some payloads use stringified numbers like "1000000".
                try:
                    max_bounty = int(max_bounty.replace(",", "").replace("$", ""))
                except ValueError:
                    pass
            out.append(
                self.record(
                    id=f"imm-{bid}",
                    rank=i + 1,
                    title=b.get("project", {}).get("name") or b.get("name", ""),
                    project_slug=slug,
                    category=b.get("category"),
                    severity=b.get("severity"),
                    max_payout_usd=max_bounty,
                    instructions=(b.get("instructions") or "")[:300],
                    url=f"https://immunefi.com/bug-bounty/{slug}/" if slug else "https://immunefi.com/bug-bounties/",
                )
            )
        return out


class GitcoinRoundsFetcher(BaseFetcher):
    """Gitcoin — public rounds via the Allo Protocol API.

    Endpoint: `https://gitcoin.co/grants-v2/api/rounds/` (free, no key).
    Filter by `approved=true` & `active=true`.

    Note: Gitcoin v2 grant rounds; legacy 'bounties' explorer is at
    https://gitcoin.co/explorer (Bounties / Hackathon / Micro-Tasks).
    """

    CATEGORY = "bounty"
    SOURCE = "gitcoin"

    async def fetch(self) -> List[Dict[str, Any]]:
        url = "https://gitcoin.co/grants-v2/api/rounds/"
        stealth = Stealth()
        try:
            resp = stealth.get(url, params={"active": "true", "limit": 30})
            if resp.status_code != 200:
                log.info("Gitcoin rounds status=%d", resp.status_code)
                return []
            data = resp.json()
        finally:
            stealth.close()

        items = data if isinstance(data, list) else data.get("rounds") or data.get("results") or []
        out: List[Dict[str, Any]] = []
        for i, r in enumerate(items[:30]):
            rid = r.get("id") or r.get("roundId") or f"gc-{i}"
            out.append(
                self.record(
                    id=f"gc-{rid}",
                    rank=i + 1,
                    title=r.get("name") or r.get("title") or "",
                    chain_id=r.get("chainId") or r.get("chain"),
                    match_pool_usd=r.get("matchAmount") or r.get("matchAmountUSD"),
                    start_date=r.get("roundStartTime") or r.get("startTime"),
                    end_date=r.get("roundEndTime") or r.get("endTime"),
                    status=r.get("isActive") if "isActive" in r else r.get("status"),
                    url=r.get("url") or f"https://builder.gitcoin.co/",
                )
            )
        return out


class BinanceSquareFetcher(BaseFetcher):
    """Binance Square — crypto news + airdrop tips aggregator.

    Public RSS-ish: https://www.binance.com/en/square (HTML page, no JSON API).
    The square feed is heavily JS-rendered; we extract basic info if available.
    """

    CATEGORY = "bounty"
    SOURCE = "binance_square"

    async def fetch(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            resp = stealth.get(
                "https://www.binance.com/en/square/feeds/all?category=hot&page=1",
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                log.info("Binance square status=%d", resp.status_code)
                return []
            # Some Binance APIs return JSON, others HTML.
            try:
                data = resp.json()
            except Exception:
                return []
        finally:
            stealth.close()

        items = data.get("items") or data.get("data", {}).get("list") or data.get("data", []) or []
        out: List[Dict[str, Any]] = []
        for i, post in enumerate(items[:25]):
            pid = post.get("id") or post.get("postId") or f"bs-{i}"
            out.append(
                self.record(
                    id=f"bng-sq-{pid}",
                    rank=i + 1,
                    title=post.get("title") or post.get("summary", "")[:80] or f"Post {i+1}",
                    author=post.get("authorName") or post.get("nickName"),
                    asset=post.get("assets") or post.get("topics"),
                    published=post.get("ctime") or post.get("createdAt"),
                    url=post.get("shareUrl") or f"https://www.binance.com/en/square/post/{pid}",
                )
            )
        return out
