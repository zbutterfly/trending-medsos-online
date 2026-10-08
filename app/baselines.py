"""Baseline harga untuk label excess-return sinyal (riset 10/10 bab 1, rekomendasi #3).

Hit-rate absolut menyesatkan saat pasar naik — sinyal hanya dianggap "benar"
bila MENGALAHKAN baseline pasarnya (crypto → BTC, IDX → IHSG). Harga baseline
DISIMPAN saat sinyal dibuat (kolom signal_log.btc_px / ihsg_px) supaya
evaluasi nanti bebas look-ahead.

Sumber (diverifikasi live 9 Okt 2026):
- BTC  : data-api.binance.vision 1h klines — keyless, TIDAK kena blokir ISP
  (berbeda dgn fapi.binance.com); WAJIB header User-Agent browser
  (tanpa UA → 403 CloudFront).
- IHSG : Yahoo Finance chart ^JKSE interval 1h — keyless (query1 & query2 200,
  13 bar). Timestamp Yahoo = detik (konversi ke ms).
- Gagal fetch = None → pipeline TIDAK pernah mati karena baseline; baris venue
  itu dinilai absolut sementara.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import httpx

log = logging.getLogger(__name__)

BINANCE_VISION = "https://data-api.binance.vision/api/v3/klines"
YAHOO_JKSE = "https://query1.finance.yahoo.com/v8/finance/chart/%5EJKSE"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def fetch_btc_price(client: httpx.AsyncClient) -> Optional[float]:
    """Close bar 1-jam terakhir BTCUSDT."""
    try:
        r = await client.get(
            BINANCE_VISION,
            params={"symbol": "BTCUSDT", "interval": "1h", "limit": 3},
            headers={"User-Agent": UA},
        )
        if r.status_code == 200:
            rows = r.json()
            if rows:
                return float(rows[-1][4])
    except Exception as e:  # noqa: BLE001
        log.debug("baseline BTC gagal: %s", e)
    return None


async def fetch_ihsg_price(client: httpx.AsyncClient) -> Optional[float]:
    """Close bar 1-jam terakhir ^JKSE (Yahoo, keyless)."""
    try:
        r = await client.get(
            YAHOO_JKSE,
            params={"interval": "1h", "range": "2d"},
            headers={"User-Agent": UA},
        )
        if r.status_code == 200:
            res = (r.json().get("chart", {}).get("result") or [None])[0]
            if res:
                closes = (
                    ((res.get("indicators", {}).get("quote") or [{}])[0]).get("close")
                    or []
                )
                for c in reversed(closes):
                    if c:
                        return float(c)
    except Exception as e:  # noqa: BLE001
        log.debug("baseline IHSG gagal: %s", e)
    return None


async def fetch_ihsg_series(client: httpx.AsyncClient, days: int = 5) -> Dict[int, float]:
    """Riwayat ^JKSE 1-jam (utk evaluasi excess di accuracy): {ts_ms: close}.

    Yahoo hanya punya bar selama jam bursa — evaluator mencari bar terdekat ±2 jam.
    """
    try:
        r = await client.get(
            YAHOO_JKSE,
            params={"interval": "1h", "range": f"{days}d"},
            headers={"User-Agent": UA},
        )
        if r.status_code == 200:
            res = (r.json().get("chart", {}).get("result") or [None])[0]
            if res:
                ts = res.get("timestamp") or []
                closes = (
                    ((res.get("indicators", {}).get("quote") or [{}])[0]).get("close")
                    or []
                )
                return {int(t) * 1000: float(c) for t, c in zip(ts, closes) if c}
    except Exception as e:  # noqa: BLE001
        log.debug("riwayat IHSG gagal: %s", e)
    return {}


async def fetch_baselines() -> Dict[str, Optional[float]]:
    """Sekali per siklus: {"BTC": px|None, "IHSG": px|None}."""
    out: Dict[str, Optional[float]] = {"BTC": None, "IHSG": None}
    async with httpx.AsyncClient(timeout=12.0) as client:
        out["BTC"] = await fetch_btc_price(client)
        out["IHSG"] = await fetch_ihsg_price(client)
    return out
