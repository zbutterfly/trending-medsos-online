"""Emerging chains fetcher — MegaETH, Monad, Abstract Chain.

Each chain has different public telemetry. Strategy:
1. Check RPC `eth_blockNumber` to confirm testnet alive.
2. Pull a project wiki / dev docs page (GitHub org activity) for ecosystem
   news: new contracts, integrations, faucets, incentives.
3. Probe faucet endpoints for free token eligibility.

Configure custom RPCs via .env.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..core.config import settings
from ..core.http import Stealth
from .base import BaseFetcher, epoch_ms

log = logging.getLogger(__name__)


CHAIN_REGISTRY = [
    {
        "name": "MegaETH",
        "slug": "megaeth",
        "rpc": settings.megaeth_rpc,
        "website": "https://megaeth.com",
        "docs": "https://docs.megaeth.com",
        "faucet": "https://carrot.megaeth.com/faucet",
        "github_org": "megaeth-xyz",
        "twitter": "mega_eth",
        "description": "Real-time EVM-compatible L2 with sub-block transaction confirmation.",
    },
    {
        "name": "Monad",
        "slug": "monad",
        "rpc": settings.monad_rpc,
        "website": "https://monad.xyz",
        "docs": "https://docs.monad.xyz",
        "faucet": "https://faucet.monad.xyz",
        "github_org": "monad-labs",
        "twitter": "monad_xyz",
        "description": "High-performance L1 EVM-compatible. ~10k TPS throughput target.",
    },
    {
        "name": "Abstract Chain",
        "slug": "abstract",
        "rpc": settings.abstract_rpc,
        "website": "https://abs.xyz",
        "docs": "https://docs.abs.xyz",
        "faucet": "https://faucet.abs.xyz",
        "github_org": "absorg",
        "twitter": "abs_xyz",
        "description": "Consumer crypto chain led by the Pudgy Penguins team.",
    },
]


async def _rpc_block_number(rpc_url: str, timeout: int = 10) -> Optional[int]:
    """POST a JSON-RPC `eth_blockNumber` over stealth HTTP. None if RPC dead."""
    import asyncio

    def _do() -> Optional[int]:
        stealth = Stealth()
        try:
            payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
            resp = stealth.post(rpc_url, json=payload, timeout=timeout)
            if resp.status_code != 200:
                return None
            try:
                body = resp.json()
            except Exception:
                return None
            if "result" not in body:
                return None
            # eth_blockNumber returns hex string.
            hex_block = body["result"]
            if isinstance(hex_block, str) and hex_block.startswith("0x"):
                return int(hex_block, 16)
        except Exception as e:  # noqa: BLE001
            log.debug(" RPC %s failed: %s", rpc_url, e)
            return None
        finally:
            try:
                stealth.close()
            except Exception:
                pass
        return None

    return await asyncio.get_running_loop().run_in_executor(None, _do)


class EmergingChainStatusFetcher(BaseFetcher):
    """Probe status of each registered chain: block height, RPC liveness.

    Returns one record per chain with optional latest block + status string.
    """

    CATEGORY = "emerging_chain.status"
    SOURCE = "rpc-probe"

    async def fetch(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for chain in CHAIN_REGISTRY:
            block = await _rpc_block_number(chain["rpc"])
            out.append(
                self.record(
                    id=f"chain-st-{chain['slug']}",
                    name=chain["name"],
                    slug=chain["slug"],
                    rpc_url=chain["rpc"],
                    latest_block=block,
                    alive=(block is not None),
                    website=chain["website"],
                    docs=chain["docs"],
                    faucet=chain["faucet"],
                    github_org=chain["github_org"],
                    twitter_handle=chain["twitter"],
                    description=chain["description"],
                )
            )
        return out


class EmergingChainNewsFetcher(BaseFetcher):
    """GitHub activity per chain org → recent repositories / releases.

    GitHub API is rate-limited (60 req/hr unauth) but public; we cache.
    OpenAPI: https://api.github.com/orgs/{org}/repos?sort=pushed.
    """

    CATEGORY = "emerging_chain.news"
    SOURCE = "github-org"

    async def fetch(self) -> List[Dict[str, Any]]:
        from selectolax.parser import HTMLParser  # noqa: F401 — not used but lint-friendly.

        import httpx

        out: List[Dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            for chain in CHAIN_REGISTRY:
                org = chain["github_org"]
                if not org:
                    continue
                try:
                    resp = await client.get(
                        f"https://api.github.com/orgs/{org}/repos",
                        params={"sort": "pushed", "per_page": 5},
                        headers={"Accept": "application/vnd.github+json"},
                    )
                    if resp.status_code != 200:
                        continue
                    repos = resp.json()
                except Exception as e:  # noqa: BLE001
                    log.warning(" github orgs/%s failed: %s", org, e)
                    continue

                for r in repos[:5]:
                    out.append(
                        self.record(
                            id=f"gh-{chain['slug']}-{r.get('name', '')}",
                            chain=chain["name"],
                            chain_slug=chain["slug"],
                            repo=r.get("name"),
                            description=(r.get("description") or "")[:200],
                            stars=r.get("stargazers_count"),
                            language=r.get("language"),
                            pushed_at=r.get("pushed_at"),
                            homepage=r.get("homepage") or "",
                            repo_url=r.get("html_url"),
                        )
                    )
        return out


class EmergingChainTwitterFetcher(BaseFetcher):
    """Optional: fetch latest chain-project tweets via nitter.

    Falls back to nothing if nitter is dead (very likely 2026).
    """

    CATEGORY = "emerging_chain.social"
    SOURCE = "nitter-chain"

    async def fetch(self) -> List[Dict[str, Any]]:
        from .sentiment import NitterTrendingFetcher

        out: List[Dict[str, Any]] = []
        for chain in CHAIN_REGISTRY:
            handle = chain.get("twitter")
            if not handle:
                continue
            # Search by handle (without @) for recent posts.
            nf = NitterTrendingFetcher(keywords=[f"@{handle}"], limit_per_keyword=3)
            try:
                items = await nf.fetch()
            except Exception as e:  # noqa: BLE001
                log.warning("nitter chain %s failed: %s", handle, e)
                items = []
            for it in items:
                it["chain"] = chain["name"]
                it["chain_slug"] = chain["slug"]
                out.append(it)
        return out
