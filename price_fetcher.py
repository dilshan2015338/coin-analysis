import asyncio
import httpx
import logging
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)

# Typical quote assets used in crypto pairings
QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "BTC", "ETH", "EUR", "TRY", "FDUSD")

def resolve_symbol(user_symbol: str) -> str:
    """
    Standardizes user-provided cryptocurrency symbols.
    - Strips spaces and slashes/dashes (e.g., BTC/USDT or BTC-USDT -> BTCUSDT).
    - If no quote asset is found at the end, appends 'USDT' (e.g., BTC -> BTCUSDT).
    """
    sym = user_symbol.strip().upper().replace("/", "").replace("-", "")
    if not sym:
        return ""
    for suffix in QUOTE_SUFFIXES:
        if sym.endswith(suffix) and len(sym) > len(suffix):
            return sym
    return f"{sym}USDT"

async def fetch_ticker_data(client: httpx.AsyncClient, url: str) -> List[Dict[str, str]]:
    """Fetches and parses a price ticker endpoint."""
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.warning(f"Error fetching ticker from {url}: {e}")
        return []

async def fetch_prices(resolved_symbols: List[str]) -> Dict[str, float]:
    """
    Fetches the current price for a list of resolved symbols.
    Queries both Binance Spot and Futures endpoints concurrently to resolve both types of assets.
    Returns a dictionary of symbol -> price.
    """
    if not resolved_symbols:
        return {}

    futures_url = "https://fapi.binance.com/fapi/v1/ticker/price"
    spot_url = "https://api.binance.com/api/v3/ticker/price"

    async with httpx.AsyncClient() as client:
        futures_task = fetch_ticker_data(client, futures_url)
        spot_task = fetch_ticker_data(client, spot_url)
        futures_data, spot_data = await asyncio.gather(futures_task, spot_task)

    price_map: Dict[str, float] = {}

    # Process Spot data first
    for item in spot_data:
        sym = item.get("symbol")
        val = item.get("price")
        if sym and val:
            try:
                price_map[sym] = float(val)
            except ValueError:
                pass

    # Process Futures data (prioritize futures prices if there is overlap/hedging)
    for item in futures_data:
        sym = item.get("symbol")
        val = item.get("price")
        if sym and val:
            try:
                price_map[sym] = float(val)
            except ValueError:
                pass

    # Filter for the requested symbols
    result: Dict[str, float] = {}
    for sym in resolved_symbols:
        if sym in price_map:
            result[sym] = price_map[sym]
        else:
            logger.warning(f"Price for symbol {sym} could not be found on Binance Spot or Futures.")

    return result
