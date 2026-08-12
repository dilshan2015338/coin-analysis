import asyncio
import datetime
import httpx
import logging
from typing import List, Tuple, Dict, Any, Optional
import db

logger = logging.getLogger(__name__)

async def fetch_klines_from_api(symbol: str, start_ms: int, end_ms: int) -> List[list]:
    """
    Fetches daily 1D klines for a symbol from Binance APIs.
    Tries the Spot API first, and falls back to Futures API if spot fails or doesn't have it.
    """
    urls = [
        "https://api.binance.com/api/v3/klines",
        "https://fapi.binance.com/fapi/v1/klines"
    ]
    
    params = {
        "symbol": symbol,
        "interval": "1d",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000
    }
    
    async with httpx.AsyncClient() as client:
        for url in urls:
            try:
                response = await client.get(url, params=params, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        return data
                else:
                    logger.warning(f"Failed to fetch klines from {url} for {symbol} (status: {response.status_code})")
            except Exception as e:
                logger.error(f"Error requesting klines from {url} for {symbol}: {e}")
    return []

async def fetch_today_open_price(symbol: str) -> Optional[float]:
    """
    Fetches the open price of today's current 1D candle from Binance APIs (Spot or Futures fallback).
    Uses limit=1 to pull only the current daily candle.
    """
    urls = [
        "https://api.binance.com/api/v3/klines",
        "https://fapi.binance.com/fapi/v1/klines"
    ]
    params = {
        "symbol": symbol,
        "interval": "1d",
        "limit": 1
    }
    async with httpx.AsyncClient() as client:
        for url in urls:
            try:
                response = await client.get(url, params=params, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        return float(data[0][1])  # Index 1 is open price
            except Exception as e:
                logger.error(f"Error fetching today's open price from {url} for {symbol}: {e}")
    return None

async def initialize_historical_klines(symbol: str):
    """
    Fetches all daily klines from January 1, 2026, up to yesterday,
    inserts them into `daily_klines` table, and computes and caches average metrics in `average_metrics`.
    """
    logger.info(f"Initializing historical klines for {symbol} from January 1, 2026...")
    
    start_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    yesterday_dt = now_utc - datetime.timedelta(days=1)
    yesterday_end_dt = datetime.datetime(yesterday_dt.year, yesterday_dt.month, yesterday_dt.day, 23, 59, 59, tzinfo=datetime.timezone.utc)
    end_ms = int(yesterday_end_dt.timestamp() * 1000)
    
    yesterday_date_str = yesterday_dt.strftime("%Y-%m-%d")
    
    klines_data = await fetch_klines_from_api(symbol, start_ms, end_ms)
    if not klines_data:
        logger.warning(f"Could not fetch historical klines for {symbol}.")
        return

    records = []
    for candle in klines_data:
        open_time = candle[0]
        open_val = float(candle[1])
        high = float(candle[2])
        low = float(candle[3])
        close = float(candle[4])
        volume = float(candle[5])
        
        date_str = datetime.datetime.fromtimestamp(open_time / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        
        # We only want candles from Jan 1, 2026 up to yesterday
        if date_str >= "2026-01-01" and date_str <= yesterday_date_str:
            records.append((symbol, date_str, open_val, high, low, close, volume))
            
    if records:
        db.insert_daily_klines(symbol, records)
        logger.info(f"Inserted {len(records)} daily klines for {symbol} into DB.")
        
        # Calculate and cache average metrics
        metrics = db.recalculate_average_metrics(symbol)
        if metrics:
            avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days = metrics
            db.update_average_metrics(symbol, avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days)
            logger.info(f"Successfully updated average metrics for {symbol}. Total days: {total_days}")
    else:
        logger.warning(f"No kline records matched date range for {symbol}.")

async def catch_up_historical_klines(symbol: str):
    """
    Compares the last date stored in daily_klines for a symbol with yesterday's date.
    Fetches and inserts missing daily candles up to yesterday and updates metrics.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    yesterday_dt = now_utc - datetime.timedelta(days=1)
    yesterday_str = yesterday_dt.strftime("%Y-%m-%d")
    
    last_date = db.get_last_kline_date(symbol)
    
    if not last_date:
        # No klines stored, run initial historical download
        await initialize_historical_klines(symbol)
        return
        
    if last_date >= yesterday_str:
        logger.info(f"Daily klines for {symbol} are already up to date. Last date: {last_date}")
        return
        
    logger.info(f"Catching up klines for {symbol} (Last date: {last_date}, Yesterday: {yesterday_str})")
    
    last_dt = datetime.datetime.strptime(last_date, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    start_dt = last_dt + datetime.timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    
    yesterday_end_dt = datetime.datetime(yesterday_dt.year, yesterday_dt.month, yesterday_dt.day, 23, 59, 59, tzinfo=datetime.timezone.utc)
    end_ms = int(yesterday_end_dt.timestamp() * 1000)
    
    klines_data = await fetch_klines_from_api(symbol, start_ms, end_ms)
    if not klines_data:
        logger.warning(f"Could not fetch catch-up klines for {symbol}.")
        return

    records = []
    for candle in klines_data:
        open_time = candle[0]
        open_val = float(candle[1])
        high = float(candle[2])
        low = float(candle[3])
        close = float(candle[4])
        volume = float(candle[5])
        
        date_str = datetime.datetime.fromtimestamp(open_time / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        
        if date_str > last_date and date_str <= yesterday_str:
            records.append((symbol, date_str, open_val, high, low, close, volume))
            
    if records:
        db.insert_daily_klines(symbol, records)
        logger.info(f"Inserted {len(records)} missing daily klines for {symbol}.")
        
        # Update metrics
        metrics = db.recalculate_average_metrics(symbol)
        if metrics:
            avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days = metrics
            db.update_average_metrics(symbol, avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days)
            logger.info(f"Recalculated metrics for {symbol} after catch-up. Total days: {total_days}")

def start_midnight_scheduler(watched_coins_fetcher_fn):
    """
    Sets up and starts the APScheduler to run the daily update job at 00:01 UTC.
    watched_coins_fetcher_fn is a callable returning a list of watched coins.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler(timezone='UTC')

    async def daily_update_job():
        logger.info("Triggering automated daily midnight update at 00:01 UTC...")
        watched_coins = watched_coins_fetcher_fn()
        for coin in watched_coins:
            symbol = coin["symbol"]
            try:
                await catch_up_historical_klines(symbol)
                logger.info(f"Daily midnight update successful for {symbol}.")
            except Exception as e:
                logger.error(f"Error in daily update for {symbol}: {e}")

    scheduler.add_job(
        daily_update_job,
        CronTrigger(hour=0, minute=1, timezone='UTC'),
        name="daily_midnight_update"
    )
    scheduler.start()
    logger.info("APScheduler started: Daily midnight updates scheduled at 00:01 UTC.")
    return scheduler
