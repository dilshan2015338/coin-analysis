import os
import asyncio
import logging
from typing import Any
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

import database
import price_fetcher
from config_parser import parse_message_command

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load Environment Variables
load_dotenv()

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
    if price >= 1.0:
        return f"${price:,.2f}"
    else:
        return f"${price:,.6f}"

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens to text updates, filters for the admin chat, and parses commands."""
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    # Filter out messages from non-admin chats
    if msg.chat.id != ADMIN_CHAT_ID:
        return

    logger.info(f"Received admin command message: {msg.text} in chat {msg.chat.id}")

    try:
        command = parse_message_command(msg.text)
        if not command:
            # Not a recognized command
            return

        cmd_type = command["type"]

        if cmd_type == "watch":
            coins_list = command["coins"]
            resolved_pairs = []
            ignored_coins = []

            # Look up prices to verify validity
            all_resolved = [price_fetcher.resolve_symbol(c) for c in coins_list]
            prices = await price_fetcher.fetch_prices(all_resolved)

            for c in coins_list:
                res = price_fetcher.resolve_symbol(c)
                if res in prices:
                    resolved_pairs.append((res, c))
                else:
                    ignored_coins.append(c)

            database.update_watched_coins(resolved_pairs)

            response = "✅ *Watchlist Updated!*\n\n"
            if resolved_pairs:
                response += "*Monitored Assets:*\n"
                for res, orig in resolved_pairs:
                    price = prices[res]
                    response += f"• *{orig}* ({res}): {format_price(price)}\n"
            else:
                response += "No active assets in the watchlist.\n"

            if ignored_coins:
                response += f"\n⚠️ *Ignored invalid symbols:* {', '.join(ignored_coins)}"

            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "step":
            user_symbol = command["symbol"]
            step_interval = command["step_interval"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            prices = await price_fetcher.fetch_prices([resolved])
            if resolved not in prices:
                await msg.reply_text(f"❌ Could not find price for *{user_symbol}* on Binance.", parse_mode="Markdown")
                return

            current_price = prices[resolved]

            # Auto-watch if not watched
            watched = database.get_watched_coins()
            if not any(w["symbol"] == resolved for w in watched):
                new_watched = [(w["symbol"], w["user_symbol"]) for w in watched]
                new_watched.append((resolved, user_symbol))
                database.update_watched_coins(new_watched)

            database.set_step_alert(resolved, step_interval, current_price)

            response = (
                f"✅ *Step Alert Configured!*\n\n"
                f"Asset: *{user_symbol}* ({resolved})\n"
                f"Step Interval: *{format_price(step_interval)}*\n"
                f"Baseline Price: *{format_price(current_price)}*\n"
                f"Alerting on every price change of {format_price(step_interval)}."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "target":
            user_symbol = command["symbol"]
            target_price = command["target_price"]
            condition = command["condition"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            prices = await price_fetcher.fetch_prices([resolved])
            if resolved not in prices:
                await msg.reply_text(f"❌ Could not find price for *{user_symbol}* on Binance.", parse_mode="Markdown")
                return

            current_price = prices[resolved]

            # Auto-detect ABOVE/BELOW if not supplied
            if not condition:
                condition = "ABOVE" if target_price > current_price else "BELOW"

            # Auto-watch if not watched
            watched = database.get_watched_coins()
            if not any(w["symbol"] == resolved for w in watched):
                new_watched = [(w["symbol"], w["user_symbol"]) for w in watched]
                new_watched.append((resolved, user_symbol))
                database.update_watched_coins(new_watched)

            database.add_target_alert(resolved, target_price, condition)

            response = (
                f"✅ *Target Alert Configured!*\n\n"
                f"Asset: *{user_symbol}* ({resolved})\n"
                f"Target Price: *{format_price(target_price)}*\n"
                f"Trigger Condition: *{condition}*\n"
                f"Current Price: *{format_price(current_price)}*"
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "remove_step":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)
            database.remove_step_alert(resolved)
            response = (
                f"✅ *Step Alert Removed!*\n\n"
                f"Removed step tracker configuration for *{user_symbol}* ({resolved})."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "remove_target":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)
            database.clear_target_alerts(resolved)
            response = (
                f"✅ *Target Alerts Removed!*\n\n"
                f"Removed all active target price alerts for *{user_symbol}* ({resolved})."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "status":
            watched = database.get_watched_coins()
            targets = database.get_active_target_alerts()
            steps = database.get_step_alerts()

            if not watched:
                await msg.reply_text("📊 *Bot Status*: No assets are currently monitored.", parse_mode="Markdown")
                return

            resolved_symbols = [w["symbol"] for w in watched]
            prices = await price_fetcher.fetch_prices(resolved_symbols)

            response = "📊 *Current Alert Bot Status*\n\n"
            response += "*Watched Assets & Prices:*\n"
            for w in watched:
                sym = w["symbol"]
                user_sym = w["user_symbol"]
                price_str = format_price(prices[sym]) if sym in prices else "Price lookup error"
                response += f"• *{user_sym}* ({sym}): {price_str}\n"

            # Active Target Alerts
            if targets:
                response += "\n🎯 *Active Target Price Alerts:*\n"
                for t in targets:
                    sym = t["symbol"]
                    user_sym = next((w["user_symbol"] for w in watched if w["symbol"] == sym), sym)
                    response += f"• *{user_sym}*: {t['condition']} {format_price(t['target_price'])}\n"
            else:
                response += "\n🎯 *Active Target Price Alerts:* None\n"

            # Active Step Alerts
            if steps:
                response += "\n⚡ *Active Step Price Alerts:*\n"
                for s in steps:
                    sym = s["symbol"]
                    user_sym = next((w["user_symbol"] for w in watched if w["symbol"] == sym), sym)
                    response += f"• *{user_sym}*: Interval {format_price(s['step_interval'])}, Baseline {format_price(s['baseline_price'])}\n"
            else:
                response += "\n⚡ *Active Step Price Alerts:* None\n"

            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "help":
            help_text = (
                "ℹ️ <b>Telegram Crypto Alert Bot - Commands List</b>\n\n"
                "You can configure and interact with the bot using the following commands:\n\n"
                "1️⃣ <b>Watch List</b>\n"
                "Set the list of coins to monitor (overwrites existing list):\n"
                "<code>/watch XRP, ADA, UBUSDT, BTC</code>\n"
                "<i>(Alternative: <code>CONFIG WATCH XRP, ADA, UBUSDT, BTC</code>)</i>\n\n"
                "2️⃣ <b>Set Target Alert</b>\n"
                "Set a target price notification alert:\n"
                "<code>/set_target BTC 65000 ABOVE</code> or <code>/set_target BTC 65000 BELOW</code>\n"
                "<i>(Omit ABOVE/BELOW to auto-detect based on current price)</i>\n"
                "<i>(Alternative: <code>CONFIG TARGET BTC 65000</code>)</i>\n\n"
                "3️⃣ <b>Set Step Alert</b>\n"
                "Set a recurring alert every time the price changes by the step amount:\n"
                "<code>/set_step BTC 500</code>\n"
                "<i>(Alternative: <code>CONFIG STEP BTC 500</code>)</i>\n\n"
                "4️⃣ <b>Remove Step Alert</b>\n"
                "Disable step tracking for a specific coin:\n"
                "<code>/remove_step BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_STEP BTC</code>)</i>\n\n"
                "5️⃣ <b>Remove Target Alert</b>\n"
                "Disable all active target price alerts for a specific coin:\n"
                "<code>/remove_target BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_TARGET BTC</code>)</i>\n\n"
                "6️⃣ <b>Check Status</b>\n"
                "Show active targets, step configurations, and current prices:\n"
                "<code>/status</code>\n"
                "<i>(Alternative: <code>CONFIG STATUS</code>)</i>\n\n"
                "7️⃣ <b>Help Instructions</b>\n"
                "Display this help message:\n"
                "<code>/help</code> or <code>/start</code>\n"
                "<i>(Alternative: <code>CONFIG HELP</code>)</i>"
            )
            await msg.reply_text(help_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error handling admin command: {e}", exc_info=True)
        await msg.reply_text(f"❌ *Error processing command:* {str(e)}", parse_mode="Markdown")

async def price_polling_loop(application: Application):
    """Continuously polls price feeds and alerts the target channel when conditions are met."""
    logger.info("Starting crypto price polling loop...")
    while True:
        try:
            # 1. Load watched coins
            watched = database.get_watched_coins()
            if not watched:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            resolved_symbols = [w["symbol"] for w in watched]
            symbol_to_user = {w["symbol"]: w["user_symbol"] for w in watched}

            # 2. Fetch prices
            prices = await price_fetcher.fetch_prices(resolved_symbols)

            # 3. Check targets
            targets = database.get_active_target_alerts()
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
                    database.mark_target_alert_triggered(alert_id)

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
            step_alerts = database.get_step_alerts()
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
                    # Calculate new baseline based on the target step interval.
                    # This standardizes baseline updates to current price intervals.
                    database.update_step_baseline(symbol, current_price)

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

        except Exception as e:
            logger.error(f"Error in price polling loop: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)

async def post_init(application: Application):
    """Runs after application is initialized to start background tasks."""
    asyncio.create_task(price_polling_loop(application))

def main():
    # Initialize local SQLite DB
    database.init_db()
    logger.info("Local SQLite database initialized.")

    # Initialize Telegram Bot Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Register admin message handler for both direct messages and channel posts
    application.add_handler(MessageHandler(filters.TEXT, handle_admin_message))

    # Start Polling for commands
    logger.info("Crypto Alert Bot started polling for commands...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
