import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import logging
from typing import Any
from telegram import Update
from telegram.ext import ContextTypes

import db
import price_fetcher
import kline_service
import gainer_service
from config_parser import parse_message_command

logger = logging.getLogger(__name__)

# Load configurations
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID")
if not ADMIN_CHAT_ID_RAW:
    raise ValueError("ADMIN_CHAT_ID is not configured in the environment variables.")

MARKET_ANALYZER_URL = os.getenv("MARKET_ANALYZER_URL", "http://127.0.0.1:8000/api/v1/analyze")
MARKET_ANALYZER_API_KEY = os.getenv("MARKET_ANALYZER_API_KEY", "lF2vIg=Pik8S_(^iC8$23H&fBz$4h7L6-rOCiZ*x&a#bjvp+(fF_9hTII5aul@gq")

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

        elif cmd_type == "short_status":
            import httpx
            import analyst_service
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)
            
            # Fetch price to verify symbol validity
            prices = await price_fetcher.fetch_prices([resolved])
            if resolved not in prices:
                await msg.reply_text(f"❌ Could not find price for *{user_symbol}* on Binance.", parse_mode="Markdown")
                return
            
            status_msg = await msg.reply_text(f"⏳ Querying data and evaluating short reversion signal for *{user_symbol}* ({resolved})...", parse_mode="Markdown")
            
            try:
                ticker_24h = {}
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=10.0)
                    if resp.status_code == 200:
                        t_data = resp.json()
                        ticker_24h = {
                            "symbol": resolved,
                            "priceChangePercent": float(t_data.get("priceChangePercent", 0.0)),
                            "lastPrice": float(t_data.get("lastPrice", 0.0)),
                            "highPrice": float(t_data.get("highPrice", 0.0)),
                            "lowPrice": float(t_data.get("lowPrice", 0.0)),
                            "quoteVolume": float(t_data.get("quoteVolume", 0.0))
                        }
                
                if not ticker_24h:
                    current_price = prices[resolved]
                    ticker_24h = {
                        "symbol": resolved,
                        "priceChangePercent": 0.0,
                        "lastPrice": current_price,
                        "highPrice": current_price,
                        "lowPrice": current_price,
                        "quoteVolume": 0.0
                    }
                    
                eval_result = await analyst_service.evaluate_gainer(ticker_24h)
                alert_text = eval_result.get("telegram_alert")
                if alert_text:
                    await status_msg.edit_text(alert_text, parse_mode="Markdown")
                else:
                    await status_msg.edit_text(f"❌ Evaluation returned empty alert for *{user_symbol}*.", parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Error evaluating {resolved} on short_status command: {e}", exc_info=True)
                await status_msg.edit_text(f"❌ Error during evaluation of *{user_symbol}*: {str(e)}", parse_mode="Markdown")

        elif cmd_type == "analyze":
            import httpx
            user_symbol = command["symbol"]
            resolved = price_fetcher.resolve_symbol(user_symbol)

            # Check if resolved is in watched or just look up on Binance to make sure it's valid
            prices = await price_fetcher.fetch_prices([resolved])
            if resolved not in prices:
                await msg.reply_text(f"❌ Could not find price for *{user_symbol}* on Binance.", parse_mode="Markdown")
                return

            status_msg = await msg.reply_text(f"⏳ Running market analysis for *{user_symbol}* ({resolved})...", parse_mode="Markdown")

            def format_for_analyzer(resolved_symbol: str) -> str:
                # standard quote assets
                for suffix in ("USDT", "USDC", "BUSD", "BTC", "ETH", "EUR", "TRY", "FDUSD"):
                    if resolved_symbol.endswith(suffix) and len(resolved_symbol) > len(suffix):
                        base = resolved_symbol[:-len(suffix)]
                        return f"{base}/{suffix}"
                return resolved_symbol

            formatted_sym = format_for_analyzer(resolved)

            payload = {
                "symbol": formatted_sym,
                "exchange": "binance",
                "timeframes": ["15m", "1h", "4h"],
                "include_reasoning": True,
                "force_refresh": False
            }

            headers = {
                "X-API-Key": MARKET_ANALYZER_API_KEY
            }

            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(MARKET_ANALYZER_URL, json=payload, headers=headers, timeout=20.0)
                    if resp.status_code == 403:
                        await status_msg.edit_text("❌ *Authorization Error*: Invalid X-API-Key configured for the Market Analyzer API.", parse_mode="Markdown")
                        return
                    elif resp.status_code != 200:
                        await status_msg.edit_text(f"❌ *API Error*: Server returned status code {resp.status_code}.", parse_mode="Markdown")
                        return

                    data = resp.json()

                    # Extract values safely
                    symbol_res = data.get("symbol", formatted_sym)
                    regime = data.get("market_regime", "N/A")
                    reasoning = data.get("reasoning_summary", "N/A")
                    risk_warning = data.get("risk_warning", "N/A")
                    exec_time = data.get("execution_time_ms", 0) / 1000.0

                    signals = data.get("microstructure_signals", {})
                    liq_hunt = "True" if signals.get("liquidity_hunt_detected") else "False"
                    liq_type = signals.get("liquidity_hunt_type", "NONE")
                    pump_risk = signals.get("pump_risk_level", "LOW")
                    sentiment = signals.get("funding_oi_sentiment", "Neutral")
                    imbalance = signals.get("orderbook_imbalance_ratio", 0.0)

                    rec = data.get("trade_recommendation", {})
                    rec_action = rec.get("action", "HOLD")
                    rec_lev = rec.get("recommended_leverage_max", 1)
                    rec_rr = rec.get("risk_reward_ratio", 0.0)

                    preds = data.get("predictions", {})

                    def format_pred(pred_data):
                        if not pred_data:
                            return "_No prediction data available_"
                        bias = pred_data.get("bias", "NEUTRAL")
                        conf = pred_data.get("confidence", 0.0) * 100
                        target = pred_data.get("target_price")
                        invalidation = pred_data.get("invalidation_price")
                        driver = pred_data.get("key_driver", "N/A")

                        target_str = format_price(target) if target else "N/A"
                        inv_str = format_price(invalidation) if invalidation else "N/A"

                        return (
                            f"`{bias}` (Confidence: {conf:.1f}%)\n"
                            f"  - Target: *{target_str}* | Invalid: *{inv_str}*\n"
                            f"  - Key Driver: {driver}"
                        )

                    pred_15m = format_pred(preds.get("next_15m"))
                    pred_1h = format_pred(preds.get("next_1h"))
                    pred_4h = format_pred(preds.get("next_4h"))

                    analysis_response = (
                        f"📊 *Market Analysis: {symbol_res}* 📊\n"
                        f"🕒 *Execution:* {exec_time:.2f}s\n\n"
                        f"📈 *Market Regime:* `{regime}`\n\n"
                        f"🔍 *Microstructure Signals:*\n"
                        f"• Liquidity Hunt: *{liq_hunt}* (Type: `{liq_type}`)\n"
                        f"• Pump Risk Level: *{pump_risk}*\n"
                        f"• Funding/OI Sentiment: *{sentiment}*\n"
                        f"• Orderbook Imbalance: *{imbalance:.2f}*\n\n"
                        f"💡 *Trade Recommendation:*\n"
                        f"• Action: *{rec_action}*\n"
                        f"• Max Leverage: *{rec_lev}x*\n"
                        f"• Risk/Reward Ratio: *{rec_rr:.2f}*\n\n"
                        f"🔮 *Predictions:*\n"
                        f"• *15m Bias:* {pred_15m}\n"
                        f"• *1h Bias:* {pred_1h}\n"
                        f"• *4h Bias:* {pred_4h}\n\n"
                        f"📝 *Reasoning Summary:*\n"
                        f"_{reasoning}_\n\n"
                        f"⚠️ *Risk Warning:*\n"
                        f"_{risk_warning}_"
                    )

                    await status_msg.edit_text(analysis_response, parse_mode="Markdown")

            except Exception as e:
                logger.error(f"Error querying market analyzer for {resolved}: {e}", exc_info=True)
                await status_msg.edit_text(f"❌ *Connection Error*: Failed to connect to Market Analyzer API at {MARKET_ANALYZER_URL}.\nDetails: `{str(e)}`", parse_mode="Markdown")

        elif cmd_type == "gainers":
            min_percent = command.get("min_percent")
            if min_percent is None:
                try:
                    min_percent = float(db.get_setting("gainer_threshold", "50.0"))
                except ValueError:
                    min_percent = 50.0
                    
            await msg.reply_text(f"⏳ Querying Binance 24h market stats for pairs >={min_percent}% gain...")
            
            tickers = await gainer_service.fetch_24h_tickers()
            if not tickers:
                await msg.reply_text("❌ Failed to fetch market stats from Binance.")
                return

            gainers = gainer_service.filter_gainers(tickers, min_percent)
            if not gainers:
                await msg.reply_text(f"No USDT trading pairs met the >={min_percent}% gain criteria.")
                return

            # Display top 15
            top_gainers = gainers[:15]
            response = f"🚀 *24-Hour Top Gainers (>={min_percent}%)*\n\n"
            for g in top_gainers:
                sym = g["symbol"]
                change = g["priceChangePercent"]
                price = g["lastPrice"]
                vol = g["quoteVolume"]
                response += f"• *{sym}*: +{change:.1f}% | Price: {gainer_service.format_price(price)} | Vol: {gainer_service.format_volume(vol)}\n"
                
            if len(gainers) > 15:
                response += f"\n_(showing top 15 out of {len(gainers)} total gainers above {min_percent}%)_"
                
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "set_gainer_threshold":
            percent = command["percent"]
            db.set_setting("gainer_threshold", str(percent))
            response = (
                f"✅ *Gainer Alert Threshold Updated!*\n\n"
                f"Automatic scanner will now trigger for assets gaining *>{percent}%* in 24 hours."
            )
            await msg.reply_text(response, parse_mode="Markdown")

        elif cmd_type == "gainer_scanner":
            status = command["status"]
            val = "1" if status == "ON" else "0"
            db.set_setting("gainer_scanner_enabled", val)
            response = (
                f"✅ *Gainer Background Scanner Updated!*\n\n"
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
                "1️⃣2️⃣ <b>Market Analysis</b>\n"
                "Query local market analyzer for regime, microstructure, trade rec and predictions:\n"
                "<code>/analyze BTC</code> or <code>/analysis ETH</code>\n"
                "<i>(Alternative: <code>CONFIG ANALYZE BTC</code>)</i>\n\n"
                "1️⃣3️⃣ <b>24h Gainer Scanner Settings</b>\n"
                "Get current top gainers list:\n"
                "<code>/gainers</code> or <code>/gainers 30</code>\n"
                "Set automatic alert threshold percentage:\n"
                "<code>/set_gainer_threshold 40</code>\n"
                "Toggle background scanner ON or OFF:\n"
                "<code>/gainer_scanner ON</code> or <code>/gainer_scanner OFF</code>\n\n"
                "1️⃣4️⃣ <b>Help Instructions</b>\n"
                "Display this help message:\n"
                "<code>/help</code> or <code>/start</code>\n"
                "<i>(Alternative: <code>CONFIG HELP</code>)</i>"
            )
            await msg.reply_text(help_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error handling admin command: {e}", exc_info=True)
        await msg.reply_text(f"❌ *Error processing command:* {str(e)}", parse_mode="Markdown")

import asyncio
