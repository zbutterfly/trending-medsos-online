"""GeckoTerminal new-pool watcher — deteksi coin/token BARU di DEX (free, keyless).

Endpoint : GET https://api.geckoterminal.com/api/v2/networks/eth/new_pools
           ?networks=eth,solana,base,bsc,arbitrum&include=base_token,dex
Verified : 2026-10-06 (200; multi-network via query param `networks`).

Kenapa penting: token baru tidak ada di direktori CoinGecko/CMC — pool DEX adalah
sinyal paling awal yang bisa didapat gratis. TAPI konteksnya brutal: studi yang
beredar menyebut mayoritas token baru DEX berakhir rug. Maka filter anti-rug dasar
wajib (likuiditas & volume minimum) — ini FILTER AWARENESS, bukan rekomendasi beli.

Category : signal.newtoken
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from ..core.http import Plain
from ..signals import STABLECOINS
from .base import BaseFetcher

log = logging.getLogger(__name__)

GT_URL = "https://api.geckoterminal.com/api/v2/networks/eth/new_pools"
NETWORKS = "eth,solana,base,bsc,arbitrum"
MAX_AGE_H = 72.0        # hanya pool berusia ≤ 3 hari
MIN_LIQUIDITY_USD = 15_000.0   # di bawah ini = buang (rugs & saluran spam)
MIN_VOLUME_24H_USD = 10_000.0

# Slug URL web DEXscreener ≠ id network GeckoTerminal. DEXscreener memakai
# nama chain PENUH (verifikasi 9 Okt 2026 via api.dexscreener.com — field url
# pair resmi = dexscreener.com/ethereum/0x…; id 'eth' ditolak HTTP 400),
# sedangkan web GeckoTerminal sendiri pakai id pendek (…/eth/pools/0x… → 200).
DEXSCREENER_SLUGS = {
    "eth": "ethereum",
    "solana": "solana",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "polygon_pos": "polygon",
    "avax": "avalanche",
    "optimism": "optimism",
    "sui": "sui",
    "ton": "ton",
}

# Stablecoin = bukan objek sinyal "akan naik". Tiga lapis: simbol ticker
# (STABLECOINS di signals.py), coingecko_coin_id (paling andal — pool USDT
# 0x83cbee… lolos dengan simbol "TETHER", tapi coin_id-nya "tether"), nama.
STABLECOIN_CG_IDS = {
    "tether", "usd-coin", "dai", "first-digital-usd", "true-usd",
    "binance-usd", "frax", "usds", "usdd", "paypal-usd", "bridged-usdc-3",
}
STABLECOIN_NAMES = {
    "tether", "usd coin", "dai", "first digital usd", "trueusd", "true usd",
    "frax", "usds",
}
# Detektor word-boundary gabungan (simbol+nama+coin_id) — menangkap pool
# stablecoin PALASU (contoh riil 9 Okt: eth_0x83cbee ber-simbol 'Tether',
# nama 'USDT', alamat ≠ kontrak USDT asli, FDV palsu $89 M) sekaligus yang
# asli — keduanya bukan objek sinyal "akan naik". Word-boundary mencegah
# false-positive seperti 'DAILY' mengandung 'dai'.
_STABLE_RE = re.compile(
    r"\b(usdt|usdc|tether|usd\s?coin|dai|fdusd|first\s?digital|true\s?usd"
    r"|tusd|frax|usds|usde|pyusd|usdd)\b",
    re.I,
)

SOURCE_WEIGHT_HINT = 0.5  # lihat signals.py SOURCE_WEIGHTS["geckoterminal"]


class NewTokenPoolsFetcher(BaseFetcher):
    CATEGORY = "signal.newtoken"
    SOURCE = "geckoterminal"
    FAST_LANE = True

    def __init__(self, networks: str = NETWORKS) -> None:
        self.networks = networks

    async def fetch(self) -> List[Dict[str, Any]]:
        client = Plain()
        out: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        try:
            resp = await client.get(
                GT_URL,
                params={"networks": self.networks, "include": "base_token,dex"},
            )
            resp.raise_for_status()
            payload = resp.json() or {}
        except Exception as e:  # noqa: BLE001
            log.warning("geckoterminal new_pools failed: %s", e)
            return []
        finally:
            await client.aclose()

        # included: id -> (type, attributes)
        inc: Dict[str, Dict[str, Any]] = {}
        for row in payload.get("included") or []:
            rid = row.get("id")
            if rid:
                inc[rid] = {"type": row.get("type"), "attrs": row.get("attributes") or {}}

        for pool in payload.get("data") or []:
            attrs = pool.get("attributes") or {}
            rel = (pool.get("relationships") or {})
            created = attrs.get("pool_created_at")
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_h = (now - created_dt).total_seconds() / 3600.0
            except (AttributeError, ValueError, TypeError):
                continue
            if age_h < 0 or age_h > MAX_AGE_H:
                continue

            base_rel = ((rel.get("base_token") or {}).get("data") or {}).get("id")
            base_inc = inc.get(base_rel) or {}
            base_attrs = base_inc.get("attrs") or {}
            symbol = (base_attrs.get("symbol") or "").strip().upper()
            if not symbol or len(symbol) > 12:
                continue  # token tanpa simbol layak = buang
            cg_id = str(base_attrs.get("coingecko_coin_id") or "").strip().lower()
            base_name = str(base_attrs.get("name") or "").strip().lower()
            if (symbol in STABLECOINS or cg_id in STABLECOIN_CG_IDS
                    or base_name in STABLECOIN_NAMES
                    or _STABLE_RE.search(f"{symbol} {base_name} {cg_id}")):
                continue  # pool stablecoin asli ATAU palasu (impersonasi) — buang

            dex_rel = ((rel.get("dex") or {}).get("data") or {}).get("id")
            dex_name = (inc.get(dex_rel) or {}).get("attrs", {}).get("name") or "DEX"

            try:
                liquidity = float(attrs.get("reserve_in_usd") or 0)
                vol24 = float((attrs.get("volume_usd") or {}).get("h24") or 0)
            except (TypeError, ValueError):
                continue
            if liquidity < MIN_LIQUIDITY_USD or vol24 < MIN_VOLUME_24H_USD:
                continue  # anti-rug dasar: likuiditas & volume minimum

            fdv = attrs.get("fdv_usd")
            tx = attrs.get("transactions") or {}
            network = pool.get("id", "_").split("_", 1)[0]
            pool_addr = attrs.get("address") or ""
            token_addr = base_attrs.get("address") or ""
            # URL yang benar di web DEXscreener: slug penuh + alamat POOL
            # (bukan alamat token). Lihat catatan DEXSCREENER_SLUGS di atas.
            net_slug = DEXSCREENER_SLUGS.get(network, network)

            def _usd(v: Any) -> str:
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return "?"
                return f"{f/1e9:.1f}B" if f >= 1e9 else (
                    f"{f/1e6:.1f}M" if f >= 1e6 else f"{f/1e3:.0f}K")

            title = (
                f"{symbol} — pool baru di {dex_name} ({net_slug})"
                f" | Liq ${_usd(liquidity)} | Vol24 ${_usd(vol24)}"
                + (f" | FDV ${_usd(fdv)}" if fdv else "")
                + f" | usia pool {age_h:.0f} jam"
            )
            out.append(self.record(
                id=str(pool.get("id") or pool_addr),
                title=title,
                url=f"https://dexscreener.com/{net_slug}/{pool_addr}",
                extra={
                    "coins": [symbol],  # entity resolution via extract_symbols
                    "symbol": symbol,
                    "network": network,
                    "dex": dex_name,
                    "age_h": round(age_h, 1),
                    "liquidity_usd": liquidity,
                    "volume_24h_usd": vol24,
                    "fdv_usd": float(fdv) if fdv else None,
                    "tx_24h": tx.get("h24"),
                    "token_address": token_addr,
                    "pool_address": pool_addr,
                    "dexscreener_url": f"https://dexscreener.com/{net_slug}/{pool_addr}",
                    "gecko_pool_url": f"https://www.geckoterminal.com/{network}/pools/{pool_addr}",
                    "goplus_url": (
                        f"https://gopluslabs.io/token-security/{token_addr}"
                        if network in ("eth", "base", "bsc", "arbitrum") and token_addr else None
                    ),
                    "rugcheck_url": (
                        f"https://rugcheck.xyz/tokens/{token_addr}" if network == "solana" and token_addr else None
                    ),
                    "venue": "crypto",
                },
            ))
        log.info("geckoterminal: %d pool baru lolos filter", len(out))
        return out
