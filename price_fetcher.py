import asyncio
import httpx
import logging
from typing import List, Dict, Tuple, Optional, Any

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

# ---------------------------------------------------------
# Market Type Resolver (Spot vs Futures)
# ---------------------------------------------------------

_SPOT_SYMBOLS: set = set()
_FUTURES_SYMBOLS: set = set()
_CACHE_TIMESTAMP: float = 0.0
_CACHE_TTL: float = 900.0  # 15 minutes
_CACHE_LOCK: Optional[asyncio.Lock] = None

def _get_cache_lock() -> asyncio.Lock:
    global _CACHE_LOCK
    if _CACHE_LOCK is None:
        _CACHE_LOCK = asyncio.Lock()
    return _CACHE_LOCK

async def refresh_market_cache(force: bool = False) -> None:
    """
    Refreshes the cached sets of Binance Spot and Futures tickers every 15 minutes.
    """
    import time
    global _SPOT_SYMBOLS, _FUTURES_SYMBOLS, _CACHE_TIMESTAMP

    now = time.time()
    if not force and _SPOT_SYMBOLS and (now - _CACHE_TIMESTAMP < _CACHE_TTL):
        return

    lock = _get_cache_lock()
    async with lock:
        now = time.time()
        if not force and _SPOT_SYMBOLS and (now - _CACHE_TIMESTAMP < _CACHE_TTL):
            return

        futures_url = "https://fapi.binance.com/fapi/v1/ticker/price"
        spot_url = "https://api.binance.com/api/v3/ticker/price"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                f_task = client.get(futures_url)
                s_task = client.get(spot_url)
                f_res, s_res = await asyncio.gather(f_task, s_task, return_exceptions=True)

                if not isinstance(s_res, Exception) and s_res.status_code == 200:
                    _SPOT_SYMBOLS = {item["symbol"] for item in s_res.json() if "symbol" in item}
                
                if not isinstance(f_res, Exception) and f_res.status_code == 200:
                    _FUTURES_SYMBOLS = {item["symbol"] for item in f_res.json() if "symbol" in item}

                _CACHE_TIMESTAMP = time.time()
                logger.info(f"Market cache refreshed: {len(_SPOT_SYMBOLS)} Spot symbols, {len(_FUTURES_SYMBOLS)} Futures symbols.")
        except Exception as e:
            logger.warning(f"Failed to refresh market cache: {e}")

async def get_market_type(symbol: str) -> str:
    """
    Determines whether the cryptocurrency symbol is traded on Binance Spot, Binance Futures, or both.
    Returns 'Spot & Futures', 'Futures', 'Spot', or 'Spot' default.
    """
    sym = resolve_symbol(symbol).upper()
    if not sym:
        return "Spot"

    await refresh_market_cache()

    in_spot = sym in _SPOT_SYMBOLS
    in_fut = sym in _FUTURES_SYMBOLS

    if in_spot and in_fut:
        return "Spot & Futures"
    elif in_fut:
        return "Futures"
    elif in_spot:
        return "Spot"
    return "Spot"

def get_market_type_sync(symbol: str) -> str:
    """
    Synchronous fallback to determine market type from current cache.
    If cache is empty, attempts a fast one-time sync fetch.
    """
    import time
    sym = resolve_symbol(symbol).upper()
    if not sym:
        return "Spot"

    global _SPOT_SYMBOLS, _FUTURES_SYMBOLS, _CACHE_TIMESTAMP
    if not _SPOT_SYMBOLS and not _FUTURES_SYMBOLS:
        try:
            with httpx.Client(timeout=4.0) as client:
                r_s = client.get("https://api.binance.com/api/v3/ticker/price")
                r_f = client.get("https://fapi.binance.com/fapi/v1/ticker/price")
                if r_s.status_code == 200:
                    _SPOT_SYMBOLS = {item["symbol"] for item in r_s.json() if "symbol" in item}
                if r_f.status_code == 200:
                    _FUTURES_SYMBOLS = {item["symbol"] for item in r_f.json() if "symbol" in item}
                _CACHE_TIMESTAMP = time.time()
        except Exception as e:
            logger.warning(f"Sync fallback market type lookup failed for {sym}: {e}")

    in_spot = sym in _SPOT_SYMBOLS
    in_fut = sym in _FUTURES_SYMBOLS

    if in_spot and in_fut:
        return "Spot & Futures"
    elif in_fut:
        return "Futures"
    elif in_spot:
        return "Spot"
    return "Spot"

async def is_futures_symbol(symbol: str) -> bool:
    """
    Returns True if the cryptocurrency symbol has active Futures contracts on Binance
    (either as 'Futures' only or 'Spot & Futures').
    """
    sym = resolve_symbol(symbol).upper()
    if not sym:
        return False
    await refresh_market_cache()
    return sym in _FUTURES_SYMBOLS

def is_futures_symbol_sync(symbol: str) -> bool:
    """
    Synchronous check whether a symbol is traded on Binance Futures.
    """
    sym = resolve_symbol(symbol).upper()
    if not sym:
        return False
    return sym in _FUTURES_SYMBOLS

async def fetch_24h_ticker(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Fetches 24-hour ticker statistics for a symbol, querying both Binance Futures and Spot endpoints.
    Returns normalized dictionary with lastPrice, highPrice, lowPrice, priceChangePercent, and quoteVolume.
    """
    sym = resolve_symbol(symbol).upper()
    if not sym:
        return None

    futures_url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}"
    spot_url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        f_task = client.get(futures_url)
        s_task = client.get(spot_url)
        f_res, s_res = await asyncio.gather(f_task, s_task, return_exceptions=True)

    # Prioritize Futures if available, otherwise use Spot
    if not isinstance(f_res, Exception) and f_res.status_code == 200:
        data = f_res.json()
        try:
            return {
                "symbol": sym,
                "priceChangePercent": float(data.get("priceChangePercent", 0.0)),
                "lastPrice": float(data.get("lastPrice", 0.0)),
                "highPrice": float(data.get("highPrice", 0.0)),
                "lowPrice": float(data.get("lowPrice", 0.0)),
                "quoteVolume": float(data.get("quoteVolume", 0.0)),
                "source": "futures"
            }
        except (ValueError, TypeError):
            pass

    if not isinstance(s_res, Exception) and s_res.status_code == 200:
        data = s_res.json()
        try:
            return {
                "symbol": sym,
                "priceChangePercent": float(data.get("priceChangePercent", 0.0)),
                "lastPrice": float(data.get("lastPrice", 0.0)),
                "highPrice": float(data.get("highPrice", 0.0)),
                "lowPrice": float(data.get("lowPrice", 0.0)),
                "quoteVolume": float(data.get("quoteVolume", 0.0)),
                "source": "spot"
            }
        except (ValueError, TypeError):
            pass

    return None

