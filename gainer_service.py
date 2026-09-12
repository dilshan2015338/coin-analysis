import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import httpx
import logging
import datetime
import html
from typing import List, Dict, Any, Optional

import db
import chart_service
import card_service

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
    """Formats coin prices cleanly based on magnitude."""
    if price is None:
        return "$0.00"
    if price >= 1000.0:
        return f"${price:,.2f}"
    elif price >= 1.0:
        return f"${price:,.4f}".rstrip('0').rstrip('.') if f"${price:,.4f}".endswith('0') and not f"${price:,.2f}".endswith('.00') else f"${price:,.2f}"
    elif price >= 0.01:
        return f"${price:.4f}"
    elif price >= 0.0001:
        return f"${price:.6f}"
    else:
        return f"${price:.8f}"

def format_pump_alert(
    symbol: str,
    current_pct: float,
    current_price: Optional[float] = None,
    high_24h: Optional[float] = None,
    low_24h: Optional[float] = None,
    volume_24h: Optional[float] = None,
    multiplier: Optional[float] = None,
    alert_id: Optional[int] = None,
    market_type: Optional[str] = None
) -> str:
    """
    Formats a sleek, creative Telegram alert caption highlighting:
    - CryptoPulse VIP Bot
    - PUMP or DUMP ALERT
    - Currency pair
    - Market type (Spot / Futures / Spot & Futures)
    - 24h Change
    """
    esc_sym = html.escape(symbol)
    if not market_type:
        try:
            import price_fetcher
            market_type = price_fetcher.get_market_type_sync(symbol)
        except Exception:
            market_type = "Spot"

    esc_market = html.escape(market_type)
    if current_pct < 0:
        pct_str = f"{current_pct:.2f}%"
        return (
            f"⚡ <b>CryptoPulse VIP Bot</b>\n"
            f"<blockquote>🔻 <b>DUMP ALERT: #{esc_sym}</b> ➔ <code>{pct_str}</code> 🔴\n"
            f"🏷️ <b>Market:</b> <code>{esc_market}</code></blockquote>"
        )
    else:
        pct_str = f"+{current_pct:.2f}%"
        return (
            f"⚡ <b>CryptoPulse VIP Bot</b>\n"
            f"<blockquote>🚨 <b>PUMP ALERT: #{esc_sym}</b> ➔ <code>{pct_str}</code> 🟢\n"
            f"🏷️ <b>Market:</b> <code>{esc_market}</code></blockquote>"
        )

# Backward compatibility alias
format_dump_alert = format_pump_alert


async def fetch_24h_tickers(futures_priority: bool = True) -> List[dict]:
    """
    Queries 24-hour ticker statistics from Binance.
    When futures_priority is True (default), queries Binance Futures API (fapi.binance.com)
    to prioritize active Futures contracts over SPOT-only tokens.
    Falls back to Binance Spot API if Futures API is unavailable.
    """
    primary_url = "https://fapi.binance.com/fapi/v1/ticker/24hr" if futures_priority else "https://api.binance.com/api/v3/ticker/24hr"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(primary_url, timeout=15.0)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning(f"Binance 24h ticker response code: {response.status_code} from {primary_url}")
        except Exception as e:
            logger.error(f"Error fetching 24h tickers from {primary_url}: {e}")

    # Fallback to Spot if futures was primary and failed
    if futures_priority:
        logger.info("Attempting fallback to Binance Spot 24h ticker endpoint...")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15.0)
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return data
        except Exception as e:
            logger.error(f"Error fetching 24h tickers from Spot fallback: {e}")

    return []

def filter_gainers(tickers: List[dict], min_percent: float, futures_only: bool = True) -> List[dict]:
    """
    Filters ticker lists for USDT trading pairs that meet minimum percentage 
    and daily quote volume (> $100k USDT) requirements.
    When futures_only is True, excludes SPOT-only coins.
    """
    import price_fetcher
    gainers = []
    for t in tickers:
        symbol = t.get("symbol", "")
        # Filter for USDT pairs
        if not symbol.endswith("USDT"):
            continue

        # Exclude Spot-only coins if futures_only is enforced and market cache is populated
        if futures_only and price_fetcher._FUTURES_SYMBOLS and symbol not in price_fetcher._FUTURES_SYMBOLS:
            continue
            
        try:
            pct_change = float(t.get("priceChangePercent", 0.0))
            quote_volume = float(t.get("quoteVolume", 0.0))
            
            # Enforce gain threshold and liquid volume > $100k USDT
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

def filter_losers(tickers: List[dict], min_drop_percent: float, futures_only: bool = True) -> List[dict]:
    """
    Filters ticker lists for USDT trading pairs that meet minimum drop percentage 
    (e.g. drop >= 30%) and daily quote volume (> $100k USDT) requirements.
    When futures_only is True, excludes SPOT-only coins.
    Sorted ascending by change percentage (biggest drops first).
    """
    import price_fetcher
    threshold_neg = -abs(min_drop_percent)
    losers = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        # Exclude Spot-only coins if futures_only is enforced and market cache is populated
        if futures_only and price_fetcher._FUTURES_SYMBOLS and symbol not in price_fetcher._FUTURES_SYMBOLS:
            continue
            
        try:
            pct_change = float(t.get("priceChangePercent", 0.0))
            quote_volume = float(t.get("quoteVolume", 0.0))
            
            if pct_change <= threshold_neg and quote_volume >= 100000.0:
                losers.append({
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
            
    # Sort ascending so the worst dumps (-50%, -40%, etc.) appear first
    losers.sort(key=lambda x: x["priceChangePercent"])
    return losers

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

def get_dump_milestone_tier(pct: float, threshold: float) -> int:
    """
    Groups dump drop percentages into milestone tiers:
    - Tier 1: threshold <= drop < 40%
    - Tier 2: 40% <= drop < 50%
    - Tier 3: 50% <= drop < 60%
    - Tier 4+: Step of 10% increment thereafter
    """
    drop = abs(pct) if pct < 0 else 0.0
    thresh = abs(threshold)
    if drop < thresh:
        return 0
    if drop < 40.0:
        return 1
    elif drop < 50.0:
        return 2
    elif drop < 60.0:
        return 3
    else:
        return 4 + int((drop - 60.0) // 10.0)

async def run_gainer_scanner(application: Any):
    """
    Periodic background job executing every 5 minutes to scan USDT market tickers 
    for major daily pump milestones.
    """
    # 1. Check if scanner is enabled
    enabled = db.get_setting("gainer_scanner_enabled", "1")
    if enabled != "1":
        return

    # 2. Get active alert thresholds
    try:
        threshold = float(db.get_setting("gainer_threshold", "50.0"))
    except ValueError:
        threshold = 50.0

    try:
        dump_threshold = float(db.get_setting("dump_threshold", "30.0"))
    except ValueError:
        dump_threshold = 30.0

    logger.info(f"Running market-wide 24h radar scanner (gainer threshold: +{threshold}%, dump threshold: -{dump_threshold}%)...")
    
    tickers = await fetch_24h_tickers(futures_priority=True)
    if not tickers:
        logger.warning("No tickers returned from Binance 24h endpoint.")
        return

    # Ensure market cache is fresh so we can strictly exclude SPOT-only coins
    import price_fetcher
    await price_fetcher.refresh_market_cache()

    gainers = filter_gainers(tickers, threshold, futures_only=True)
    losers = filter_losers(tickers, dump_threshold, futures_only=True)

    now = datetime.datetime.now(datetime.timezone.utc)

    # --- 1. Process 24h Momentum Surge Gainers ---
    for g in gainers:
        symbol = g["symbol"]
        current_pct = g["priceChangePercent"]
        current_price = g["lastPrice"]
        high_24h = g["highPrice"]
        low_24h = g["lowPrice"]
        volume_24h = g["quoteVolume"]

        # Market requirement: Futures priority - strictly skip SPOT-only pairs
        market_type = await price_fetcher.get_market_type(symbol)
        if market_type == "Spot":
            logger.info(f"Skipping pump alert for {symbol}: Spot-only pair excluded (Futures priority requirement).")
            continue

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
            # Log alert in database to trigger cooldown tracker
            alert_id = db.insert_gainer_alert(symbol, current_pct, current_price)

            # Generate combined VIP alert card + chart and dispatch alert to Target Channel
            card_bytes, multiplier = await card_service.generate_combined_alert(
                symbol=symbol,
                current_pct=current_pct,
                current_price=current_price,
                high_24h=high_24h,
                low_24h=low_24h,
                volume_24h=volume_24h,
                market_type=market_type
            )

            msg = format_pump_alert(
                symbol=symbol,
                current_pct=current_pct,
                current_price=current_price,
                high_24h=high_24h,
                low_24h=low_24h,
                volume_24h=volume_24h,
                multiplier=multiplier,
                market_type=market_type
            )

            try:
                if card_bytes:
                    await application.bot.send_photo(
                        chat_id=TARGET_CHAT_ID,
                        photo=card_bytes,
                        caption=msg,
                        parse_mode="HTML"
                    )
                    logger.info(f"Sent 24h VIP pump alert card to Telegram for {symbol} (+{current_pct:.1f}%)")
                else:
                    await application.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode="HTML"
                    )
                    logger.info(f"Sent 24h pump alert text to Telegram for {symbol} (+{current_pct:.1f}%)")
            except Exception as e:
                logger.error(f"Failed to send pump alert with photo for {symbol}: {e}. Trying text fallback...")
                try:
                    await application.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode="HTML"
                    )
                    logger.info(f"Sent fallback text pump alert for {symbol} (+{current_pct:.1f}%)")
                except Exception as fe:
                    logger.warning(f"Failed fallback HTML pump alert for {symbol}: {fe}. Retrying plain text...")
                    try:
                        clean_text = msg.replace('<b>', '').replace('</b>', '').replace('<code>', '').replace('</code>', '').replace('<blockquote>', '').replace('</blockquote>', '').replace('&amp;', '&')
                        await application.bot.send_message(chat_id=TARGET_CHAT_ID, text=clean_text)
                    except Exception as pte:
                        logger.error(f"Failed plain text pump alert for {symbol}: {pte}")

            # Run Mean Reversion Short Analyst signal evaluation
            try:
                import analyst_service
                eval_data = {
                    "symbol": symbol,
                    "priceChangePercent": current_pct,
                    "lastPrice": current_price,
                    "highPrice": high_24h,
                    "lowPrice": low_24h,
                    "quoteVolume": volume_24h
                }
                eval_result = await analyst_service.evaluate_gainer(eval_data)
                decision = eval_result.get("decision", "NO_TRADE")
                logger.info(f"Analyst evaluation for {symbol}: decision={decision}, confidence={eval_result.get('confidence_score')}%")
                
                if decision == "ENTER_SHORT":
                    short_alert_msg = eval_result.get("telegram_alert")
                    if short_alert_msg:
                        try:
                            await application.bot.send_message(
                                chat_id=TARGET_CHAT_ID,
                                text=short_alert_msg,
                                parse_mode="Markdown"
                            )
                            logger.info(f"Dispatched trade short signal alert for {symbol} to Telegram.")
                        except Exception as sme:
                            logger.warning(f"Markdown send failed for short alert ({sme}), falling back to plain text...")
                            clean_short = short_alert_msg.replace('*', '').replace('`', '').replace('_', '')
                            await application.bot.send_message(
                                chat_id=TARGET_CHAT_ID,
                                text=clean_short
                            )
            except Exception as ae:
                logger.error(f"Failed to run short analyst evaluation or send alert for {symbol}: {ae}", exc_info=True)

    # --- 2. Process 24h Downside Crash Losers ---
    for l in losers:
        symbol = l["symbol"]
        current_pct = l["priceChangePercent"]
        current_price = l["lastPrice"]
        high_24h = l["highPrice"]
        low_24h = l["lowPrice"]
        volume_24h = l["quoteVolume"]

        # Market requirement: Futures priority - strictly skip SPOT-only pairs
        market_type = await price_fetcher.get_market_type(symbol)
        if market_type == "Spot":
            logger.info(f"Skipping dump alert for {symbol}: Spot-only pair excluded (Futures priority requirement).")
            continue

        # Check last dump alert history in DB
        last_alert = db.get_last_dump_alert(symbol)
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
                    # Within 4 hours, alert only if the coin drops into a deeper milestone tier
                    last_tier = get_dump_milestone_tier(last_alert["price_change_pct"], dump_threshold)
                    current_tier = get_dump_milestone_tier(current_pct, dump_threshold)
                    if current_tier > last_tier:
                        should_alert = True
                        logger.info(f"{symbol} reached deeper dump tier: Tier {last_tier} -> Tier {current_tier}")
            except Exception as e:
                logger.error(f"Error checking dump cooldown for {symbol}: {e}")
                should_alert = True

        if should_alert:
            # Log dump alert in database to trigger cooldown tracker
            alert_id = db.insert_dump_alert(symbol, current_pct, current_price)

            # Generate combined VIP dump alert card + chart and dispatch alert to Target Channel
            card_bytes, multiplier = await card_service.generate_combined_alert(
                symbol=symbol,
                current_pct=current_pct,
                current_price=current_price,
                high_24h=high_24h,
                low_24h=low_24h,
                volume_24h=volume_24h,
                market_type=market_type
            )

            msg = format_pump_alert(
                symbol=symbol,
                current_pct=current_pct,
                current_price=current_price,
                high_24h=high_24h,
                low_24h=low_24h,
                volume_24h=volume_24h,
                multiplier=multiplier,
                market_type=market_type
            )

            try:
                if card_bytes:
                    await application.bot.send_photo(
                        chat_id=TARGET_CHAT_ID,
                        photo=card_bytes,
                        caption=msg,
                        parse_mode="HTML"
                    )
                    logger.info(f"Sent 24h VIP dump alert card to Telegram for {symbol} ({current_pct:.1f}%)")
                else:
                    await application.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode="HTML"
                    )
                    logger.info(f"Sent 24h dump alert text to Telegram for {symbol} ({current_pct:.1f}%)")
            except Exception as e:
                logger.error(f"Failed to send dump alert with photo for {symbol}: {e}. Trying text fallback...")
                try:
                    await application.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode="HTML"
                    )
                    logger.info(f"Sent fallback text dump alert for {symbol} ({current_pct:.1f}%)")
                except Exception as fe:
                    logger.warning(f"Failed fallback HTML dump alert for {symbol}: {fe}. Retrying plain text...")
                    try:
                        clean_text = msg.replace('<b>', '').replace('</b>', '').replace('<code>', '').replace('</code>', '').replace('<blockquote>', '').replace('</blockquote>', '').replace('&amp;', '&')
                        await application.bot.send_message(chat_id=TARGET_CHAT_ID, text=clean_text)
                    except Exception as pte:
                        logger.error(f"Failed plain text dump alert for {symbol}: {pte}")
