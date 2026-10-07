"""Satu siklus lengkap untuk pipeline statis (GitHub Actions / lokal):

  fetch semua sumber -> upsert SQLite -> skor sinyal -> export JSON statis
  -> (opsional) upload ke HF Space statis.

Pemakaian:
  python scripts/run_once.py              # + upload bila HF_TOKEN ada
  NO_UPLOAD=1 python scripts/run_once.py  # hanya generate data lokal

Dipanggil oleh .github/workflows/refresh.yml tiap 10 menit.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Muat kredensial lokal (backend/.env) bila ada — di Actions, env vars dari
# Secrets yang diutamakan (setdefault tidak menimpa yang sudah ada).
_local_env = ROOT.parent / "backend" / ".env"
if _local_env.exists():
    for line in _local_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from app.db import storage                      # noqa: E402
from app.fetchers.base import epoch_ms, gather_all  # noqa: E402
from app.fetchers.registry import FETCHER_GROUPS, all_fetchers  # noqa: E402
from app.signals import compute_signals         # noqa: E402
from app.accuracy import compute_accuracy       # noqa: E402

SITE = ROOT / "static-site"
DATA = SITE / "data"
HF_REPO = os.environ.get("HF_SPACE_REPO", "Jetbard/trending-medsos")
# Data JSON dipublikasikan ke repo dataset bernama acak (UI Space tetap privat).
DATA_REPO = os.environ.get("HF_DATA_REPO", "Jetbard/tmx-feed-g9nh5j")
CATEGORIES = sorted({c for g in FETCHER_GROUPS for c in g["categories"]})


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


async def main() -> None:
    started = epoch_ms()
    await storage.init_db()
    fetchers = all_fetchers()
    records = await gather_all(fetchers)
    counts = await storage.upsert_records(records)
    print(f"[run] fetchers={len(fetchers)} total={counts['total']} "
          f"new={counts['new']} updated={counts['updated']}")

    # Jendela sinyal 72h — item lebih tua dari 8 hari tak terpakai lagi;
    # pangkas agar SQLite (cache antar-run Actions) tidak membengkak.
    # signal_log & position_history TIDAK dipangkas (bahan akurasi + build).
    pruned = await storage.prune_items(started - 8 * 86_400_000)
    print(f"[run] prune item >8 hari: {pruned} baris")

    signals = await compute_signals()
    accuracy = await compute_accuracy()
    ended = epoch_ms()

    # health.json — bentuk sama dengan GET /api/health
    _write(DATA / "health.json", {
        "fetchers": [
            {"category": f.CATEGORY, "source": f.SOURCE, "cls": type(f).__name__}
            for f in fetchers
        ],
        "last_run": {
            "started_at": started, "ended_at": ended,
            "new": counts["new"], "updated": counts["updated"],
            "fetchers": len(fetchers),
        },
        "recent_runs": [],
        "groups": FETCHER_GROUPS,
    })

    # category-stats.json — bentuk sama dengan GET /api/category-stats
    cats = await storage.list_categories()
    _write(DATA / "category-stats.json", {"categories": cats, "groups": FETCHER_GROUPS})

    # items/<category>.json — bentuk sama dengan GET /api/items?category=...
    for cat in CATEGORIES + ["_all"]:
        items = await storage.list_items(
            category=None if cat == "_all" else cat, limit=200)
        _write(DATA / "items" / f"{cat}.json", {"count": len(items), "items": items})

    # signals.json — ranked symbols (sama dengan GET /api/signals)
    _write(DATA / "signals.json", signals)

    # signals-accuracy.json — hit-rate 4h/24h dari signal_log
    # (sama dengan GET /api/signals/accuracy di backend API)
    _write(DATA / "signals-accuracy.json", accuracy)

    n_files = sum(1 for _ in DATA.rglob("*.json"))
    print(f"[run] {n_files} file JSON ditulis ke {DATA}")

    if os.environ.get("NO_UPLOAD") == "1":
        print("[run] NO_UPLOAD=1 — selesai tanpa upload.")
        return

    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print("[run] HF_TOKEN tidak ada — data lokal saja, skip upload.")
        return
    from huggingface_hub import HfApi
    api = HfApi(token=token)

    # 1) Data JSON -> repo dataset publik (nama acak; di-fetch langsung browser).
    try:
        api.create_repo(DATA_REPO, repo_type="dataset", private=False, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        print(f"[run] create dataset repo skip: {e}")
    api.upload_folder(
        folder_path=str(DATA), repo_id=DATA_REPO, repo_type="dataset",
        commit_message=f"data refresh {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
    )
    print(f"[run] data ter-publish ke dataset {DATA_REPO}")

    # 2) Situs (tanpa data) -> Space privat.
    api.upload_folder(
        folder_path=str(SITE), repo_id=HF_REPO, repo_type="space",
        commit_message=f"site refresh {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        ignore_patterns=["data/**", "data/*"],
    )
    print(f"[run] situs ter-upload ke Space {HF_REPO}")


if __name__ == "__main__":
    asyncio.run(main())
