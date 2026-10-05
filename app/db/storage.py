"""SQLite persistence layer — dedupe + last-seen tracking.

Uses aiosqlite for async access. Schema:
- items(category, source, source_id, payload_json, first_seen, last_seen, expired_at)

Design notes:
- A record is identified by (category, source, source_id) — composite key.
- On each fetch, existing rows get `last_seen` bumped; missing rows inserted.
- `first_seen` stays — frontend uses to highlight "new since last refresh".
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from ..core.config import settings

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[2] / "var" / "trending.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    category       TEXT NOT NULL,
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    title          TEXT,
    url            TEXT,
    payload        TEXT NOT NULL,
    first_seen     INTEGER NOT NULL,
    last_seen      INTEGER NOT NULL,
    PRIMARY KEY (category, source, source_id)
);
CREATE INDEX IF NOT EXISTS items_by_cat_last  ON items(category, last_seen DESC);
CREATE INDEX IF NOT EXISTS items_by_last      ON items(last_seen DESC);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,
    category   TEXT,
    success    INTEGER,
    count      INTEGER,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_started ON fetch_runs(started_at DESC);
"""


def _ensure_db() -> None:
    if DB_PATH.parent.exists():
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


async def init_db() -> None:
    _ensure_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _coerce_id(rec: Dict[str, Any]) -> str:
    """Pick best id from possibly different field names."""
    rid = rec.get("id") or rec.get("source_id") or rec.get("post_id") or rec.get("url")
    if rid is None:
        # Stable fallback.
        return f"{rec.get('title','')[:40]}".strip().replace("\n", " ")
    return str(rid)


async def upsert_records(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Bulk upsert. Returns counts: new, updated, total."""
    import time as _t

    now_ms = int(_t.time() * 1000)
    new_count = 0
    updated_count = 0

    if not records:
        return {"new": 0, "updated": 0, "total": 0}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        for rec in records:
            category = rec.get("category")
            source = rec.get("source")
            rid = _coerce_id(rec)
            if not category or not source:
                continue
            title = rec.get("title")
            url = rec.get("url")
            payload = json.dumps(rec, ensure_ascii=False, default=str)
            row = await db.execute_select_one_or_none(
                "SELECT 1 FROM items WHERE category=? AND source=? AND source_id=?",
                (category, source, rid),
            ) if hasattr(db, "execute_select_one_or_none") else None

            # Check existence manually.
            cursor = await db.execute(
                "SELECT 1 FROM items WHERE category=? AND source=? AND source_id=?",
                (category, source, rid),
            )
            existing = await cursor.fetchone()
            await cursor.close()

            if existing:
                await db.execute(
                    "UPDATE items SET title=?, url=?, payload=?, last_seen=? "
                    "WHERE category=? AND source=? AND source_id=?",
                    (title, url, payload, now_ms, category, source, rid),
                )
                updated_count += 1
            else:
                await db.execute(
                    "INSERT INTO items (category, source, source_id, title, url, payload, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (category, source, rid, title, url, payload, now_ms, now_ms),
                )
                new_count += 1
        await db.commit()
    return {"new": new_count, "updated": updated_count, "total": len(records)}


async def list_items(
    *,
    category: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
    include_expired: bool = False,
) -> List[Dict[str, Any]]:
    sql = "SELECT payload FROM items WHERE 1=1"
    params: List[Any] = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()

    return [json.loads(r[0]) for r in rows]


async def list_categories() -> List[Dict[str, Any]]:
    sql = (
        "SELECT category, COUNT(*) as n, MAX(last_seen) as last_seen, MAX(first_seen) as first_seen "
        "FROM items GROUP BY category ORDER BY n DESC"
    )
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        await cursor.close()
    return [
        {"category": r[0], "count": r[1], "last_seen": r[2], "first_seen": r[3]}
        for r in rows
    ]


async def record_run(started_at: int, ended_at: int, category: Optional[str], success: bool, count: int, error: Optional[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO fetch_runs (started_at, ended_at, category, success, count, error)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (started_at, ended_at, category, 1 if success else 0, count, error),
        )
        await db.commit()


async def latest_runs(limit: int = 20) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, started_at, ended_at, category, success, count, error "
            "FROM fetch_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [
        {
            "id": r[0],
            "started_at": r[1],
            "ended_at": r[2],
            "category": r[3],
            "success": bool(r[4]),
            "count": r[5],
            "error": r[6],
        }
        for r in rows
    ]
