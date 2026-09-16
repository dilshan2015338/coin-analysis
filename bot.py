import os
import asyncio
import logging
import html
from typing import Any
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ContextTypes

# Load environment variables
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import db
import price_fetcher
import kline_service
import gainer_service
import card_service
import chart_service
import analyst_service
from config_parser import parse_message_command

logger = logging.getLogger(__name__)

# Load configurations
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID_ME") or os.getenv("ADMIN_CHAT_ME") or os.getenv("ADMIN_CHAT_ID")
if not ADMIN_CHAT_ID_RAW:
    raise ValueError("Neither ADMIN_CHAT_ID_ME nor ADMIN_CHAT_ID is configured in the environment variables.")

MARKET_ANALYZER_URL = os.getenv("MARKET_ANALYZER_URL", "http://127.0.0.1:8000/api/v1/analyze")
MARKET_ANALYZER_API_KEY = os.getenv("MARKET_ANALYZER_API_KEY", "lF2vIg=Pik8S_(^iC8$23H&fBz$4h7L6-rOCiZ*x&a#bjvp+(fF_9hTII5aul@gq")

def parse_chat_id(value: str) -> Any:
    value_str = value.strip()
    try:
        return int(value_str)
    except ValueError:
        return value_str

ADMIN_CHAT_ID = parse_chat_id(ADMIN_CHAT_ID_RAW.split(",")[0])

def get_authorized_chat_ids() -> set:
    """
    Returns a set of authorized chat/group IDs parsed from environment variables:
    - ADMIN_CHAT_ID (supports comma-separated list of IDs)
    - ADMIN_CHAT_ID_ME / ADMIN_CHAT_ME (new group / personal group)
    - ALLOWED_CHAT_IDS (optional comma-separated list)
    """
    chat_ids = set()
    for var in ("ADMIN_CHAT_ID", "ADMIN_CHAT_ID_ME", "ADMIN_CHAT_ME", "ALLOWED_CHAT_IDS"):
        raw_val = os.getenv(var, "")
        if raw_val:
            for piece in raw_val.split(","):
                clean = piece.strip()
                if clean:
                    chat_ids.add(parse_chat_id(clean))
    return chat_ids

def format_price(price: float) -> str:
    """Formats prices cleanly for human readability."""
    if price is None:
        return "N/A"
    if price >= 1.0:
        return f"${price:,.2f}"
    else:
        return f"${price:,.6f}"

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens to text updates, filters for authorized chats, and parses commands."""
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    # Check against authorized chats (ADMIN_CHAT_ID, ADMIN_CHAT_ID_ME, etc.)
    authorized_chats = get_authorized_chat_ids()
    if msg.chat.id not in authorized_chats:
        logger.debug(f"Ignoring message from unauthorized chat: {msg.chat.id}")
        return

    chat_title = getattr(msg.chat, 'title', None) or getattr(msg.chat, 'username', None) or 'Direct/Private'
    logger.info(f"Received command message: '{msg.text}' from chat {msg.chat.id} ({chat_title})")

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

        elif cmd_type == "set_update":
            user_symbol = command["symbol"]
            interval_minutes = command["interval_minutes"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            prices = await price_fetcher.fetch_prices([resolved])
            if resolved not in prices:
                await msg.reply_text(f"❌ Could not find price for *{user_symbol}* on Binance.", parse_mode="Markdown")
                return

            # Auto-watch if not watched
            watched = db.get_watched_coins()
            if not any(w["symbol"] == resolved for w in watched):
                new_watched = [(w["symbol"], w["user_symbol"]) for w in watched]
                new_watched.append((resolved, user_symbol))
                db.update_watched_coins(new_watched)
                asyncio.create_task(kline_service.catch_up_historical_klines(resolved))

            db.set_recurring_update(resolved, interval_minutes)

            response = (
                f"✅ *Recurring Price Update Configured!*\n\n"
                f"Asset: *{user_symbol}* ({resolved})\n"
                f"Interval: Every *{interval_minutes}* minutes\n"
                f"Updates will be posted automatically."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "remove_update":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)
            db.remove_recurring_update(resolved)
            response = (
                f"✅ *Recurring Price Update Removed!*\n\n"
                f"Removed recurring update schedule for *{user_symbol}* ({resolved})."
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

                # Active Recurring Updates
                recurring_updates = db.get_recurring_updates()
                if recurring_updates:
                    response += "\n⏰ *Active Recurring Price Updates:*\n"
                    for r in recurring_updates:
                        sym = r["symbol"]
                        user_sym = next((w["user_symbol"] for w in watched if w["symbol"] == sym), sym)
                        response += f"• *{user_sym}*: Every {r['interval_minutes']} min\n"
                else:
                    response += "\n⏰ *Active Recurring Price Updates:* None\n"

                # 24h Gainer Scanner settings
                scanner_enabled = db.get_setting("gainer_scanner_enabled", "1")
                gainer_threshold = db.get_setting("gainer_threshold", "50.0")
                scanner_status = "ON" if scanner_enabled == "1" else "OFF"
                
                response += f"\n📢 *24h Pump Scanner:*\n"
                response += f"• Status: *{scanner_status}*\n"
                response += f"• Threshold: *{gainer_threshold}%*\n"

                await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type in ("short_status", "evaluate"):
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            status_msg = await msg.reply_text(f"⏳ Querying data and evaluating short reversion signal for <b>#{user_symbol}</b> (<code>{resolved}</code>)...", parse_mode="HTML")

            try:
                ticker_24h = await price_fetcher.fetch_24h_ticker(resolved)
                if not ticker_24h:
                    prices = await price_fetcher.fetch_prices([resolved])
                    if resolved in prices:
                        current_price = prices[resolved]
                        ticker_24h = {
                            "symbol": resolved,
                            "priceChangePercent": 0.0,
                            "lastPrice": current_price,
                            "highPrice": current_price,
                            "lowPrice": current_price,
                            "quoteVolume": 0.0,
                            "source": "spot"
                        }
                    else:
                        await status_msg.edit_text(f"❌ Could not find active market price for <b>#{user_symbol}</b> on Binance.", parse_mode="HTML")
                        return

                eval_result = await analyst_service.evaluate_gainer(ticker_24h)
                alert_text = eval_result.get("telegram_alert")
                if alert_text:
                    try:
                        await status_msg.edit_text(alert_text, parse_mode="HTML")
                    except Exception as he:
                        logger.warning(f"HTML format edit failed ({he}), sending clean text...")
                        import re
                        clean_text = re.sub(r'<[^>]+>', '', alert_text)
                        await status_msg.edit_text(clean_text)
                else:
                    await status_msg.edit_text(f"❌ Evaluation returned empty alert for <b>#{user_symbol}</b>.", parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error evaluating {resolved} on short_status command: {e}", exc_info=True)
                await status_msg.edit_text(f"❌ Error during evaluation of <b>#{user_symbol}</b>: {html.escape(str(e))}", parse_mode="HTML")

        elif cmd_type == "analyze":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            status_msg = await msg.reply_text(
                f"⏳ Running Quantitative Risk & Squeeze Audit for <b>#{user_symbol}</b> (<code>{resolved}</code>)...",
                parse_mode="HTML"
            )

            try:
                # 1. Fetch 24h ticker data (handles both Binance Futures & Spot assets)
                ticker_24h = await price_fetcher.fetch_24h_ticker(resolved)
                if not ticker_24h:
                    prices = await price_fetcher.fetch_prices([resolved])
                    if resolved in prices:
                        current_price = prices[resolved]
                        ticker_24h = {
                            "symbol": resolved,
                            "priceChangePercent": 0.0,
                            "lastPrice": current_price,
                            "highPrice": current_price,
                            "lowPrice": current_price,
                            "quoteVolume": 0.0,
                            "source": "spot"
                        }
                    else:
                        await status_msg.edit_text(f"❌ Could not find market data for <b>#{user_symbol}</b> on Binance Spot or Futures.", parse_mode="HTML")
                        return

                current_price = float(ticker_24h.get("lastPrice", 0.0))
                change_pct = float(ticker_24h.get("priceChangePercent", 0.0))
                high_24h = float(ticker_24h.get("highPrice", current_price))
                low_24h = float(ticker_24h.get("lowPrice", current_price))
                volume_24h = float(ticker_24h.get("quoteVolume", 0.0))

                # 2. Run Quantitative Risk & Squeeze Audit
                eval_result = await analyst_service.evaluate_gainer(ticker_24h)
                risk_alert_text = eval_result.get("telegram_alert")

                # 3. Render and dispatch VIP Alert Card + 15M Candlestick Chart graphic
                try:
                    market_type = await price_fetcher.get_market_type(resolved)
                    card_bytes, _ = await card_service.generate_combined_alert(
                        symbol=resolved,
                        current_pct=change_pct,
                        current_price=current_price,
                        high_24h=high_24h,
                        low_24h=low_24h,
                        volume_24h=volume_24h,
                        multiplier=1.5,
                        market_type=market_type
                    )
                    if card_bytes:
                        verdict = eval_result.get("verdict", "ANALYZE")
                        score = eval_result.get("confidence_score", 0)
                        card_caption = (
                            f"📊 <b>#{resolved} Quantitative Market Analysis</b>\n"
                            f"🎯 <b>Verdict:</b> {verdict} (Score: <b>{score}/100</b>)\n"
                            f"💵 <b>Price:</b> <code>${current_price:,.4f}</code> ({change_pct:+.2f}%)"
                        )
                        card_bytes.seek(0)
                        await context.bot.send_photo(
                            chat_id=msg.chat.id,
                            photo=card_bytes,
                            caption=card_caption,
                            parse_mode="HTML"
                        )
                except Exception as card_err:
                    logger.warning(f"VIP Chart card rendering skipped for {resolved}: {card_err}")

                # 4. Deliver the Full Quantitative Risk & Stop-Loss Audit Report to the group
                if risk_alert_text:
                    try:
                        await status_msg.edit_text(risk_alert_text, parse_mode="HTML")
                    except Exception as he:
                        logger.warning(f"HTML report edit failed ({he}), falling back to plain text...")
                        import re
                        clean_text = re.sub(r'<[^>]+>', '', risk_alert_text)
                        await status_msg.edit_text(clean_text)
                else:
                    await status_msg.edit_text(f"❌ Quantitative evaluation produced no alert output for <b>#{user_symbol}</b>.", parse_mode="HTML")

            except Exception as e:
                logger.error(f"Error during /analyze evaluation of {resolved}: {e}", exc_info=True)
                await status_msg.edit_text(f"❌ Error during analysis of <b>#{user_symbol}</b>: {html.escape(str(e))}", parse_mode="HTML")


        elif cmd_type == "gainers":
            min_percent = command.get("min_percent")
            if min_percent is None:
                try:
                    min_percent = float(db.get_setting("gainer_threshold", "50.0"))
                except ValueError:
                    min_percent = 50.0
                    
            status_msg = await msg.reply_text(f"⏳ Scanning Binance 24h Futures market stats for pairs ≥ <b>{min_percent:.0f}%</b> gain...", parse_mode="HTML")
            
            tickers = await gainer_service.fetch_24h_tickers(futures_priority=True)
            if not tickers:
                await status_msg.edit_text("❌ Failed to fetch market stats from Binance.", parse_mode="HTML")
                return

            gainers = gainer_service.filter_gainers(tickers, min_percent, futures_only=True)
            if not gainers:
                await status_msg.edit_text(f"ℹ️ No Futures USDT trading pairs currently meet the ≥ <b>{min_percent:.0f}%</b> gain criteria.", parse_mode="HTML")
                return

            # Display top 15
            top_gainers = gainers[:15]
            response = (
                f"⚡ <b>CryptoPulse VIP | 24h Futures Top Gainers</b> ⚡\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥 <i>Futures pairs with ≥{min_percent:.0f}% rolling 24h momentum</i>\n\n"
            )
            for g in top_gainers:
                sym = g["symbol"]
                change = g["priceChangePercent"]
                price = g["lastPrice"]
                vol = g["quoteVolume"]
                response += f"• <b>#{sym}</b>: <code>+{change:.2f}%</code> 🟢 | <code>{gainer_service.format_price(price)}</code> | Vol: <code>{gainer_service.format_volume(vol)}</code>\n"
                
            if len(gainers) > 15:
                response += f"\n<i>(showing top 15 out of {len(gainers)} total gainers above {min_percent:.0f}%)</i>\n"
                
            response += (
                f"\n━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>Tip: Type <code>/chart &lt;symbol&gt;</code> to view the momentum candlestick chart!</i>"
            )
                
            await status_msg.edit_text(response, parse_mode="HTML")

        elif cmd_type == "losers":
            min_drop = command["min_percent"]
            if min_drop is None:
                try:
                    min_drop = float(db.get_setting("dump_threshold", "30.0"))
                except ValueError:
                    min_drop = 30.0
                    
            status_msg = await msg.reply_text(f"⏳ Scanning Binance 24h Futures market stats for pairs ≤ <b>-{min_drop:.0f}%</b> drop...", parse_mode="HTML")
            
            tickers = await gainer_service.fetch_24h_tickers(futures_priority=True)
            if not tickers:
                await status_msg.edit_text("❌ Failed to fetch market stats from Binance.", parse_mode="HTML")
                return

            losers = gainer_service.filter_losers(tickers, min_drop, futures_only=True)
            if not losers:
                await status_msg.edit_text(f"ℹ️ No Futures USDT trading pairs currently meet the ≤ <b>-{min_drop:.0f}%</b> drop criteria.", parse_mode="HTML")
                return

            # Display top 15
            top_losers = losers[:15]
            response = (
                f"⚡ <b>CryptoPulse VIP | 24h Futures Top Losers</b> ⚡\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔻 <i>Futures pairs with ≥{min_drop:.0f}% rolling 24h downside crash</i>\n\n"
            )
            for l in top_losers:
                sym = l["symbol"]
                change = l["priceChangePercent"]
                price = l["lastPrice"]
                vol = l["quoteVolume"]
                response += f"• <b>#{sym}</b>: <code>{change:.2f}%</code> 🔴 | <code>{gainer_service.format_price(price)}</code> | Vol: <code>{gainer_service.format_volume(vol)}</code>\n"
                
            if len(losers) > 15:
                response += f"\n<i>(showing top 15 out of {len(losers)} total losers dropped ≥{min_drop:.0f}%)</i>\n"
                
            response += (
                f"\n━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>Tip: Type <code>/dump &lt;symbol&gt;</code> to view the VIP dump alert card!</i>"
            )
                
            await status_msg.edit_text(response, parse_mode="HTML")

        elif cmd_type == "pump_card":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            status_msg = await msg.reply_text(f"⏳ Generating VIP pump alert card for <b>#{resolved}</b>...", parse_mode="HTML")
            try:
                # Fetch 24h ticker prioritizing Futures endpoint, with Spot fallback
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={resolved}", timeout=10.0)
                    if resp.status_code != 200:
                        resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=10.0)
                    if resp.status_code != 200:
                        await status_msg.edit_text(f"❌ Could not find 24h statistics for <b>#{resolved}</b> on Binance.", parse_mode="HTML")
                        return
                    d = resp.json()

                current_pct = float(d.get("priceChangePercent", 0.0))
                current_price = float(d.get("lastPrice", 0.0))
                high_24h = float(d.get("highPrice", 0.0))
                low_24h = float(d.get("lowPrice", 0.0))
                volume_24h = float(d.get("quoteVolume", 0.0))

                market_type = await price_fetcher.get_market_type(resolved)

                card_bytes, multiplier = await card_service.generate_combined_alert(
                    symbol=resolved,
                    current_pct=current_pct,
                    current_price=current_price,
                    high_24h=high_24h,
                    low_24h=low_24h,
                    volume_24h=volume_24h,
                    market_type=market_type
                )

                card_caption = gainer_service.format_pump_alert(
                    symbol=resolved,
                    current_pct=current_pct,
                    current_price=current_price,
                    high_24h=high_24h,
                    low_24h=low_24h,
                    volume_24h=volume_24h,
                    multiplier=multiplier,
                    market_type=market_type
                )

                if card_bytes:
                    await msg.reply_photo(
                        photo=card_bytes,
                        caption=card_caption,
                        parse_mode="HTML"
                    )
                else:
                    await msg.reply_text(card_caption, parse_mode="HTML")
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error handling pump_card command for {resolved}: {e}", exc_info=True)
                try:
                    await status_msg.edit_text(f"❌ Error generating VIP card for <b>#{html.escape(resolved)}</b>: {html.escape(str(e))}", parse_mode="HTML")
                except Exception:
                    await msg.reply_text(f"❌ Error generating VIP card for #{resolved}: {html.escape(str(e))}")

        elif cmd_type == "dump_card":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            status_msg = await msg.reply_text(f"⏳ Generating VIP dump alert card for <b>#{resolved}</b>...", parse_mode="HTML")
            try:
                # Fetch 24h ticker prioritizing Futures endpoint, with Spot fallback
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={resolved}", timeout=10.0)
                    if resp.status_code != 200:
                        resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=10.0)
                    if resp.status_code != 200:
                        await status_msg.edit_text(f"❌ Could not find 24h statistics for <b>#{resolved}</b> on Binance.", parse_mode="HTML")
                        return
                    d = resp.json()

                current_pct = float(d.get("priceChangePercent", 0.0))
                current_price = float(d.get("lastPrice", 0.0))
                high_24h = float(d.get("highPrice", 0.0))
                low_24h = float(d.get("lowPrice", 0.0))
                volume_24h = float(d.get("quoteVolume", 0.0))

                market_type = await price_fetcher.get_market_type(resolved)

                card_bytes, multiplier = await card_service.generate_combined_alert(
                    symbol=resolved,
                    current_pct=current_pct,
                    current_price=current_price,
                    high_24h=high_24h,
                    low_24h=low_24h,
                    volume_24h=volume_24h,
                    market_type=market_type
                )

                card_caption = gainer_service.format_pump_alert(
                    symbol=resolved,
                    current_pct=current_pct,
                    current_price=current_price,
                    high_24h=high_24h,
                    low_24h=low_24h,
                    volume_24h=volume_24h,
                    multiplier=multiplier,
                    market_type=market_type
                )

                if card_bytes:
                    await msg.reply_photo(
                        photo=card_bytes,
                        caption=card_caption,
                        parse_mode="HTML"
                    )
                else:
                    await msg.reply_text(card_caption, parse_mode="HTML")
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error handling dump_card command for {resolved}: {e}", exc_info=True)
                try:
                    await status_msg.edit_text(f"❌ Error generating VIP card for <b>#{html.escape(resolved)}</b>: {html.escape(str(e))}", parse_mode="HTML")
                except Exception:
                    await msg.reply_text(f"❌ Error generating VIP card for #{resolved}: {html.escape(str(e))}")

        elif cmd_type == "pump_chart":
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            status_msg = await msg.reply_text(f"⏳ Generating momentum chart for <b>#{resolved}</b>...", parse_mode="HTML")
            try:
                # Fetch 24h ticker prioritizing Futures endpoint, with Spot fallback
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={resolved}", timeout=10.0)
                    if resp.status_code != 200:
                        resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=10.0)
                    if resp.status_code != 200:
                        await status_msg.edit_text(f"❌ Could not find 24h statistics for <b>#{resolved}</b> on Binance.", parse_mode="HTML")
                        return
                    d = resp.json()

                current_pct = float(d.get("priceChangePercent", 0.0))
                current_price = float(d.get("lastPrice", 0.0))
                high_24h = float(d.get("highPrice", 0.0))
                low_24h = float(d.get("lowPrice", 0.0))
                volume_24h = float(d.get("quoteVolume", 0.0))

                market_type = await price_fetcher.get_market_type(resolved)

                chart_bytes, multiplier = await chart_service.generate_pump_chart(
                    symbol=resolved,
                    current_pct=current_pct,
                    current_price=current_price,
                    high_24h=high_24h,
                    low_24h=low_24h,
                    volume_24h=volume_24h
                )

                card_caption = gainer_service.format_pump_alert(
                    symbol=resolved,
                    current_pct=current_pct,
                    current_price=current_price,
                    high_24h=high_24h,
                    low_24h=low_24h,
                    volume_24h=volume_24h,
                    multiplier=multiplier,
                    market_type=market_type
                )

                if chart_bytes:
                    await msg.reply_photo(
                        photo=chart_bytes,
                        caption=card_caption,
                        parse_mode="HTML"
                    )
                else:
                    await msg.reply_text(card_caption, parse_mode="HTML")
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error handling pump_chart command for {resolved}: {e}", exc_info=True)
                try:
                    await status_msg.edit_text(f"❌ Error generating chart for <b>#{html.escape(resolved)}</b>: {html.escape(str(e))}", parse_mode="HTML")
                except Exception:
                    await msg.reply_text(f"❌ Error generating chart for #{resolved}: {html.escape(str(e))}")

        elif cmd_type == "test_pump":
            raw_sym = command.get("symbol") or "PNTUSDT"
            resolved = price_fetcher.resolve_symbol(raw_sym) if not raw_sym.upper().startswith("TEST") else "TESTUSDT"

            status_msg = await msg.reply_text(f"🧪 Generating simulated VIP pump alert for <b>#{resolved}</b>...", parse_mode="HTML")
            try:
                test_pct = 45.23
                test_price = 0.0350
                test_high = 0.0350
                test_low = 0.0215
                test_vol = 261850.0
                test_multiplier = 1.6

                if resolved != "TESTUSDT":
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=6.0)
                            if resp.status_code == 200:
                                d = resp.json()
                                test_price = float(d.get("lastPrice", 0.0))
                                test_high = float(d.get("highPrice", test_price * 1.05))
                                test_low = float(d.get("lowPrice", test_price * 0.70))
                                test_vol = float(d.get("quoteVolume", 500000.0))
                                real_pct = float(d.get("priceChangePercent", 0.0))
                                test_pct = real_pct if real_pct >= 20.0 else 52.40
                                test_multiplier = 2.4
                    except Exception as fe:
                        logger.debug(f"Ticker fetch failed for test {resolved}, using default test values: {fe}")

                market_type = await price_fetcher.get_market_type(resolved)

                card_bytes, _ = await card_service.generate_combined_alert(
                    symbol=resolved,
                    current_pct=test_pct,
                    current_price=test_price,
                    high_24h=test_high,
                    low_24h=test_low,
                    volume_24h=test_vol,
                    multiplier=test_multiplier,
                    market_type=market_type
                )

                card_caption = gainer_service.format_pump_alert(
                    symbol=resolved,
                    current_pct=test_pct,
                    current_price=test_price,
                    high_24h=test_high,
                    low_24h=test_low,
                    volume_24h=test_vol,
                    multiplier=test_multiplier,
                    market_type=market_type
                )

                if card_bytes:
                    await msg.reply_photo(
                        photo=card_bytes,
                        caption=card_caption,
                        parse_mode="HTML"
                    )
                else:
                    await msg.reply_text(card_caption, parse_mode="HTML")
                try:
                    await status_msg.delete()
                except Exception:
                    pass

                # Dispatch test copy to TARGET_CHAT_ID to test end-to-end integration
                target_chat = gainer_service.TARGET_CHAT_ID
                if target_chat and target_chat != msg.chat.id:
                    try:
                        if card_bytes:
                            card_bytes.seek(0)
                            await context.bot.send_photo(
                                chat_id=target_chat,
                                photo=card_bytes,
                                caption=card_caption,
                                parse_mode="HTML"
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=target_chat,
                                text=card_caption,
                                parse_mode="HTML"
                            )
                        await msg.reply_text(f"✅ Successfully dispatched test pump alert to target channel (<code>{target_chat}</code>)!", parse_mode="HTML")
                    except Exception as te:
                        await msg.reply_text(f"⚠️ Warning: Could not post to target channel (<code>{target_chat}</code>): <i>{html.escape(str(te))}</i>", parse_mode="HTML")

            except Exception as e:
                logger.error(f"Error executing test_pump command: {e}", exc_info=True)
                try:
                    await status_msg.edit_text(f"❌ Error generating test alert: {html.escape(str(e))}", parse_mode="HTML")
                except Exception:
                    await msg.reply_text(f"❌ Error generating test alert: {html.escape(str(e))}")

        elif cmd_type == "test_dump":
            raw_sym = command.get("symbol") or "LUNAUSDT"
            resolved = price_fetcher.resolve_symbol(raw_sym) if not raw_sym.upper().startswith("TEST") else "TESTUSDT"

            status_msg = await msg.reply_text(f"🧪 Generating simulated VIP dump alert for <b>#{resolved}</b>...", parse_mode="HTML")
            try:
                test_pct = -34.50
                test_price = 0.3850
                test_high = 0.5890
                test_low = 0.3620
                test_vol = 85200000.0
                test_multiplier = 2.7

                if resolved != "TESTUSDT":
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=6.0)
                            if resp.status_code == 200:
                                d = resp.json()
                                test_price = float(d.get("lastPrice", 0.0))
                                test_high = float(d.get("highPrice", test_price * 1.45))
                                test_low = float(d.get("lowPrice", test_price * 0.95))
                                test_vol = float(d.get("quoteVolume", 1500000.0))
                                real_pct = float(d.get("priceChangePercent", 0.0))
                                test_pct = real_pct if real_pct <= -20.0 else -35.80
                                test_multiplier = 2.5
                    except Exception as fe:
                        logger.debug(f"Ticker fetch failed for test {resolved}, using default test values: {fe}")

                market_type = await price_fetcher.get_market_type(resolved)

                card_bytes, _ = await card_service.generate_combined_alert(
                    symbol=resolved,
                    current_pct=test_pct,
                    current_price=test_price,
                    high_24h=test_high,
                    low_24h=test_low,
                    volume_24h=test_vol,
                    multiplier=test_multiplier,
                    market_type=market_type
                )

                card_caption = gainer_service.format_pump_alert(
                    symbol=resolved,
                    current_pct=test_pct,
                    current_price=test_price,
                    high_24h=test_high,
                    low_24h=test_low,
                    volume_24h=test_vol,
                    multiplier=test_multiplier,
                    market_type=market_type
                )

                if card_bytes:
                    await msg.reply_photo(
                        photo=card_bytes,
                        caption=card_caption,
                        parse_mode="HTML"
                    )
                else:
                    await msg.reply_text(card_caption, parse_mode="HTML")
                try:
                    await status_msg.delete()
                except Exception:
                    pass

                # Dispatch test copy to TARGET_CHAT_ID to test end-to-end integration
                target_chat = gainer_service.TARGET_CHAT_ID
                if target_chat and target_chat != msg.chat.id:
                    try:
                        if card_bytes:
                            card_bytes.seek(0)
                            await context.bot.send_photo(
                                chat_id=target_chat,
                                photo=card_bytes,
                                caption=card_caption,
                                parse_mode="HTML"
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=target_chat,
                                text=card_caption,
                                parse_mode="HTML"
                            )
                        await msg.reply_text(f"✅ Successfully dispatched test dump alert to target channel (<code>{target_chat}</code>)!", parse_mode="HTML")
                    except Exception as te:
                        await msg.reply_text(f"⚠️ Warning: Could not post to target channel (<code>{target_chat}</code>): <i>{html.escape(str(te))}</i>", parse_mode="HTML")

            except Exception as e:
                logger.error(f"Error executing test_dump command: {e}", exc_info=True)
                try:
                    await status_msg.edit_text(f"❌ Error generating test alert: {html.escape(str(e))}", parse_mode="HTML")
                except Exception:
                    await msg.reply_text(f"❌ Error generating test alert: {html.escape(str(e))}")

        elif cmd_type == "set_gainer_threshold":
            percent = command["percent"]
            db.set_setting("gainer_threshold", str(percent))
            response = (
                f"✅ *Gainer Alert Threshold Updated!*\n\n"
                f"Automatic scanner will now trigger for assets gaining *≥{percent:.0f}%* in 24 hours."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "set_dump_threshold":
            percent = abs(command["percent"])
            db.set_setting("dump_threshold", str(percent))
            response = (
                f"✅ *Dump Alert Threshold Updated!*\n\n"
                f"Automatic scanner will now trigger for assets dropping *≤-{percent:.0f}%* in 24 hours."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "gainer_scanner":
            status = command["status"]
            val = "1" if status == "ON" else "0"
            db.set_setting("gainer_scanner_enabled", val)
            response = (
                f"✅ *Gainer & Dump Background Scanner Updated!*\n\n"
                f"Automatic scanning and notifications are now *{status}*."
            )
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
                "5️⃣ <b>Set Recurring Update Alert</b>\n"
                "Set a recurring price update to be posted at intervals (in minutes):\n"
                "<code>/set_update BTC 2</code> or <code>/set_update BTC 5</code>\n"
                "<i>(Alternative: <code>CONFIG UPDATE BTC 2</code>)</i>\n\n"
                "6️⃣ <b>Remove Step Alert</b>\n"
                "Disable step tracking for a specific coin:\n"
                "<code>/remove_step BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_STEP BTC</code>)</i>\n\n"
                "7️⃣ <b>Remove Target Alert</b>\n"
                "Disable all active target price alerts for a specific coin:\n"
                "<code>/remove_target BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_TARGET BTC</code>)</i>\n\n"
                "8️⃣ <b>Remove Average Alert</b>\n"
                "Disable average price alerts for a specific coin:\n"
                "<code>/remove_avg_alert BTC HIGH</code> or <code>/remove_avg_alert BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_AVG_ALERT BTC LOW</code>)</i>\n\n"
                "9️⃣ <b>Remove Recurring Update Alert</b>\n"
                "Disable recurring price updates for a specific coin:\n"
                "<code>/remove_update BTC</code>\n"
                "<i>(Alternative: <code>CONFIG REMOVE_UPDATE BTC</code>)</i>\n\n"
                "1️⃣0️⃣ <b>Check Status</b>\n"
                "Show active targets, step configurations, average alerts and current prices:\n"
                "<code>/status</code> or <code>/status BTC</code>\n"
                "<i>(Alternative: <code>CONFIG STATUS BTC</code>)</i>\n\n"
                "1️⃣1️⃣ <b>Short Reversion Signal Evaluation</b>\n"
                "Evaluate confluences and get entry/exit short signals on demand:\n"
                "<code>/short_status BTC</code> or <code>/evaluate ETH</code>\n"
                "<i>(Alternative: <code>CONFIG SHORT_STATUS BTC</code>)</i>\n\n"
                "1️⃣2️⃣ <b>Market Analysis & Quantitative Risk Audit</b>\n"
                "Run quantitative risk audit, squeeze detection, resistance levels & VIP candlestick chart:\n"
                "<code>/analyze BRUSDT</code> or <code>/analyze BTC</code>\n"
                "<i>(Alternative: <code>/analysis ETH</code> or <code>CONFIG ANALYZE BTC</code>)</i>\n\n"
                "1️⃣3️⃣ <b>24h Gainer & Dump Scanner Settings</b>\n"
                "Get current top gainers list:\n"
                "<code>/gainers</code> or <code>/gainers 30</code>\n"
                "Get current top losers list:\n"
                "<code>/losers</code> or <code>/losers 20</code>\n"
                "Set automatic alert threshold percentage:\n"
                "<code>/set_gainer_threshold 40</code>\n"
                "<code>/set_dump_threshold 30</code>\n"
                "Toggle background scanner ON or OFF:\n"
                "<code>/gainer_scanner ON</code> or <code>/gainer_scanner OFF</code>\n\n"
                "1️⃣4️⃣ <b>VIP Alert Cards & Charts</b>\n"
                "Generate live VIP pump alert card:\n"
                "<code>/pump VTHO</code>\n"
                "Generate live VIP dump alert card:\n"
                "<code>/dump LUNA</code>\n"
                "Generate live 15M candlestick breakdown chart:\n"
                "<code>/chart VTHO</code> or <code>/chart LUNA</code>\n\n"
                "1️⃣5️⃣ <b>Test Alert Integrations</b>\n"
                "Simulate mock pump alert and test Telegram target channel integration:\n"
                "<code>/test_pump</code> or <code>/test_pump BTC</code>\n"
                "Simulate mock dump alert and test Telegram target channel integration:\n"
                "<code>/test_dump</code> or <code>/test_dump LUNA</code>\n\n"
                "1️⃣6️⃣ <b>Help Instructions</b>\n"
                "Display this help message:\n"
                "<code>/help</code> or <code>/start</code>\n"
                "<i>(Alternative: <code>CONFIG HELP</code>)</i>"
            )
            await msg.reply_text(help_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error handling admin command: {e}", exc_info=True)
        try:
            await msg.reply_text(f"❌ Error processing command: {str(e)}")
        except Exception as reply_err:
            logger.error(f"Failed to reply with error message: {reply_err}")
