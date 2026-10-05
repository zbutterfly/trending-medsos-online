"""Shared base for fetchers + small helpers.

Each fetcher follows this pattern:
  class XFetcher(BaseFetcher):
      CATEGORY = "crypto.trending"
      async def fetch(self) -> list[dict]: ...

Return records with at minimum:
  {
    "category": str,
    "source":   str,
    "id":       str,        # dedupe key (stable)
    "title":    str,        # what to show
    "url":      str,        # link
    "fetched_at": int,      # epoch ms
    "extra":    dict,        # source-specific fields
  }
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def epoch_ms() -> int:
    return int(time.time() * 1000)


class BaseFetcher(ABC):
    CATEGORY: str = "base"
    SOURCE: str = "unknown"

    @abstractmethod
    async def fetch(self) -> List[Dict[str, Any]]:
        ...

    def record(self, **kw: Any) -> Dict[str, Any]:
        rec = {
            "category": self.CATEGORY,
            "source": self.SOURCE,
            "fetched_at": epoch_ms(),
        }
        rec.update(kw)
        return rec

    async def safe_fetch(self) -> List[Dict[str, Any]]:
        """Run fetch with timeout + exception guard. Returns [] on failure."""
        try:
            res = await asyncio.wait_for(self.fetch(), timeout=60.0)
            log.info("%s/%s OK %d records", self.CATEGORY, self.SOURCE, len(res))
            return res
        except asyncio.TimeoutError:
            log.warning("%s/%s timeout", self.CATEGORY, self.SOURCE)
        except Exception as e:  # noqa: BLE001
            log.error("%s/%s FAILED: %s", self.CATEGORY, self.SOURCE, e)
        return []


async def gather_all(fetchers: List[BaseFetcher]) -> List[Dict[str, Any]]:
    """Run all fetchers concurrently and flatten results.

    Use this from the scheduler / API layer.
    """
    out: List[Dict[str, Any]] = []
    results = await asyncio.gather(*[f.safe_fetch() for f in fetchers], return_exceptions=False)
    for batch in results:
        if batch:
            out.extend(batch)
    return out
