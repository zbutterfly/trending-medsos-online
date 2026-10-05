"""Hyperliquid whale watcher — poll perp positions of a labeled watchlist.

Endpoint : POST https://api.hyperliquid.xyz/info  {"type": "clearinghouseState", "user": addr}
Free     : no auth, no documented rate limit (be polite: run on the 30-min
           scheduler cycle, or 1–5 min later via a dedicated task).

Why this matters (see docs/RISET-SIGNAL-FILTER.md): the only reproducible
public alpha in the 11 verified smart-money cases is "wallet label + position
build". Polling positions directly beats waiting for Lookonchain/Arkham posts,
which lag hours behind.

Watchlist format (backend/.env):
    HYPERLIQUID_WATCHLIST=0x082e...ca88:SMARTESTMONEY,0xabc...def:Machi

Category : signal.onchain
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import settings
from ..core.http import Plain
from .base import BaseFetcher

log = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


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
                        HL_INFO_URL,
                        json={"type": "clearinghouseState", "user": addr},
                    )
                    resp.raise_for_status()
                    state: Dict[str, Any] = resp.json()
                except Exception as e:  # noqa: BLE001
                    log.warning("hyperliquid-whales %s failed: %s", label, e)
                    continue

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
                    liq = pos.get("liquidationPx")

                    title = (
                        f"{label} {side} {_fmt_num(abs(szi))} {coin}"
                        f" | ${_fmt_num(pos_val)}"
                        + (f" @ ${_fmt_num(entry)} {_fmt_num(lev)}x" if entry and lev else "")
                        + f" | PnL {'+' if float(pnl or 0) >= 0 else ''}{_fmt_num(pnl)}"
                    )
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
                            "liquidation_px": liq,
                            "account_value": account_value,
                            "venue": "hyperliquid",
                        },
                    ))
        finally:
            await client.aclose()
        return out
