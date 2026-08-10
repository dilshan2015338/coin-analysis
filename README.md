# Telegram Crypto Alert Bot

A lightweight, modular, and dynamic Telegram Crypto Alert Bot written in Python. It polls prices from Binance Spot and Futures endpoints in real-time, monitors price levels against user-defined thresholds (both fixed absolute targets and step intervals), and updates its configuration dynamically by parsing control messages in an Admin/Control Channel.

---

## Features
- **Dynamic Configuration**: Change monitored coins, set fixed target alerts, or configure price step alerts directly via Telegram chat commands without restarting the service.
- **Support for Spot & Futures Coins**: Fetches from Binance Spot and Binance Futures. This covers standard spot assets (BTC, ETH, XRP, ADA) and futures-only assets (like Unibase - `UBUSDT`).
- **Data Persistence**: Uses a local SQLite database to persist watchlists, active step baseline prices, and target thresholds.
- **Robust Parsing**: Supports both Slash Commands and general key-value syntax (`CONFIG ...`).

---

## Project Structure
```
├── .env.example        # Environment variables configuration template
├── README.md           # Instructions, guides, and details
├── requirements.txt    # Application dependencies
├── main.py             # App entrypoint & Telegram Event Polling Loop
├── database.py         # SQLite connection layer and schema helpers
├── config_parser.py    # Command message parser using Regular Expressions
└── price_fetcher.py    # Concurrently fetches prices from Binance Spot & Futures
```

---

## Getting Started

### 1. Prerequisites
Make sure you have Python 3.8+ installed on your system.

### 2. Dependencies Installation
Install the required dependencies using `pip`:
```bash
pip install -r requirements.txt
```

### 3. Create Bot and Get Credentials
1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the instructions to create a bot. Note down the **HTTP API Token** (e.g., `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
3. Set your bot's privacy settings: By default, bots in groups/channels cannot see messages unless they start with `/` or the bot is designated as an administrator. We will add the bot as an administrator so it can read commands and post alerts.

### 4. Create and Configure Channels

You will need two Telegram channels (or groups/chats):
1. **Admin / Control Channel**: Where you type configuration commands (e.g., `/watch BTC, ETH`).
2. **Target Channel**: Where the bot posts alerts when target prices are crossed or step changes occur.

#### Adding Bot as Admin to Channels:
1. Open your channel (or create a new one).
2. Go to **Channel Info** -> **Administrators** (or **Manage Channel** -> **Administrators**).
3. Click **Add Administrator** and search for your bot's username.
4. Enable the following permissions:
   - **Post Messages** (Required for both channels: so the bot can acknowledge commands in the Admin channel and post alerts in the Target channel).
   - **Delete Messages** (Optional: allows cleaning up older messages if wanted).
5. Post a test message in the channel and copy the **Channel ID**. 
   *Note: Channel IDs typically start with `-100` (e.g., `-1002234567890`). You can find this ID by forwarding a post from the channel to `@ShowJsonBot` or using web Telegram clients.*

### 5. Setup Environment File
Copy the example environment file and fill in your credentials:
```bash
cp .env.example .env
```
Open `.env` and fill out:
- `TELEGRAM_BOT_TOKEN`: The HTTP API token from BotFather.
- `ADMIN_CHAT_ID`: The ID of your control channel (include the `-100` prefix).
- `TARGET_CHAT_ID`: The ID of your alert output channel (include the `-100` prefix).
- `POLL_INTERVAL`: Price polling rate in seconds (default is 10).

### 6. Run the Bot
Start the application:
```bash
python main.py
```

---

## Configuration Commands Syntax

Type these messages in your **Admin/Control Channel** to dynamically update configurations. The bot will reply with a confirmation message upon successful configuration.

| Command Action | Slash Command Example | Key-Value Alternative | Description |
| :--- | :--- | :--- | :--- |
| **Watch List** | `/watch XRP, ADA, UBUSDT, BTC` | `CONFIG WATCH XRP, ADA, UBUSDT, BTC` | Overwrites watched assets. Automatically resolves inputs (e.g., `BTC` -> `BTCUSDT`). Validates pricing on Binance before saving. |
| **Set Step** | `/set_step BTC 500` | `CONFIG STEP BTC 500` | Sets a recurring alert every time the price changes by $500 (up or down). Baseline price starts at the current market rate. |
| **Remove Step** | `/remove_step BTC` | `CONFIG REMOVE_STEP BTC` | Removes the recurring step threshold configuration for a specific coin. |
| **Set Target** | `/set_target BTC 65000 ABOVE` | `CONFIG TARGET BTC 65000` | Triggers a one-off alert when price crosses $65,000. If condition is omitted (`ABOVE` / `BELOW`), it is auto-detected relative to the current price. |
| **Remove Target** | `/remove_target BTC` | `CONFIG REMOVE_TARGET BTC` | Removes all active target price alerts for a specific coin. |
| **Check Status** | `/status` | `CONFIG STATUS` | Lists current watched asset prices, active target thresholds, and active step baselines. |
| **Help Instructions** | `/help` or `/start` | `CONFIG HELP` | Displays the help text listing all supported commands with examples. |

---

## Technical Details

### Price Resolution Logic
When you configure an asset, the bot normalizes the input:
- Removes slashes `/` and dashes `-` (e.g., `BTC/USDT` -> `BTCUSDT`).
- Converts characters to uppercase (e.g., `btc` -> `BTC`).
- If no quote asset suffix (`USDT`, `BTC`, `USDC`, `BUSD`, etc.) is present, it automatically appends `USDT` (e.g., `BTC` -> `BTCUSDT`).

### Evaluation Polling Loop
The bot operates in a non-blocking asynchronous event loop:
1. Fetches price tickers from the Binance Futures endpoint.
2. Evaluates active targets: Checks if `current_price >= target` (for `ABOVE`) or `current_price <= target` (for `BELOW`). Triggers are marked as disabled immediately upon alerting to prevent spam.
3. Evaluates step changes: Compares `|current_price - baseline_price|`. If it meets or exceeds the step interval threshold, it triggers an alert and updates the baseline to the current price.
