"""FastAPI application entrypoint.

Run locally with: `uvicorn app.main:app --reload --port 8000`.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router as trending_router
from .core.auth import make_auth_middleware
from .core.config import settings
from .core.logging import setup_logging
from .db import storage
from .scheduler import refresh_now, start_scheduler, stop_scheduler

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("TRENDING-MEDSOS backend booting")
    try:
        await storage.init_db()
        await refresh_now()  # warm cache on startup
        await start_scheduler()
    except Exception as e:
        log.error("Startup error: %s", e)
    try:
        yield
    finally:
        try:
            await stop_scheduler()
        except Exception:
            pass
        log.info("Backend shutdown")


app = FastAPI(
    title="TRENDING-MEDSOS API",
    description="Crypto trending + airdrop + bounty + IDX news aggregator.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow Vite dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Vite default
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # Vite on port 4173 (Windows excluded port range 5119-5218 covers 5173)
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trending_router)


def _find_static_dir() -> Optional[Path]:
    """Serve the built frontend (single-service deploy) if it exists.

    Priority: ./static (HF Space layout) → ../frontend/dist (local dev build).
    """
    candidates = [
        Path.cwd() / "static",
        Path(__file__).resolve().parents[2] / "frontend" / "dist",
    ]
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return None


_static_dir = _find_static_dir()
if _static_dir:
    # Mounted AFTER the API router, so /api/* keeps precedence.
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
    log.info("Serving frontend from %s", _static_dir)


@app.get("/")
async def root():
    return {"name": "TRENDING-MEDSOS API", "version": "0.1.0"}


# Optional deployment lock — set AUTH_PASSWORD (e.g. HF Space secret) to enable.
# Applied last (after every route is registered) and replaces the app object.
app = make_auth_middleware(app, settings.auth_username, settings.auth_password)  # type: ignore[assignment]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=settings.app_env == "development",
    )
