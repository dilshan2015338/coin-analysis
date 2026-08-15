import os
import asyncio
import logging
import datetime
from typing import Any
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Load Environment Variables first so all subsequent imports have access to config
load_dotenv()

import db
import price_fetcher
import kline_service
import gainer_service
import bot

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID")
TARGET_CHAT_ID_RAW = os.getenv("TARGET_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

# Check if essential configurations are present
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not configured in the environment variables.")
if not ADMIN_CHAT_ID_RAW:
    raise ValueError("ADMIN_CHAT_ID is not configured in the environment variables.")
if not TARGET_CHAT_ID_RAW:
    raise ValueError("TARGET_CHAT_ID is not configured in the environment variables.")

# Helper to parse Chat IDs (handles integers or string usernames like @channel)
def parse_chat_id(value: str) -> Any:
    value_str = value.strip()
    try:
        return int(value_str)
    except ValueError:
        return value_str

ADMIN_CHAT_ID = parse_chat_id(ADMIN_CHAT_ID_RAW)
TARGET_CHAT_ID = parse_chat_id(TARGET_CHAT_ID_RAW)

def format_price(price: float) -> str:
    """Formats prices cleanly for human readability."""
    if price is None:
        return "N/A"
    if price >= 1.0:
        return f"${price:,.2f}"
    else:
        return f"${price:,.6f}"

async def price_polling_loop(application: Application):
    """Continuously polls price feeds and alerts the target channel when conditions are met."""
    logger.info("Starting crypto price polling loop...")
    
    # Store last prices in memory to detect midpoint crossovers
    last_prices = {}
    
    # Cache for today's daily open prices: symbol -> (date_str, open_price)
    today_opens = {}
    
    while True:
        try:
            # 1. Load watched coins
            watched = db.get_watched_coins()
            if not watched:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            resolved_symbols = [w["symbol"] for w in watched]
            symbol_to_user = {w["symbol"]: w["user_symbol"] for w in watched}

            # 2. Fetch prices
            prices = await price_fetcher.fetch_prices(resolved_symbols)
            now = datetime.datetime.now(datetime.timezone.utc)
            current_date_str = now.strftime("%Y-%m-%d")

            # 3. Check targets
            targets = db.get_active_target_alerts()
            for t in targets:
                symbol = t["symbol"]
                target_price = t["target_price"]
                condition = t["condition"]
                alert_id = t["id"]

                if symbol not in prices:
                    continue
                current_price = prices[symbol]

                triggered = False
                if condition == "ABOVE" and current_price >= target_price:
                    triggered = True
                elif condition == "BELOW" and current_price <= target_price:
                    triggered = True

                if triggered:
                    # Update database trigger status before sending message to prevent duplicate alerts
                    db.mark_target_alert_triggered(alert_id)

                    user_sym = symbol_to_user.get(symbol, symbol)
                    msg = (
                        f"🚨 *Crypto Price Target Alert* 🚨\n\n"
                        f"Asset: *{user_sym}*\n"
                        f"Target Crossed: {condition} *{format_price(target_price)}*\n"
                        f"Current Price: *{format_price(current_price)}*\n"
                    )
                    await application.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode="Markdown"
                    )
                    logger.info(f"Target Alert Triggered: {symbol} is {current_price} (Target: {condition} {target_price})")

            # 4. Check step thresholds
            step_alerts = db.get_step_alerts()
            for s in step_alerts:
                symbol = s["symbol"]
                step_interval = s["step_interval"]
                baseline_price = s["baseline_price"]

                if symbol not in prices:
                    continue
                current_price = prices[symbol]

                price_change = current_price - baseline_price
                abs_change = abs(price_change)

                if abs_change >= step_interval:
                    # Calculate new baseline based on the target step interval
                    db.update_step_baseline(symbol, current_price)

                    direction = "📈 Up" if price_change > 0 else "📉 Down"
                    user_sym = symbol_to_user.get(symbol, symbol)

                    msg = (
                        f"⚡ *Crypto Step Alert* ⚡\n\n"
                        f"Asset: *{user_sym}*\n"
                        f"Movement: {direction}\n"
                        f"Change: *{price_change:+,.4f}* (Threshold: {format_price(step_interval)})\n"
                        f"Previous Baseline: *{format_price(baseline_price)}*\n"
                        f"Current Price: *{format_price(current_price)}*\n"
                    )
                    await application.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode="Markdown"
                    )
                    logger.info(f"Step Alert Triggered: {symbol} moved by {price_change:+,.4f} (Baseline: {baseline_price} -> {current_price})")

            # 5. Check YTD average alerts (dynamic relative to today's open price starting point)
            avg_alerts = db.get_active_average_alerts()
            for alert in avg_alerts:
                symbol = alert["symbol"]
                metric_type = alert["metric_type"]
                last_triggered_at = alert["last_triggered_at"]
                avg_high_pct = alert["avg_high_pct"]
                avg_low_pct = alert["avg_low_pct"]

                if symbol not in prices:
                    continue
                current_price = prices[symbol]

                # Ensure we have today's open price cached for this symbol
                cached_data = today_opens.get(symbol)
                if not cached_data or cached_data[0] != current_date_str:
                    logger.info(f"Caching today's open price for {symbol}...")
                    open_price = await kline_service.fetch_today_open_price(symbol)
                    if open_price is not None:
                        today_opens[symbol] = (current_date_str, open_price)
                        logger.info(f"Today's open price cached for {symbol}: {open_price}")
                    else:
                        # Skip if open price couldn't be loaded in this iteration
                        continue
                
                open_price = today_opens[symbol][1]

                # Check 1-hour cooldown
                if last_triggered_at:
                    try:
                        last_triggered = datetime.datetime.fromisoformat(last_triggered_at)
                        if last_triggered.tzinfo is None:
                            last_triggered = last_triggered.replace(tzinfo=datetime.timezone.utc)
                        if now - last_triggered < datetime.timedelta(hours=1):
                            # Cooldown active, skip this alert
                            continue
                    except ValueError:
                        pass

                # Compute dynamic daily target based on open price starting point
                triggered = False
                val = 0.0
                if metric_type == "HIGH":
                    val = open_price * (1.0 + avg_high_pct)
                    if current_price >= val:
                        triggered = True
                elif metric_type == "LOW":
                    val = open_price * (1.0 - avg_low_pct)
                    if current_price <= val:
                        triggered = True
                elif metric_type == "MIDPOINT":
                    val = open_price * (1.0 + (avg_high_pct - avg_low_pct) / 2.0)
                    last_price = last_prices.get(symbol)
                    if last_price is not None:
                        # Check crossover crossing the midpoint average line
                        if (last_price < val <= current_price) or (last_price > val >= current_price):
                            triggered = True

                if triggered:
                    # Update triggered timestamp immediately before sending to avoid race conditions
                    db.update_average_alert_triggered(symbol, metric_type)

                    user_sym = symbol_to_user.get(symbol, symbol)
                    msg = (
                        f"🚨 *Crypto YTD Average Alert* 🚨\n\n"
                        f"Asset: *{user_sym}* ({symbol})\n"
                        f"Metric Triggered: *{metric_type}* Average Crossed/Reached\n"
                        f"Today's Start (Open): *{format_price(open_price)}*\n"
                        f"Today's Expected Target: *{format_price(val)}*\n"
                        f"Current Price: *{format_price(current_price)}*\n"
                        f"Cooldown: 1-hour initiated."
                    )
                    await application.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode="Markdown"
                    )
                    logger.info(f"Average Alert Triggered: {symbol} {metric_type} (Price: {current_price}, Open: {open_price}, Target: {val})")

            # Update last prices cache in memory
            for sym, price in prices.items():
                last_prices[sym] = price

        except Exception as e:
            logger.error(f"Error in price polling loop: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)

async def post_init(application: Application):
    """Runs after application is initialized to start background tasks."""
    # 1. Initialize daily_klines and average_metrics for watched coins on startup
    logger.info("Performing historical data catch-up check for all watched coins...")
    watched = db.get_watched_coins()
    for coin in watched:
        asyncio.create_task(kline_service.catch_up_historical_klines(coin["symbol"]))
    
    # 2. Start automated daily update scheduler
    scheduler = kline_service.start_midnight_scheduler(db.get_watched_coins)
    
    # Register the gainer scanner background job (run every 5 minutes)
    scheduler.add_job(
        gainer_service.run_gainer_scanner,
        "interval",
        minutes=5,
        args=[application],
        name="gainer_scanner"
    )
    logger.info("Gainer background scanner scheduled to run every 5 minutes.")
    
    # 3. Start price poller
    asyncio.create_task(price_polling_loop(application))

def main():
    # Initialize local SQLite DB
    db.init_db()
    logger.info("Local SQLite database initialized.")

    # Initialize Telegram Bot Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Register admin message handler for both direct messages and channel posts
    application.add_handler(MessageHandler(filters.TEXT, bot.handle_admin_message))

    # Start Polling for commands
    logger.info("Crypto Alert Bot started polling for commands...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
