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
    parser = argparse.ArgumentParser(description="Test and simulate a VIP Pump Alert to verify Telegram channel integrations.")
    parser.add_argument("symbol", nargs="?", default="TESTUSDT", help="Trading pair to test (default: TESTUSDT, or e.g. BTC, PNT, VTHO)")
    parser.add_argument("--preview", action="store_true", help="Only generate and save PNG preview locally without posting to Telegram")
    parser.add_argument("--chat_id", type=str, default=None, help="Override target chat ID for this test")
    args = parser.parse_args()

    raw_symbol = args.symbol.upper()
    resolved = price_fetcher.resolve_symbol(raw_symbol) if raw_symbol != "TESTUSDT" else "TESTUSDT"

    print(f"🧪 Generating test pump data for #{resolved}...")

    # Default simulation data
    test_pct = 45.23
    test_price = 0.0350
    test_high = 0.0350
    test_low = 0.0215
    test_vol = 261850.0
    test_multiplier = 1.6

    # If user provided a real coin symbol, fetch its actual price and simulate a pump surge
    if resolved != "TESTUSDT":
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={resolved}", timeout=8.0)
                if resp.status_code == 200:
                    d = resp.json()
                    test_price = float(d.get("lastPrice", test_price))
                    test_high = float(d.get("highPrice", test_price * 1.08))
                    test_low = float(d.get("lowPrice", test_price * 0.75))
                    test_vol = float(d.get("quoteVolume", test_vol))
                    real_pct = float(d.get("priceChangePercent", 0.0))
                    test_pct = real_pct if real_pct >= 20.0 else 48.65
                    test_multiplier = 2.4
                    print(f"ℹ️ Loaded live Binance ticker for #{resolved}: ${test_price}")
        except Exception as e:
            print(f"⚠️ Could not fetch live ticker: {e}. Using simulated values.")

    # Generate Combined VIP Card + 15M Chart
    print("🎨 Rendering VIP Pump Alert Card + 15M Candlestick Chart graphic...")
    card_bytes, _ = await card_service.generate_combined_alert(
        symbol=resolved,
        current_pct=test_pct,
        current_price=test_price,
        high_24h=test_high,
        low_24h=test_low,
        volume_24h=test_vol,
        multiplier=test_multiplier
    )

    caption = gainer_service.format_pump_alert(
        symbol=resolved,
        current_pct=test_pct,
        current_price=test_price,
        high_24h=test_high,
        low_24h=test_low,
        volume_24h=test_vol,
        multiplier=test_multiplier
    )

    # Local file preview
    preview_file = os.path.join(BASE_DIR, "test_pump_preview.png")
    with open(preview_file, "wb") as f:
        f.write(card_bytes.getvalue())
    print(f"✅ Saved local image preview to: {preview_file}")

    if args.preview:
        print("\n--- Alert Caption Preview ---")
        print(caption)
        print("----------------------------")
        print("✨ Done (--preview mode: not sending to Telegram).")
        return

    # Send to Telegram
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    target_chat = args.chat_id or os.getenv("TARGET_CHAT_ID")

    if not bot_token or bot_token == "your_telegram_bot_token_here":
        print("\n⚠️ TELEGRAM_BOT_TOKEN not set in .env! Cannot dispatch to Telegram.")
        print(f"However, your card was successfully rendered and saved to: {preview_file}")
        return

    if not target_chat or target_chat == "your_target_channel_or_chat_id_here":
        print("\n⚠️ TARGET_CHAT_ID not set in .env! Cannot dispatch to Telegram.")
        print(f"However, your card was successfully rendered and saved to: {preview_file}")
        return

    print(f"\n🚀 Dispatching test pump alert to Telegram chat/channel: {target_chat}...")
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
    except Exception as e:
        print(f"❌ Failed to dispatch to Telegram: {e}")
        print("Tip: Make sure your bot is added as an administrator in the target channel.")

if __name__ == "__main__":
    asyncio.run(main())
