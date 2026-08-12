import os
import logging
from typing import Any
from telegram import Update
from telegram.ext import ContextTypes

import db
import price_fetcher
import kline_service
from config_parser import parse_message_command

logger = logging.getLogger(__name__)

# Load configurations
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID")
if not ADMIN_CHAT_ID_RAW:
    raise ValueError("ADMIN_CHAT_ID is not configured in the environment variables.")

def parse_chat_id(value: str) -> Any:
    value_str = value.strip()
    try:
        return int(value_str)
    except ValueError:
        return value_str

ADMIN_CHAT_ID = parse_chat_id(ADMIN_CHAT_ID_RAW)

def format_price(price: float) -> str:
    """Formats prices cleanly for human readability."""
    if price is None:
        return "N/A"
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

            db.update_watched_coins(resolved_pairs)

            # Trigger historical download/catch-up for newly watched coins asynchronously
            for res, orig in resolved_pairs:
                asyncio.create_task(kline_service.catch_up_historical_klines(res))

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
            watched = db.get_watched_coins()
            if not any(w["symbol"] == resolved for w in watched):
                new_watched = [(w["symbol"], w["user_symbol"]) for w in watched]
                new_watched.append((resolved, user_symbol))
                db.update_watched_coins(new_watched)
                asyncio.create_task(kline_service.catch_up_historical_klines(resolved))

            db.set_step_alert(resolved, step_interval, current_price)

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
            watched = db.get_watched_coins()
            if not any(w["symbol"] == resolved for w in watched):
                new_watched = [(w["symbol"], w["user_symbol"]) for w in watched]
                new_watched.append((resolved, user_symbol))
                db.update_watched_coins(new_watched)
                asyncio.create_task(kline_service.catch_up_historical_klines(resolved))

            db.add_target_alert(resolved, target_price, condition)

            response = (
                f"✅ *Target Alert Configured!*\n\n"
                f"Asset: *{user_symbol}* ({resolved})\n"
                f"Target Price: *{format_price(target_price)}*\n"
                f"Trigger Condition: *{condition}*\n"
                f"Current Price: *{format_price(current_price)}*"
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "set_avg_alert":
            user_symbol = command["symbol"]
            metric_type = command["metric_type"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            prices = await price_fetcher.fetch_prices([resolved])
            if resolved not in prices:
                await msg.reply_text(f"❌ Could not find price for *{user_symbol}* on Binance.", parse_mode="Markdown")
                return

            current_price = prices[resolved]

            # Auto-watch if not watched
            watched = db.get_watched_coins()
            if not any(w["symbol"] == resolved for w in watched):
                new_watched = [(w["symbol"], w["user_symbol"]) for w in watched]
                new_watched.append((resolved, user_symbol))
                db.update_watched_coins(new_watched)

            # Ensure historical metrics are populated in database
            metrics = db.get_average_metrics(resolved)
            if not metrics:
                await msg.reply_text(f"⏳ Fetching historical klines since Jan 1, 2026 for *{user_symbol}*...", parse_mode="Markdown")
                await kline_service.catch_up_historical_klines(resolved)
                metrics = db.get_average_metrics(resolved)
                if not metrics:
                    await msg.reply_text(f"❌ Failed to calculate YTD metrics for *{user_symbol}*.", parse_mode="Markdown")
                    return

            open_price = await kline_service.fetch_today_open_price(resolved)
            if open_price is None:
                await msg.reply_text(f"❌ Could not retrieve today's open price to set threshold for *{user_symbol}*.", parse_mode="Markdown")
                return

            db.set_average_alert(resolved, metric_type)
            
            if metric_type == "HIGH":
                val = open_price * (1.0 + metrics['avg_high_pct'])
            elif metric_type == "LOW":
                val = open_price * (1.0 - metrics['avg_low_pct'])
            else:  # MIDPOINT
                val = open_price * (1.0 + (metrics['avg_high_pct'] - metrics['avg_low_pct']) / 2.0)

            response = (
                f"✅ *Average Price Alert Configured!*\n\n"
                f"Asset: *{user_symbol}* ({resolved})\n"
                f"Today's Open: *{format_price(open_price)}*\n"
                f"Alert Type: *{metric_type}*\n"
                f"Today's Threshold: *{format_price(val)}*\n"
                f"Current Price: *{format_price(current_price)}*"
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "remove_step":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)
            db.remove_step_alert(resolved)
            response = (
                f"✅ *Step Alert Removed!*\n\n"
                f"Removed step tracker configuration for *{user_symbol}* ({resolved})."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "remove_target":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)
            db.clear_target_alerts(resolved)
            response = (
                f"✅ *Target Alerts Removed!*\n\n"
                f"Removed all active target price alerts for *{user_symbol}* ({resolved})."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "remove_avg_alert":
            user_symbol = command["symbol"]
            metric_type = command.get("metric_type")
            resolved = price_fetcher.resolve_symbol(user_symbol)

            if metric_type:
                db.remove_average_alert(resolved, metric_type)
                response = (
                    f"✅ *Average Price Alert Removed!*\n\n"
                    f"Removed *{metric_type}* alert configuration for *{user_symbol}* ({resolved})."
                )
            else:
                db.clear_average_alerts(resolved)
                response = (
                    f"✅ *Average Price Alerts Removed!*\n\n"
                    f"Removed all average price alert configurations for *{user_symbol}* ({resolved})."
                )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "status":
            symbol = command.get("symbol")
            if symbol:
                resolved = price_fetcher.resolve_symbol(symbol)
                metrics = db.get_average_metrics(resolved)
                prices = await price_fetcher.fetch_prices([resolved])
                current_price = prices.get(resolved)

                if not metrics:
                    await msg.reply_text(f"⏳ Average metrics for *{symbol}* not found in database. Initializing...", parse_mode="Markdown")
                    try:
                        await kline_service.catch_up_historical_klines(resolved)
                        metrics = db.get_average_metrics(resolved)
                    except Exception as e:
                        logger.error(f"Error loading metrics for *{symbol}*: {e}")

                open_price = await kline_service.fetch_today_open_price(resolved)

                if metrics and open_price is not None:
                    price_str = format_price(current_price) if current_price else "Price lookup error"
                    
                    # Intraday Trend Calculation
                    if current_price is not None:
                        intraday_pct = (current_price - open_price) / open_price * 100.0
                        intraday_trend = f"📈 Bullish / Uptrend ({intraday_pct:+.2f}%)" if intraday_pct >= 0 else f"📉 Bearish / Downtrend ({intraday_pct:+.2f}%)"
                    else:
                        intraday_trend = "Unknown (Price lookup error)"

                    # Daily Trend (20 SMA) Calculation
                    recent_closes = db.get_recent_closes(resolved, limit=20)
                    if recent_closes:
                        sma_20 = sum(recent_closes) / len(recent_closes)
                        if current_price is not None:
                            daily_trend_status = "📈 Bullish / Uptrend" if current_price > sma_20 else "📉 Bearish / Downtrend"
                            sma_comparison = f"Price is above 20-day SMA ({format_price(sma_20)})" if current_price > sma_20 else f"Price is below 20-day SMA ({format_price(sma_20)})"
                            daily_trend = f"{daily_trend_status} ({sma_comparison})"
                        else:
                            daily_trend = f"Unknown (20-day SMA is {format_price(sma_20)})"
                    else:
                        daily_trend = "Insufficient historical klines to calculate 20-day SMA."

                    expected_high_avg = open_price * (1.0 + metrics['avg_high_pct'])
                    expected_low_avg = open_price * (1.0 - metrics['avg_low_pct'])
                    expected_midpoint_avg = (expected_high_avg + expected_low_avg) / 2.0
                    expected_high_max = open_price * (1.0 + metrics['max_high_pct'])
                    expected_low_max = open_price * (1.0 - metrics['max_low_pct'])

                    response = (
                        f"📊 *Daily Projected Levels for {symbol}* ({resolved})\n\n"
                        f"• *Today's Start (Open):* {format_price(open_price)}\n"
                        f"• *Current Price:* {price_str}\n\n"
                        f"🎯 *Trend Analysis:*\n"
                        f"  - Intraday Trend: *{intraday_trend}*\n"
                        f"  - Daily Trend (20 SMA): *{daily_trend}*\n\n"
                        f"📈 *Expected Ranges (YTD Averages):*\n"
                        f"  - Max High: *{format_price(expected_high_avg)}* (+{metrics['avg_high_pct']*100:.2f}%)\n"
                        f"  - Max Low: *{format_price(expected_low_avg)}* (-{metrics['avg_low_pct']*100:.2f}%)\n"
                        f"  - Midpoint: *{format_price(expected_midpoint_avg)}*\n\n"
                        f"🔥 *Historical Maximums (YTD Peaks):*\n"
                        f"  - Max High Peak: *{format_price(expected_high_max)}* (+{metrics['max_high_pct']*100:.2f}%)\n"
                        f"  - Max Low Peak: *{format_price(expected_low_max)}* (-{metrics['max_low_pct']*100:.2f}%)\n\n"
                        f"⚙️ *Evaluation Metadata:*\n"
                        f"  - Total Days Analyzed: *{metrics['total_days']}*\n"
                        f"  - Last Database Refresh: *{metrics['last_updated']}*"
                    )
                else:
                    response = f"❌ Could not calculate average metrics or retrieve today's open price for *{symbol}*."
                await msg.reply_text(response, parse_mode="Markdown")
            else:
                # Global watchlist status
                watched = db.get_watched_coins()
                targets = db.get_active_target_alerts()
                steps = db.get_step_alerts()
                avg_alerts = db.get_active_average_alerts()

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

                # Active Average Alerts
                if avg_alerts:
                    response += "\n📈 *Active Average Price Alerts:*\n"
                    for a in avg_alerts:
                        sym = a["symbol"]
                        metric_type = a["metric_type"]
                        user_sym = next((w["user_symbol"] for w in watched if w["symbol"] == sym), sym)
                        # Fetch today's open price to compute current active target
                        open_price = await kline_service.fetch_today_open_price(sym)
                        if open_price:
                            if metric_type == "HIGH":
                                val = open_price * (1.0 + a['avg_high_pct'])
                            elif metric_type == "LOW":
                                val = open_price * (1.0 - a['avg_low_pct'])
                            else:
                                val = open_price * (1.0 + (a['avg_high_pct'] - a['avg_low_pct']) / 2.0)
                            response += f"• *{user_sym}*: {metric_type} (Today: {format_price(val)})\n"
                        else:
                            response += f"• *{user_sym}*: {metric_type}\n"
                else:
                    response += "\n📈 *Active Average Price Alerts:* None\n"

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
                "4️⃣ <b>Set Average Alert</b>\n"
                "Set alert for crossing YTD average high/low/midpoint metrics:\n"
                "<code>/set_avg_alert BTC HIGH</code> or <code>/set_avg_alert BTC MIDPOINT</code>\n"
                "<i>(Alternative: <code>CONFIG AVG_ALERT BTC LOW</code>)</i>\n\n"
                "5️⃣ <b>Remove Step Alert</b>\n"
                "Disable step tracking for a specific coin:\n"
                "<code>/remove_step BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_STEP BTC</code>)</i>\n\n"
                "6️⃣ <b>Remove Target Alert</b>\n"
                "Disable all active target price alerts for a specific coin:\n"
                "<code>/remove_target BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_TARGET BTC</code>)</i>\n\n"
                "7️⃣ <b>Remove Average Alert</b>\n"
                "Disable average price alerts for a specific coin:\n"
                "<code>/remove_avg_alert BTC HIGH</code> or <code>/remove_avg_alert BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_AVG_ALERT BTC LOW</code>)</i>\n\n"
                "8️⃣ <b>Check Status</b>\n"
                "Show active targets, step configurations, average alerts and current prices:\n"
                "<code>/status</code> or <code>/status BTC</code>\n"
                "<i>(Alternative: <code>CONFIG STATUS BTC</code>)</i>\n\n"
                "9️⃣ <b>Help Instructions</b>\n"
                "Display this help message:\n"
                "<code>/help</code> or <code>/start</code>\n"
                "<i>(Alternative: <code>CONFIG HELP</code>)</i>"
            )
            await msg.reply_text(help_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error handling admin command: {e}", exc_info=True)
        await msg.reply_text(f"❌ *Error processing command:* {str(e)}", parse_mode="Markdown")

import asyncio
