import os
import sys
import asyncio
import argparse
from dotenv import load_dotenv

# Ensure local modules can be found
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

load_dotenv(dotenv_path=os.path.join(BASE_DIR, '.env'))

import httpx
import card_service
import gainer_service
import price_fetcher

async def main():
    parser = argparse.ArgumentParser(description="Test and simulate a VIP Pump/Dump Alert to verify Telegram channel integrations.")
    parser.add_argument("symbol", nargs="?", default="TESTUSDT", help="Trading pair to test (default: TESTUSDT, or e.g. BTC, PNT, VTHO, LUNA)")
    parser.add_argument("--dump", action="store_true", help="Simulate a 24h Downside Dump Alert (red theme) instead of a Pump Alert")
    parser.add_argument("--preview", action="store_true", help="Only generate and save PNG preview locally without posting to Telegram")
    parser.add_argument("--chat_id", type=str, default=None, help="Override target chat ID for this test")
    parser.add_argument("--me", action="store_true", help="Send to personal ME channel/chat configured via _ME environment variables")
    args = parser.parse_args()

    raw_symbol = args.symbol.upper()
    resolved = price_fetcher.resolve_symbol(raw_symbol) if raw_symbol != "TESTUSDT" else "TESTUSDT"

    alert_type = "dump" if args.dump else "pump"
    print(f"🧪 Generating test {alert_type} data for #{resolved}...")

    # Default simulation data
    if args.dump:
        test_pct = -34.50
        test_price = 0.3850
        test_high = 0.5890
        test_low = 0.3620
        test_vol = 85200000.0
        test_multiplier = 2.7
    else:
        test_pct = 45.23
        test_price = 0.0350
        test_high = 0.0350
        test_low = 0.0215
        test_vol = 261850.0
        test_multiplier = 1.6

    # If user provided a real coin symbol, fetch its actual price and simulate
    if resolved != "TESTUSDT":
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=8.0)
                if resp.status_code == 200:
                    d = resp.json()
                    test_price = float(d.get("lastPrice", test_price))
                    real_pct = float(d.get("priceChangePercent", 0.0))
                    test_vol = float(d.get("quoteVolume", test_vol))
                    
                    if args.dump:
                        test_high = float(d.get("highPrice", test_price * 1.45))
                        test_low = float(d.get("lowPrice", test_price * 0.95))
                        test_pct = real_pct if real_pct <= -20.0 else -35.80
                        test_multiplier = 2.5
                    else:
                        test_high = float(d.get("highPrice", test_price * 1.08))
                        test_low = float(d.get("lowPrice", test_price * 0.75))
                        test_pct = real_pct if real_pct >= 20.0 else 48.65
                        test_multiplier = 2.4
                    print(f"ℹ️ Loaded live Binance ticker for #{resolved}: ${test_price}")
        except Exception as e:
            print(f"⚠️ Could not fetch live ticker: {e}. Using simulated values.")

    # Resolve market type (Spot vs Futures)
    market_type = await price_fetcher.get_market_type(resolved)
    print(f"🏷️ Market classification: {market_type}")

    # Generate Combined VIP Card + 15M Chart
    print(f"🎨 Rendering VIP {alert_type.capitalize()} Alert Card + 15M Candlestick Chart graphic...")
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

    caption = gainer_service.format_pump_alert(
        symbol=resolved,
        current_pct=test_pct,
        current_price=test_price,
        high_24h=test_high,
        low_24h=test_low,
        volume_24h=test_vol,
        multiplier=test_multiplier,
        market_type=market_type
    )

    # Local file preview
    preview_filename = f"test_{alert_type}_preview.png"
    preview_file = os.path.join(BASE_DIR, preview_filename)
    with open(preview_file, "wb") as f:
        f.write(card_bytes.getvalue())
    print(f"✅ Saved local image preview to: {preview_file}")

    if args.preview:
        print("\n--- Alert Caption Preview ---")
        print(caption)
        print("----------------------------")
        import analyst_service
        eval_data = {
            "symbol": resolved if resolved != "TESTUSDT" else "BTCUSDT",
            "priceChangePercent": test_pct,
            "lastPrice": test_price,
            "highPrice": test_high,
            "lowPrice": test_low,
            "quoteVolume": test_vol
        }
        eval_result = await analyst_service.evaluate_gainer(eval_data)
        if resolved == "TESTUSDT":
            eval_result["ticker"] = "TESTUSDT"
            eval_result["telegram_alert"] = analyst_service.format_quantitative_risk_alert(eval_result)
        print("\n--- Quantitative Risk & Stop-Loss Preview ---")
        print(eval_result.get("telegram_alert"))
        print("---------------------------------------------")
        print("✨ Done (--preview mode: not sending to Telegram).")
        return

    # Telegram Credentials & Destination Selection
    target_me_chat = os.getenv("TARGET_CHAT_ID_ME") or os.getenv("TARGET_CHAT_ME")
    token_me = os.getenv("TELEGRAM_BOT_TOKEN_ME") or os.getenv("BOT_TOKEN_ME")

    if args.me or (target_me_chat and not args.chat_id):
        target_chat = args.chat_id or target_me_chat
        bot_token = token_me or os.getenv("TELEGRAM_BOT_TOKEN")
        is_me_destination = True
        print(f"👤 Target mode: Personal 'ME' channel ({target_chat})")
    else:
        target_chat = args.chat_id or os.getenv("TARGET_CHAT_ID")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        is_me_destination = False
        print(f"📢 Target mode: Main channel ({target_chat})")

    if not bot_token or bot_token == "your_telegram_bot_token_here":
        print("\n⚠️ Telegram bot token not set in .env! Cannot dispatch to Telegram.")
        print(f"However, your card was successfully rendered and saved to: {preview_file}")
        return

    if not target_chat or target_chat == "your_target_channel_or_chat_id_here":
        print("\n⚠️ Target chat ID not set in .env! Cannot dispatch to Telegram.")
        print(f"However, your card was successfully rendered and saved to: {preview_file}")
        return

    print(f"\n🚀 Dispatching test pump alert card to Telegram chat: {target_chat}...")
    try:
        from telegram import Bot
        bot = Bot(token=bot_token)
        card_bytes.seek(0)
        sent_msg = await bot.send_photo(
            chat_id=target_chat,
            photo=card_bytes,
            caption=caption,
            parse_mode="HTML"
        )
        print(f"🎉 SUCCESS! Test pump alert posted to {target_chat} (message ID: {sent_msg.message_id})")
        
        # Dispatch Quantitative Risk Audit (stop-loss, resistances, squeeze flags)
        print(f"\n🔍 Running Quantitative Derivatives Risk & Stop-Loss Audit for #{resolved}...")
        import analyst_service
        eval_data = {
            "symbol": resolved if resolved != "TESTUSDT" else "BTCUSDT",
            "priceChangePercent": test_pct,
            "lastPrice": test_price,
            "highPrice": test_high,
            "lowPrice": test_low,
            "quoteVolume": test_vol
        }
        eval_result = await analyst_service.evaluate_gainer(eval_data)
        if resolved == "TESTUSDT":
            eval_result["ticker"] = "TESTUSDT"
            eval_result["telegram_alert"] = analyst_service.format_quantitative_risk_alert(eval_result)
            
        risk_alert_msg = eval_result.get("telegram_alert")
        if risk_alert_msg:
            print(f"🚀 Dispatching Quantitative Risk & Stop-Loss Report to Telegram chat: {target_chat}...")
            try:
                sent_risk = await bot.send_message(
                    chat_id=target_chat,
                    text=risk_alert_msg,
                    parse_mode="HTML"
                )
                print(f"🎉 SUCCESS! Risk audit report posted to {target_chat} (message ID: {sent_risk.message_id})")
            except Exception as re:
                print(f"⚠️ HTML send failed ({re}), trying plain text fallback...")
                import re as regex
                clean_text = regex.sub(r'<[^>]+>', '', risk_alert_msg)
                sent_risk = await bot.send_message(chat_id=target_chat, text=clean_text)
                print(f"🎉 SUCCESS! Fallback plain text risk audit posted to {target_chat}")

    except Exception as e:
        print(f"❌ Failed to dispatch to Telegram: {e}")
        print("Tip: Make sure your bot is added as an administrator in the target channel.")

if __name__ == "__main__":
    asyncio.run(main())
