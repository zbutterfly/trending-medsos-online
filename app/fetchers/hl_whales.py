"""Hyperliquid whale watcher — poll perp positions of a labeled watchlist.

Endpoints (all free, no auth):
  POST api.hyperliquid.xyz/info {"type":"clearinghouseState","user":addr} → perp positions
  POST api.hyperliquid.xyz/info {"type":"spotClearinghouseState","user":addr} → spot balances
  POST api.hyperliquid.xyz/info {"type":"portfolio","user":addr} → PnL/account-value history

Enrichment per docs/RISET-HYPURRSCAN-WHALE-TRACKING.md (the 4 documented failure
modes of "follow the whale"):
  1. HEDGE DETECTION  — short perp + same-coin spot balance = delta-neutral carry,
     NOT a bearish signal. Flagged `hedge: true`, dampened in scoring.
  2. WALLET QUALITY   — max drawdown + down-days from 30D portfolio history.
     Lifetime PnL is survivorship bias; drawdown is the filter.

Position snapshots are appended to `position_history` by the scheduler
(run_fast_fetchers) — build-pattern scoring lives in app/signals.py.

Watchlist format (backend/.env):
    HYPERLIQUID_WATCHLIST=0x082e...ca88:SMARTESTMONEY,0xabc...def:Machi

Category : signal.onchain
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import settings
from ..core.http import Plain
from .base import BaseFetcher

log = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
QUALITY_TTL_S = 6 * 3600  # portfolio history changes slowly; refresh at most 6-hourly

# module-level portfolio cache: addr -> (fetched_at, quality_dict)
_PORTFOLIO_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}


def _fmt_num(v: Any) -> str:
    """1_380_000 -> '1.38M', 76500 -> '76.5K', 3.25 -> '3.25'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    af = abs(f)
    if af >= 1_000_000_000:
        return f"{f / 1_000_000_000:.2f}B"
    if af >= 1_000_000:
        return f"{f / 1_000_000:.2f}M"
    if af >= 1_000:
        return f"{f / 1_000:.1f}K"
    return f"{f:.2f}" if af < 10 else f"{f:.0f}"


def parse_watchlist(raw: str) -> List[Tuple[str, str]]:
    """'0xabc:LABEL,0xdef' -> [(0xabc, LABEL), (0xdef, 0xdef…last4)]."""
    out: List[Tuple[str, str]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            addr, label = part.split(":", 1)
            out.append((addr.strip(), label.strip() or addr[:6]))
        else:
            a = part
            out.append((a, f"{a[:6]}…{a[-4:]}" if len(a) > 14 else a))
    return out


def _pairs(v: Any) -> List[List[float]]:
    """HL portfolio history entries arrive as [[ts, value], ...]; normalize."""
    out: List[List[float]] = []
    for row in v or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            try:
                out.append([float(row[0]), float(row[1])])
            except (TypeError, ValueError):
                continue
    return out


def _quality_from_portfolio(port: Any) -> Optional[Dict[str, Any]]:
    """Max drawdown (30D account value) + share of negative-PnL buckets.

    HL returns either {"month": {...}, ...} or a list [["day", {...}], ["month", {...}]].
    """
    if isinstance(port, list):
        port = {row[0]: row[1] for row in port if isinstance(row, (list, tuple)) and len(row) >= 2}
    if not isinstance(port, dict):
        return None
    month = port.get("month") or {}
    av = _pairs(month.get("accountValueHistory") or month.get("accountValue"))
    pnl = _pairs(month.get("pnlHistory") or month.get("pnl"))
    if not av and not pnl:
        return None
    max_dd = 0.0
    peak = float("-inf")
    for _, v in av:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100.0)
    down = sum(1 for _, p in pnl if p < 0)
    return {
        "max_dd_30d_pct": round(max_dd, 1) if av else None,
        "down_buckets_30d": down if pnl else None,
        "total_buckets": len(pnl) if pnl else None,
        "pnl_30d_usd": round(sum(p for _, p in pnl), 2) if pnl else None,
        "samples": len(av),
    }


async def fetch_wallet_quality(client: Plain, addr: str) -> Optional[Dict[str, Any]]:
    """30D max-drawdown / down-days per wallet, cached ~6h (fast lane runs 60s)."""
    now = time.time()
    cached = _PORTFOLIO_CACHE.get(addr)
    if cached and now - cached[0] < QUALITY_TTL_S:
        return cached[1]
    quality: Optional[Dict[str, Any]] = None
    try:
        resp = await client.post(HL_INFO_URL, json={"type": "portfolio", "user": addr})
        resp.raise_for_status()
        quality = _quality_from_portfolio(resp.json() or {})
    except Exception as e:  # noqa: BLE001
        log.debug("portfolio %s failed: %s", addr[:10], e)
    _PORTFOLIO_CACHE[addr] = (now, quality)
    return quality


async def fetch_spot_balances(client: Plain, addr: str) -> Dict[str, float]:
    """Core spot balances: coin -> total. Used for hedge detection."""
    try:
        resp = await client.post(
            HL_INFO_URL, json={"type": "spotClearinghouseState", "user": addr}
        )
        resp.raise_for_status()
        state = resp.json() or {}
        out: Dict[str, float] = {}
        for b in state.get("balances") or []:
            coin = b.get("coin")
            try:
                total = float(b.get("total") or 0)
            except (TypeError, ValueError):
                continue
            if coin and total > 0:
                out[str(coin)] = total
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("spot state %s failed: %s", addr[:10], e)
        return {}


class HyperliquidWhaleWatcher(BaseFetcher):
    CATEGORY = "signal.onchain"
    SOURCE = "hyperliquid"
    FAST_LANE = True  # scheduler fast lane (60 s), see app/scheduler.py

    async def fetch(self) -> List[Dict[str, Any]]:
        watchlist = parse_watchlist(settings.hyperliquid_watchlist)
        if not watchlist:
            log.info("hyperliquid-whales: watchlist empty, skipping")
            return []

        client = Plain()
        out: List[Dict[str, Any]] = []
        try:
            for addr, label in watchlist:
                try:
                    resp = await client.post(
                        HL_INFO_URL, json={"type": "clearinghouseState", "user": addr}
                    )
                    resp.raise_for_status()
                    state: Dict[str, Any] = resp.json()
                except Exception as e:  # noqa: BLE001
                    log.warning("hyperliquid-whales %s failed: %s", label, e)
                    continue

                quality = await fetch_wallet_quality(client, addr)
                spot = await fetch_spot_balances(client, addr)

                account_value = (state.get("marginSummary") or {}).get("accountValue")
                for ap in state.get("assetPositions") or []:
                    pos = ap.get("position") or {}
                    try:
                        szi = float(pos.get("szi") or 0)
                    except (TypeError, ValueError):
                        continue
                    if szi == 0:
                        continue
                    coin = pos.get("coin") or "?"
                    side = "long" if szi > 0 else "short"
                    lev = (pos.get("leverage") or {}).get("value")
                    entry = pos.get("entryPx")
                    pos_val = pos.get("positionValue")
                    pnl = pos.get("unrealizedPnl")

                    # Hedge: short perp sambil memegang spot coin yang sama
                    # (delta-neutral carry) — BUKAN sinyal bearish.
                    spot_amt = spot.get(coin, 0.0)
                    hedge = side == "short" and spot_amt > 0

                    title = (
                        f"{label} {side} {_fmt_num(abs(szi))} {coin}"
                        f" | ${_fmt_num(pos_val)}"
                        + (f" @ ${_fmt_num(entry)} {_fmt_num(lev)}x" if entry and lev else "")
                        + f" | PnL {'+' if float(pnl or 0) >= 0 else ''}{_fmt_num(pnl)}"
                    )
                    if hedge:
                        title += " [HEDGE]"
                    if quality and quality.get("max_dd_30d_pct") is not None:
                        title += f" | DD30 {quality['max_dd_30d_pct']}%"

                    out.append(self.record(
                        # stable per (address, coin) → row is UPDATED each cycle,
                        # first_seen = when we first observed the position.
                        id=f"{addr[:10]}-{coin}",
                        title=title,
                        url=f"https://hypurrscan.io/address/{addr}",
                        extra={
                            "address": addr,
                            "label": label,
                            "coin": coin,
                            "side": side,
                            "szi": szi,
                            "entry_px": entry,
                            "position_value": pos_val,
                            "unrealized_pnl": pnl,
                            "leverage": lev,
                            "liquidation_px": pos.get("liquidationPx"),
                            "account_value": account_value,
                            "spot_balance": spot_amt,
                            "hedge": hedge,
                            "quality": quality,
                            "venue": "hyperliquid",
                        },
                    ))
        finally:
            await client.aclose()
        return out
