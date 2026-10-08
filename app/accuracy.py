"""Pengukuran akurasi diri — hit-rate 4h/24h dari signal_log.

Dua lapis metrik (riset 10/10 bab 1):
1. ABSOLUT: harga 4h/24h setelah sinyal naik? (pembanding lama — menyesatkan
   saat pasar naik, tapi dipertahankan untuk kontinuitas data).
2. EXCESS (utama): sinyal hanya "benar" bila MENGALAHKAN baseline pasarnya —
   crypto vs BTC (binance.vision, kolom btc_px), IDX vs IHSG (Yahoo ^JKSE,
   kolom ihsg_px). Baseline px tersimpan SAAT sinyal dibuat (bebas look-ahead);
   harga baseline di t+4h/t+24h dicari dari seri 1-jam (±2 jam terdekat).

Harga simbol: Binance public mirror (data-api.binance.vision) — WAJIB header
User-Agent browser (tanpa UA → 403 CloudFront; temuan riset 9 Okt 2026).
Dipakai bersama GET /api/signals/accuracy (backend) dan export
signals-accuracy.json (pipeline statis, scripts/run_once.py).
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import httpx

from .baselines import UA, fetch_ihsg_series
from .db import storage

HOUR_MS = 3_600_000
BINANCE_VISION = "https://data-api.binance.vision/api/v3/klines"


def _price_at(closes: Dict[int, float], ts: int, search: int = 2) -> Optional[float]:
    """Bar jam terdekat (cari ±search jam) dari ts."""
    base = ts - ts % HOUR_MS
    for off in [0] + [s * HOUR_MS for s in range(1, search + 1)]:
        for t in ([base - off] if off == 0 else [base - off, base + off]):
            if t in closes:
                return closes[t]
    return None


async def _fetch_klines(client: httpx.AsyncClient, symbol: str) -> Dict[int, float]:
    try:
        r = await client.get(
            BINANCE_VISION,
            params={"symbol": f"{symbol}USDT", "interval": "1h", "limit": 500},
            headers={"User-Agent": UA},
        )
        if r.status_code != 200:
            return {}
        out: Dict[int, float] = {}
        for k in r.json():
            try:
                out[int(k[0])] = float(k[4])  # open time → close
            except (TypeError, ValueError, IndexError):
                continue
        return out
    except Exception:  # noqa: BLE001
        return {}


async def compute_accuracy() -> Dict[str, object]:
    """Hit-rate sinyal terlog: absolut + excess vs baseline (utama)."""
    now_ms = int(time.time() * 1000)
    rows = await storage.list_signal_log(limit=1000)
    crypto_rows = [r for r in rows if r["venue"] == "crypto"]
    idx_rows = [r for r in rows if r["venue"] == "idx"]
    symbols = sorted({r["symbol"] for r in crypto_rows})

    async with httpx.AsyncClient(timeout=15.0) as client:
        closes_map: Dict[str, Dict[int, float]] = {}
        # BTC selalu dibutuhkan sbg baseline crypto (mungkin juga simbol sinyal)
        for sym in list(dict.fromkeys(symbols[:60] + ["BTC"])):
            closes_map[sym] = await _fetch_klines(client, sym)
        ihsg_closes = await fetch_ihsg_series(client, days=5)

    def _baseline_of(r: dict):
        """(px_saat_sinyal, seri_harga) sesuai venue — None bila baseline tak tersedia."""
        if r["venue"] == "crypto":
            return r.get("btc_px"), closes_map.get("BTC") or {}
        return r.get("ihsg_px"), ihsg_closes

    n4 = h4 = n24 = h24 = 0
    rets4: List[float] = []
    rets24: List[float] = []
    nx4 = hx4 = nx24 = hx24 = 0
    exs4: List[float] = []
    exs24: List[float] = []

    for r in crypto_rows + idx_rows:
        closes = closes_map.get(r["symbol"]) or {}
        if r["venue"] != "crypto" or not closes:
            # IDX: tak ada seri per-saham gratis — hanya excess vs IHSG.
            closes = {}
        b_px0, b_series = _baseline_of(r)
        p0 = _price_at(closes, r["ts"]) if closes else None
        # P0 excess pakai harga SIMBOL saat sinyal bila serinya ada;
        # bila tidak (venue idx), excess tak bisa dihitung per-saham → skip.
        if p0 is None or p0 <= 0:
            # fallback: pakai baseline tks sbg proxy? tidak — biarkan absolut
            # menghitung venue idx nanti bila seri simbol tersedia.
            continue
        if r["ts"] <= now_ms - 4 * HOUR_MS:
            p4 = _price_at(closes, r["ts"] + 4 * HOUR_MS)
            if p4 is not None:
                n4 += 1
                ret = (p4 - p0) / p0 * 100
                rets4.append(ret)
                if p4 > p0:
                    h4 += 1
                # excess (utama) — hanya bila baseline tersedia dua titik
                if b_px0:
                    b4 = _price_at(b_series, r["ts"] + 4 * HOUR_MS)
                    if b4:
                        ex = ret - (b4 - b_px0) / b_px0 * 100
                        exs4.append(ex)
                        nx4 += 1
                        if ex > 0:
                            hx4 += 1
        if r["ts"] <= now_ms - 24 * HOUR_MS:
            p24 = _price_at(closes, r["ts"] + 24 * HOUR_MS)
            if p24 is not None:
                n24 += 1
                ret = (p24 - p0) / p0 * 100
                rets24.append(ret)
                if p24 > p0:
                    h24 += 1
                if b_px0:
                    b24 = _price_at(b_series, r["ts"] + 24 * HOUR_MS)
                    if b24:
                        ex = ret - (b24 - b_px0) / b_px0 * 100
                        exs24.append(ex)
                        nx24 += 1
                        if ex > 0:
                            hx24 += 1

    def pct(n: int, d: int) -> Optional[float]:
        return round(n / d * 100, 1) if d else None

    def avg(xs: List[float]) -> Optional[float]:
        return round(sum(xs) / len(xs), 3) if xs else None

    return {
        "generated_at": now_ms,
        "logged_signals": len(rows),
        "crypto_symbols_tracked": len(symbols),
        "evaluated_4h": n4,
        "hit_rate_4h_pct": pct(h4, n4),
        "avg_ret_4h_pct": avg(rets4),
        "evaluated_24h": n24,
        "hit_rate_24h_pct": pct(h24, n24),
        "avg_ret_24h_pct": avg(rets24),
        # EXCESS vs baseline — metrik utama (sinyal harus MENGALAHKAN pasar)
        "evaluated_excess_4h": nx4,
        "hit_rate_excess_4h_pct": pct(hx4, nx4),
        "avg_excess_4h_pct": avg(exs4),
        "evaluated_excess_24h": nx24,
        "hit_rate_excess_24h_pct": pct(hx24, nx24),
        "avg_excess_24h_pct": avg(exs24),
        "note": (
            "Metrik utama = EXCESS: sinyal dianggap 'benar' hanya bila MENGALAHKAN "
            "baseline (crypto vs BTC, IDX vs IHSG). Hit-rate absolut <50% bukan "
            "otomatis rugi — R:R menentukan. Baris venue idx tanpa seri harga "
            "gratis hanya dinilai bila baseline tersedia."
        ),
    }
