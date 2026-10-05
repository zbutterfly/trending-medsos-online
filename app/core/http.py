"""HTTP client factory using curl_cffi for TLS/JA3 fingerprint impersonation.

curl_cffi is the user's preferred stealth layer (see user memory
`free-google-search-toolkit.md`). We use it for any source that may be bot-
sensitive (aggregator sites, social scrapers, RSS). Use httpx for plain JSON
APIs (CoinGecko) where stealth is unnecessary.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from curl_cffi import requests as cffi_requests

from .config import settings

log = logging.getLogger(__name__)


def _default_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


class Stealth:
    """curl_cffi-backed stealth HTTP GET/POST wrapper.

    `impersonate` defaults to settings.curl_impersonate (chrome131).
    Retain session for cookie reuse where helpful.
    """

    def __init__(self, impersonate: Optional[str] = None) -> None:
        self.impersonate = impersonate or settings.curl_impersonate
        self.session = cffi_requests.Session(impersonate=self.impersonate)

    def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> cffi_requests.Response:
        h = _default_headers()
        if headers:
            h.update(headers)
        log.debug("stealth GET %s params=%s", url, params)
        return self.session.get(url, params=params, headers=h, timeout=timeout)

    def post(
        self,
        url: str,
        *,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> cffi_requests.Response:
        h = _default_headers()
        if headers:
            h.update(headers)
        return self.session.post(url, json=json, data=data, headers=h, timeout=timeout)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


class Plain:
    """httpx-based plain HTTP client for trusted JSON APIs (CoinGecko etc).

    Use this when there's no risk of bot-blocking — it's lighter than curl_cffi
    and supports proper connection pooling / http2.
    """

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers=_default_headers(),
            follow_redirects=True,
        )

    async def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        return await self.client.get(url, params=params, headers=headers)

    async def post(
        self,
        url: str,
        *,
        json: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        return await self.client.post(url, json=json, headers=headers)

    async def aclose(self) -> None:
        await self.client.aclose()


# === Synchronous curl_cffi convenience (for fetchers that prefer sync) ===
def stealth_get(url: str, **kwargs: Any) -> cffi_requests.Response:
    """One-shot stealth GET. Use Stealth class for repeated calls."""
    impersonate = kwargs.pop("impersonate", settings.curl_impersonate)
    return cffi_requests.get(url, impersonate=impersonate, headers=_default_headers(), **kwargs)
