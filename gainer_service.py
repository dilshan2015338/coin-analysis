import os
import httpx
import logging
import datetime
from typing import List, Dict, Any, Optional

import db

logger = logging.getLogger(__name__)

# Parse target chat ID
TARGET_CHAT_ID_RAW = os.getenv("TARGET_CHAT_ID")
def parse_chat_id(value: str) -> Any:
    if not value:
        return ""
    value_str = value.strip()
    try:
        return int(value_str)
    except ValueError:
        return value_str

TARGET_CHAT_ID = parse_chat_id(TARGET_CHAT_ID_RAW)

def format_volume(vol: float) -> str:
    """Formats dollar volumes cleanly (e.g. $5.82M or $120.50K)."""
    if vol >= 1_000_000_000.0:
        return f"${vol / 1_000_000_000.0:,.2f}B"
    elif vol >= 1_000_000.0:
        return f"${vol / 1_000_000.0:,.2f}M"
    elif vol >= 1_000.0:
        return f"${vol / 1_000.0:,.2f}K"
    else:
        return f"${vol:,.2f}"

def format_price(price: float) -> str:
    """Formats coin prices cleanly."""
    if price >= 1.0:
        return f"${price:,.2f}"
    else:
        return f"${price:,.6f}"

async def fetch_24h_tickers() -> List[dict]:
    """Queries the Binance Spot API for rolling 24-hour ticker statistics."""
    url = "https://api.binance.com/api/v3/ticker/24hr"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=15.0)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning(f"Binance 24h ticker response code: {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching 24h tickers from Binance: {e}")
    return []

def filter_gainers(tickers: List[dict], min_percent: float) -> List[dict]:
    """
    Filters ticker lists for USDT trading pairs that meet minimum percentage 
    and daily quote volume (> $100k USDT) requirements.
    """
    gainers = []
    for t in tickers:
        symbol = t.get("symbol", "")
        # Filter for USDT pairs
        if not symbol.endswith("USDT"):
            continue
            
        try:
            pct_change = float(t.get("priceChangePercent", 0.0))
            quote_volume = float(t.get("quoteVolume", 0.0))
            
            # Enforce 50% gain threshold (or configurable threshold) and liquid volume > $100k USDT
            if pct_change >= min_percent and quote_volume >= 100000.0:
                gainers.append({
                    "symbol": symbol,
                    "lastPrice": float(t.get("lastPrice", 0.0)),
                    "priceChangePercent": pct_change,
                    "volume": float(t.get("volume", 0.0)),
                    "quoteVolume": quote_volume,
                    "highPrice": float(t.get("highPrice", 0.0)),
                    "lowPrice": float(t.get("lowPrice", 0.0))
                })
        except (ValueError, TypeError):
            continue
            
    # Sort descending by change percentage
    gainers.sort(key=lambda x: x["priceChangePercent"], reverse=True)
    return gainers

def get_milestone_tier(pct: float, threshold: float) -> int:
    """
    Groups gain percentages into milestone tiers above the threshold:
    - Tier 1: threshold <= pct < 75
    - Tier 2: 75 <= pct < 100
    - Tier 3: 100 <= pct < 150
    - Tier 4: 150 <= pct < 200
    - Tier 5+: Step of 50% increment thereafter
    """
    if pct < threshold:
        return 0
    if pct < 75.0:
        return 1
    elif pct < 100.0:
        return 2
    elif pct < 150.0:
        return 3
    else:
        return 4 + int((pct - 150.0) // 50.0)

async def run_gainer_scanner(application: Any):
    """
    Periodic background job executing every 5 minutes to scan USDT market tickers 
    for major daily pump milestones.
    """
    # 1. Check if scanner is enabled
    enabled = db.get_setting("gainer_scanner_enabled", "1")
    if enabled != "1":
        return

    # 2. Get active alert threshold
    try:
        threshold = float(db.get_setting("gainer_threshold", "50.0"))
    except ValueError:
        threshold = 50.0

    logger.info(f"Running market-wide 24h pump scanner (threshold: {threshold}%)...")
    
    tickers = await fetch_24h_tickers()
    if not tickers:
        logger.warning("No tickers returned from Binance 24h endpoint.")
        return

    gainers = filter_gainers(tickers, threshold)
    if not gainers:
        return

    now = datetime.datetime.now(datetime.timezone.utc)

    for g in gainers:
        symbol = g["symbol"]
        current_pct = g["priceChangePercent"]
        current_price = g["lastPrice"]
        high_24h = g["highPrice"]
        low_24h = g["lowPrice"]
        volume_24h = g["quoteVolume"]

        # Check last alert history in DB
        last_alert = db.get_last_gainer_alert(symbol)
        should_alert = False

        if not last_alert:
            should_alert = True
        else:
            try:
                alerted_at = datetime.datetime.fromisoformat(last_alert["alerted_at"])
                if alerted_at.tzinfo is None:
                    alerted_at = alerted_at.replace(tzinfo=datetime.timezone.utc)
                
                hours_since = (now - alerted_at).total_seconds() / 3600.0
                
                if hours_since >= 4.0:
                    should_alert = True
                else:
                    # Within 4 hours, alert only if the coin crosses into a higher milestone tier
                    last_tier = get_milestone_tier(last_alert["price_change_pct"], threshold)
                    current_tier = get_milestone_tier(current_pct, threshold)
                    if current_tier > last_tier:
                        should_alert = True
                        logger.info(f"{symbol} reached higher pump tier: Tier {last_tier} -> Tier {current_tier}")
            except Exception as e:
                logger.error(f"Error checking gainer cooldown for {symbol}: {e}")
                should_alert = True

        if should_alert:
            # 3. Log alert in database to trigger cooldown tracker
            db.insert_gainer_alert(symbol, current_pct, current_price)

            # 4. Dispatch alert to Target Channel
            msg = (
                f"🚨 *PUMP ALERT: 24h Gain Exceeded {threshold:.0f}%* 🚨\n\n"
                f"• *Symbol*: {symbol}\n"
                f"• *24h Change*: +{current_pct:.1f}%\n"
                f"• *Current Price*: {format_price(current_price)}\n"
                f"• *24h High / Low*: {format_price(high_24h)} / {format_price(low_24h)}\n"
                f"• *24h Volume*: {format_volume(volume_24h)} USDT"
            )
            try:
                await application.bot.send_message(
                    chat_id=TARGET_CHAT_ID,
                    text=msg,
                    parse_mode="Markdown"
                )
                logger.info(f"Sent 24h pump alert to Telegram for {symbol} (+{current_pct:.1f}%)")
            except Exception as e:
                logger.error(f"Failed to send pump alert for {symbol} to Telegram: {e}")
