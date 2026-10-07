"""Pengukuran akurasi diri — hit-rate 4h/24h dari signal_log.

Sinyal kita implisit bullish, jadi "hit" = harga lebih tinggi dari harga saat
sinyal. Harga dari Binance public mirror (data-api.binance.vision), per jam.
Baru bermakna setelah signal_log terisi ≥ beberapa hari oleh scheduler/pipeline.

Dipakai bersama oleh:
- GET /api/signals/accuracy (backend app/api/routes.py)
- export signals-accuracy.json di pipeline statis (scripts/run_once.py)
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import httpx

from .db import storage

HOUR_MS = 3_600_000


def _price_at(closes: Dict[int, float], ts: int, search: int = 2) -> Optional[float]:
    """Bar jam terdekat (cari ±search jam) dari ts."""
    base = ts - ts % HOUR_MS
    for off in [0] + [s * HOUR_MS for s in range(1, search + 1)]:
        for t in ([base - off] if off == 0 else [base - off, base + off]):
            if t in closes:
                return closes[t]
    return None


async def compute_accuracy() -> Dict[str, object]:
    """Hit-rate sinyal terlog: apakah harga 4h/24h setelah sinyal naik?"""
    now_ms = int(time.time() * 1000)
    rows = await storage.list_signal_log(limit=1000)
    crypto_rows = [r for r in rows if r["venue"] == "crypto"]
    symbols = sorted({r["symbol"] for r in crypto_rows})

    closes_map: Dict[str, Dict[int, float]] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for sym in symbols[:60]:  # batasi biar cepat
            try:
                resp = await client.get(
                    "https://data-api.binance.vision/api/v3/klines",
                    params={"symbol": f"{sym}USDT", "interval": "1h", "limit": 500},
                )
                if resp.status_code != 200:
                    closes_map[sym] = {}
                    continue
                out: Dict[int, float] = {}
                for k in resp.json():
                    try:
                        out[int(k[0])] = float(k[4])  # open time → close
                    except (TypeError, ValueError, IndexError):
                        continue
                closes_map[sym] = out
            except Exception:  # noqa: BLE001
                closes_map[sym] = {}

    n4 = h4 = n24 = h24 = 0
    rets4: List[float] = []
    rets24: List[float] = []
    for r in crypto_rows:
        closes = closes_map.get(r["symbol"]) or {}
        if not closes:
            continue
        p0 = _price_at(closes, r["ts"])
        if p0 is None or p0 <= 0:
            continue
        if r["ts"] <= now_ms - 4 * HOUR_MS:
            p4 = _price_at(closes, r["ts"] + 4 * HOUR_MS)
            if p4 is not None:
                n4 += 1
                rets4.append((p4 - p0) / p0 * 100)
                if p4 > p0:
                    h4 += 1
        if r["ts"] <= now_ms - 24 * HOUR_MS:
            p24 = _price_at(closes, r["ts"] + 24 * HOUR_MS)
            if p24 is not None:
                n24 += 1
                rets24.append((p24 - p0) / p0 * 100)
                if p24 > p0:
                    h24 += 1

    def pct(n: int, d: int) -> Optional[float]:
        return round(n / d * 100, 1) if d else None

    return {
        "generated_at": now_ms,
        "logged_signals": len(rows),
        "crypto_symbols_tracked": len(symbols),
        "evaluated_4h": n4,
        "hit_rate_4h_pct": pct(h4, n4),
        "avg_ret_4h_pct": round(sum(rets4) / len(rets4), 3) if rets4 else None,
        "evaluated_24h": n24,
        "hit_rate_24h_pct": pct(h24, n24),
        "avg_ret_24h_pct": round(sum(rets24) / len(rets24), 3) if rets24 else None,
        "note": (
            "Sinyal implisit bullish (filter berita/smart-money). Hit-rate <50% "
            "bukan otomatis rugi — R:R menentukan. Butuh ±hari agar signal_log "
            "terisi cukup untuk bermakna."
        ),
    }
