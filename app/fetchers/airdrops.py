"""Airdrop aggregators scraper bundle — multi-source discovery.

Sources (free, no API key; all require TLS-impersonation via curl_cffi):
- airdrops.io            — most popular manual list
- daoslug                — DAO-driven airdrop tracker
- Layer3.xyz /quests     — quest-based airdrops
- Galxe.com/dashboard    — campaign-driven airdrops
- DefiLlama /airdrops    — JSON API (best — completely free)

We DO NOT scrape social media posts for airdrops; that's done by sentiment.
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


class DefiLlamaAirdropFetcher(BaseFetcher):
    """DefiLlama exposes `airdrops.yaml` — programmatic, free, the canonical source.

    Endpoint: https://defillama.com/airdrops (page) / raw: Not JSON.
    Actually DefiLlama's data is at https://defillama.com/static/airdrops.json — but
    historically they're data is in YAML. To be safe we hit the page and parse JSON
    out of <script id="__NEXT_DATA__"> tags if present; otherwise fallback to tables.

    Since DefiLlama doesn't have a documented airdrops JSON endpoint we grab from
    a community-mirrored endpoint below.
    """

    CATEGORY = "airdrop"
    SOURCE = "defillama"

    async def fetch(self) -> List[Dict[str, Any]]:
        # DefiLlama airdrops page (server-side rendered React, embedded JSON).
        stealth = Stealth()
        try:
            resp = stealth.get("https://defillama.com/airdrops")
            if resp.status_code != 200:
                log.info("DefiLlama /airdrops status=%d", resp.status_code)
                return []
            html_text = resp.text
        finally:
            stealth.close()

        # Find the embedded __NEXT_DATA__ JSON.
        m = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html_text,
            flags=re.DOTALL,
        )
        if not m:
            log.info("DefiLlama __NEXT_DATA__ not found")
            return []

        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        airdrops = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("airdrops")
            or next_data.get("props", {}).get("pageProps", {}).get("data")
            or []
        )
        if not isinstance(airdrops, list):
            return []

        out: List[Dict[str, Any]] = []
        for i, item in enumerate(airdrops[:50]):
            aid = item.get("id") or item.get("name") or f"dl-{i}"
            out.append(
                self.record(
                    id=f"dl-air-{aid}",
                    rank=i + 1,
                    title=item.get("name", ""),
                    symbol=item.get("symbol", "").upper(),
                    chain=item.get("chain"),
                    status=item.get("status"),
                    start_date=item.get("startDate") or item.get("start_date"),
                    end_date=item.get("endDate") or item.get("end_date"),
                    tvl_usd=item.get("tvl"),
                    participants=item.get("participants"),
                    url=item.get("href") or item.get("url") or f"https://defillama.com/airdrops",
                )
            )
        return out


class AirdropsIoFetcher(BaseFetcher):
    """Scrape airdrops.io latest active airdrops.

    Site layout: table on homepage, .box-row entries per airdrop.
    Field structure: name, status (Active/Upcoming/Ended), date, link.
    """

    CATEGORY = "airdrop"
    SOURCE = "airdrops.io"

    async def fetch(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            resp = stealth.get("https://airdrops.io/")
            if resp.status_code != 200:
                return []
            tree = HTMLParser(resp.text)
        finally:
            stealth.close()

        out: List[Dict[str, Any]] = []
        # New layout: article.card elements inside main loop.
        nodes = tree.css("article, .box-row, .airdrop-row")
        for i, node in enumerate(nodes[:30]):
            title_el = node.css_first("h2, .title, h3") or node.css_first("a")
            link_el = node.css_first("a[href]")
            anchor_text = (title_el.text(strip=True) if title_el else "") or (link_el.text(strip=True) if link_el else "")
            href = link_el.attributes.get("href") if link_el else None
            if not anchor_text or not href:
                continue
            # Status detection from CSS class.
            classes = node.attributes.get("class", "") or ""
            if "ended" in classes.lower():
                status = "Ended"
            elif "upcoming" in classes.lower() or "upcoming" in classes:
                status = "Upcoming"
            else:
                status = "Active"
            out.append(
                self.record(
                    id=f"ad-io-{i}-{href[-32:]}" if href else f"ad-io-{i}",
                    rank=i + 1,
                    title=anchor_text,
                    status=status,
                    url=href,
                )
            )
        return out


class Layer3Fetcher(BaseFetcher):
    """Layer3.xyz quests — quests offer token rewards + many lead to airdrops.

    Page: https://layer3.xyz/quests  — JS-heavy SPA, server-rendered skeleton.
    Use the task-listing endpoint (public, undocumented).
    """

    CATEGORY = "airdrop"
    SOURCE = "layer3"

    async def fetch(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            # Try the public logged-out quest discovery page.
            resp = stealth.get(
                "https://layer3.xyz/quests",
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            if resp.status_code != 200:
                return []
            html_text = resp.text
        finally:
            stealth.close()

        # Extract quest data from __NEXT_DATA__ if present.
        m = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*type="application/json"[^>]*>(.*?)</script>',
            html_text,
            flags=re.DOTALL,
        )
        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        quests = (
            data.get("props", {}).get("pageProps", {}).get("quests")
            or data.get("props", {}).get("pageProps", {}).get("data", {}).get("quests")
            or []
        )
        if not isinstance(quests, list):
            return []

        out: List[Dict[str, Any]] = []
        for i, q in enumerate(quests[:30]):
            qid = q.get("id") or q.get("slug") or f"l3-{i}"
            slug = q.get("slug") or q.get("id") or ""
            out.append(
                self.record(
                    id=f"l3-q-{qid}",
                    rank=i + 1,
                    title=q.get("name") or q.get("title") or "",
                    description=(q.get("description") or "")[:200],
                    status=q.get("status") or "active",
                    rewards=q.get("rewardsSummary") or q.get("rewards"),
                    deadline=q.get("deadline") or q.get("endDate"),
                    url=f"https://layer3.xyz/quests/{slug}" if slug else "https://layer3.xyz/quests",
                )
            )
        return out


class GalxeCampaignFetcher(BaseFetcher):
    """Galxe.com campaign discovery.

    Public but heavy login-wall on campaigns page. We hit the REST endpoint
    `https://api.galxe.com/api/v1/campaigns/all_campaigns` (open CORS) for a
    list of active public campaigns offering token-airdrop rewards.

    Note: This endpoint is undocumented and may change. Guard against 401/403.
    """

    CATEGORY = "airdrop"
    SOURCE = "galxe"

    async def fetch(self) -> List[Dict[str, Any]]:
        stealth = Stealth()
        try:
            url = "https://api.galxe.com/api/v1/campaigns/all_campaigns"
            params = {"page": 1, "size": 30, "status": "ongoing"}
            resp = stealth.get(url, params=params)
            if resp.status_code != 200:
                log.info("Galxe campaigns status=%d", resp.status_code)
                return []
            data = resp.json()
        finally:
            stealth.close()

        items = data.get("data", {}).get("list", []) or data.get("data", [])
        out: List[Dict[str, Any]] = []
        for i, c in enumerate(items[:30]):
            cid = c.get("id") or c.get("campaignId") or f"gal-{i}"
            out.append(
                self.record(
                    id=f"gal-{cid}",
                    rank=i + 1,
                    title=c.get("title") or c.get("name") or "",
                    chain=c.get("chain"),
                    status=c.get("status") or "ongoing",
                    participants=c.get("participants") or c.get("participantCount"),
                    start_date=c.get("startDate") or c.get("startTime"),
                    end_date=c.get("endDate") or c.get("endTime"),
                    url=c.get("url") or f"https://galxe.com/campaign/{cid}",
                )
            )
        return out
