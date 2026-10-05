"""Optional HTTP Basic Auth — protects a deployed instance (HF Space dsb.).

Enable by setting AUTH_PASSWORD (and optionally AUTH_USERNAME, default "admin")
in the environment. Unset = fully open (local development stays friction-free).

Note for same-origin SPA: the browser asks for credentials once via the native
Basic-auth prompt (triggered by the first 401), then attaches them to all
same-origin requests automatically — including the dashboard's fetch() calls.
"""
from __future__ import annotations

import base64
import hmac
from typing import Optional

from starlette.types import ASGIApp, Receive, Scope, Send


class BasicAuthMiddleware:
    """ASGI middleware checking the Authorization: Basic header."""

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        self.app = app
        self.expected = (
            "Basic "
            + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        ).encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only guard HTTP; skip CORS preflight (browsers never send auth there).
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = {k: v for k, v in scope.get("headers") or []}
        authz = headers.get(b"authorization", b"")
        if authz and hmac.compare_digest(authz, self.expected):
            await self.app(scope, receive, send)
            return

        body = b'{"detail":"Unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Basic realm="trending-medsos", charset="UTF-8"'),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def make_auth_middleware(app: ASGIApp, username: Optional[str], password: Optional[str]) -> ASGIApp:
    """Wrap `app` with BasicAuthMiddleware only if a password is configured."""
    if not password:
        return app
    return BasicAuthMiddleware(app, username=username or "admin", password=password)
