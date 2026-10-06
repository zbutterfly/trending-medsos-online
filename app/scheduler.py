"""Background scheduler using APScheduler.

Runs all fetchers every `FETCH_INTERVAL_MIN` minutes. Manual trigger available
via the /refresh endpoint for instant updates from the UI.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .core.config import settings
from .db import storage
from .fetchers.registry import all_fetchers

log = logging.getLogger(__name__)


_scheduler: Optional[AsyncIOScheduler] = None
_last_run_summary: dict = {}


async def run_all_fetchers(triggered_by: str = "scheduler") -> dict:
    """Run all fetchers concurrently, upsert records, log per-fetcher outcome."""
    from .fetchers.base import gather_all, epoch_ms

    started = epoch_ms()
    log.info("FETCH RUN triggered by %s", triggered_by)
    fetchers = [f for f in all_fetchers() if not getattr(f, "FAST_LANE", False)]
    try:
        all_records = await gather_all(fetchers)
    except Exception as e:  # noqa: BLE001
        log.error("Fetch run failed: %s", e)
        _last_run_summary["error"] = str(e)
        await storage.record_run(
            started_at=started,
            ended_at=epoch_ms(),
            category="*",
            success=False,
            count=0,
            error=str(e),
        )
        return {"error": str(e)}

    counts = await storage.upsert_records(all_records)
    full_counts = {
        "total": counts["total"],
        "new": counts["new"],
        "updated": counts["updated"],
        "fetchers": len(fetchers),
        "started_at": started,
        "ended_at": epoch_ms(),
    }
    _last_run_summary.update(full_counts)
    await storage.record_run(
        started_at=started,
        ended_at=epoch_ms(),
        category="*",
        success=True,
        count=counts["total"],
        error=None,
    )
    return full_counts


async def run_fast_fetchers(triggered_by: str = "scheduler-fast") -> dict:
    """Fast lane — only wire/on-chain fetchers (Tree News, Hyperliquid whales,
    IQPlus) every `FAST_INTERVAL_SEC` seconds. Not recorded in fetch_runs
    (would add ~1440 rows/day); summary kept in memory + logs.
    """
    from .fetchers.base import gather_all, epoch_ms

    started = epoch_ms()
    fetchers = [f for f in all_fetchers() if getattr(f, "FAST_LANE", False)]
    try:
        records = await gather_all(fetchers)
    except Exception as e:  # noqa: BLE001
        log.error("Fast fetch run failed: %s", e)
        return {"error": str(e)}
    counts = await storage.upsert_records(records)

    # Snapshot posisi whale → position_history (bahan deteksi pola build di signals.py).
    now = epoch_ms()
    hist_rows = []
    for rec in records:
        extra = rec.get("extra") or {}
        if rec.get("source") == "hyperliquid" and extra.get("address"):
            try:
                hist_rows.append({
                    "ts": now,
                    "address": str(extra["address"]),
                    "coin": str(extra["coin"]),
                    "szi": float(extra.get("szi") or 0),
                    "entry_px": (
                        float(extra["entry_px"]) if extra.get("entry_px") is not None else None
                    ),
                })
            except (TypeError, ValueError):
                continue
    if hist_rows:
        try:
            await storage.append_position_history(hist_rows)
        except Exception as e:  # noqa: BLE001
            log.warning("position_history append failed: %s", e)

    summary = {
        "fast": {
            "total": counts["total"], "new": counts["new"],
            "updated": counts["updated"], "fetchers": len(fetchers),
            "triggered_by": triggered_by, "ended_at": epoch_ms(),
        }
    }
    _last_run_summary.update(summary)
    log.info(
        "FAST RUN %s: %d records (new=%d upd=%d)",
        triggered_by, counts["total"], counts["new"], counts["updated"],
    )
    return summary["fast"]


def get_last_run() -> dict:
    return dict(_last_run_summary)


async def start_scheduler() -> AsyncIOScheduler:
    """Start the APScheduler background timers.

    Two lanes:
    - slow lane: all non-FAST_LANE fetchers every FETCH_INTERVAL_MIN minutes
    - fast lane : FAST_LANE fetchers every FAST_INTERVAL_SEC seconds
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    try:
        _scheduler = AsyncIOScheduler()
        # Slow lane — full scrape.
        _scheduler.add_job(
            run_all_fetchers,
            trigger="interval",
            minutes=settings.fetch_interval_min,
            id="fetch-all",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Fast lane — signals wire + on-chain.
        _scheduler.add_job(
            run_fast_fetchers,
            trigger="interval",
            seconds=settings.fast_interval_sec,
            id="fetch-fast",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        log.info(
            "Scheduler started (slow=%d min, fast=%d s)",
            settings.fetch_interval_min, settings.fast_interval_sec,
        )
    except Exception as e:
        log.error("Failed to start scheduler: %s", e)
        _scheduler = None
    return _scheduler


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def refresh_now() -> dict:
    """Public API for manual refresh — runs fetchers immediately."""
    # Schedule an extra one-off task.
    return await run_all_fetchers(triggered_by="manual")
