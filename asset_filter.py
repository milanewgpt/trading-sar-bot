"""
Fetches intersection of CoinGecko top-100 and BingX perpetual futures.
Result is cached to assets_rsi.json and refreshed every 24h.
"""

import json
import logging
import time
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

CACHE_FILE = Path("assets_rsi.json")
CACHE_TTL = 86400  # 24h in seconds
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

# Stablecoins and wrapped tokens to skip
EXCLUDE = {
    "usdt", "usdc", "busd", "dai", "tusd", "usdp", "frax", "gusd",
    "usde", "fdusd", "pyusd", "wbtc", "weth", "steth", "wsteth",
    "cbbtc", "btcb", "weeth",
}


async def _fetch_top100_symbols() -> list[str]:
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "sparkline": "false",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(COINGECKO_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
    return [
        c["symbol"].upper()
        for c in data
        if c.get("symbol", "").lower() not in EXCLUDE
    ]


async def _fetch_bingx_symbols(bingx_client) -> set[str]:
    data = await bingx_client._get("/openApi/swap/v2/quote/contracts")
    contracts = data.get("data", [])
    return {c["symbol"] for c in contracts if isinstance(c.get("symbol"), str)}


async def get_rsi_assets(bingx_client) -> list[dict]:
    """
    Returns asset list for RSI strategy.
    Reads from cache if fresh, otherwise fetches and saves.
    """
    # Return cache if still fresh
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        if time.time() - cached.get("ts", 0) < CACHE_TTL:
            assets = cached["assets"]
            log.info("[RSI] loaded %d assets from cache", len(assets))
            return assets

    log.info("[RSI] fetching asset list (CoinGecko + BingX)…")
    try:
        top100 = await _fetch_top100_symbols()
        bingx_symbols = await _fetch_bingx_symbols(bingx_client)

        assets = []
        for sym in top100:
            bingx_sym = f"{sym}-USDT"
            if bingx_sym in bingx_symbols:
                assets.append({
                    "symbol": bingx_sym,
                    "key": sym.lower(),
                    "label": f"{sym}USDT",
                })

        CACHE_FILE.write_text(json.dumps({"ts": time.time(), "assets": assets}, indent=2))
        log.info("[RSI] asset list: %d symbols", len(assets))
        for a in assets:
            log.info("  %s", a["symbol"])
        return assets

    except Exception as e:
        log.error("[RSI] failed to fetch asset list: %s", e)
        # Fall back to cache even if stale
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())["assets"]
        # Last resort: hardcoded fallback
        return [
            {"symbol": "ETH-USDT",  "key": "eth",  "label": "ETHUSDT"},
            {"symbol": "SOL-USDT",  "key": "sol",  "label": "SOLUSDT"},
            {"symbol": "HYPE-USDT", "key": "hype", "label": "HYPEUSDT"},
        ]
