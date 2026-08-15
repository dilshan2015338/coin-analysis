import asyncio
import json
import logging
import math
from typing import List, Dict, Any, Tuple, Optional
import httpx

logger = logging.getLogger(__name__)

# Try importing the Google Antigravity SDK
try:
    from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig
    HAS_ANTIGRAVITY = True
except ImportError:
    HAS_ANTIGRAVITY = False
    logger.warning("google.antigravity SDK not found. Fallback/Mock mode enabled for local development.")

SYSTEM_INSTRUCTIONS = """
You are an expert Quantitative Cryptocurrency Futures Analyst and Trade Signal Generator.
Your objective is to analyze market data for top daily gainers and identify high-probability "Mean Reversion Short" (fade) signals once upward momentum shows clear structural exhaustion.

### TRADING RULES & CONFLUENCE MATRIX
You must ONLY emit an "ENTER_SHORT" signal when at least 3 of the following 4 criteria are met:

1. **Volume & Exhaustion Wick:** 
   - A distinct blow-off top on the 15m/1h timeframe (unusually large volume spike accompanied by a long upper shadow/wick indicating absorption).

2. **Market Structure Breakdown (CHoCH):** 
   - A clear Change of Character where price breaks below the most recent 5m or 15m Higher Low (HL) with strong sell volume, followed by a weak pullback forming a Lower High (LH).

3. **Momentum Divergence:** 
   - Bearish divergence on the 15m or 1h timeframe (Price forms a Higher High, while RSI or MACD forms a Lower High, with RSI exiting the >70 overbought zone).

4. **Derivative Confirmation:** 
   - Extremely positive Funding Rate (>0.05% per 8h), Open Interest (OI) dropping on the latest push, or CVD divergence showing spot buying has flatlined.

---

### DECISION PROTOCOL
For every coin data payload received:
1. **Analyze the Data:** Evaluate candle structure, recent highs/lows, RSI/indicators, and derivative metrics.
2. **Determine Action:**
   - If confluence criteria are MET -> Emit `ENTER_SHORT`.
   - If price is still in a parabolic expansion making Higher Highs without breakdown -> Emit `WAIT_FOR_EXHAUSTION`.
   - If momentum is strictly bullish continuation -> Emit `NO_TRADE`.

---

### OUTPUT FORMAT
You must respond strictly in valid JSON format matching this schema:
{
  "symbol": "COIN_USDT",
  "decision": "ENTER_SHORT" | "WAIT_FOR_EXHAUSTION" | "NO_TRADE",
  "confidence_score": 0-100,
  "reasons": [
    "Brief explanation of confluence 1",
    "Brief explanation of confluence 2"
  ],
  "trade_parameters": {
    "entry_range": [min_price, max_price],
    "invalidation_stop_loss": exact_price,
    "take_profit_targets": [tp1_price, tp2_price, tp3_price],
    "risk_reward_ratio": "1:X"
  },
  "telegram_alert": "Ready-to-post short summary for Telegram"
}
Do not return any surrounding markdown text, markdown code blocks, or additional explanation. Return only raw, valid JSON.
"""

# --- Technical Indicator Helpers ---

def calculate_rsi(prices: List[float], period: int = 14) -> List[float]:
    """Calculates Wilder's smoothed RSI in pure Python."""
    if len(prices) < period + 1:
        return [50.0] * len(prices)
    
    deltas = []
    for i in range(1, len(prices)):
        deltas.append(prices[i] - prices[i-1])
        
    rsi = [50.0] * len(prices)
    
    # First average gain/loss
    gains = 0.0
    losses = 0.0
    for i in range(period):
        d = deltas[i]
        if d > 0:
            gains += d
        else:
            losses -= d
            
    avg_gain = gains / period
    avg_loss = losses / period
    
    if avg_loss == 0:
        rsi[period] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))
        
    for i in range(period + 1, len(prices)):
        d = deltas[i-1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            rsi[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
            
    return rsi

def pearson_correlation(x: List[float], y: List[float]) -> float:
    """Computes Pearson Correlation Coefficient between two lists."""
    n = len(x)
    if n == 0 or len(y) != n:
        return 0.0
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    den_x = sum((x[i] - mean_x) ** 2 for i in range(n))
    den_y = sum((y[i] - mean_y) ** 2 for i in range(n))
    if den_x == 0.0 or den_y == 0.0:
        return 0.0
    return num / ((den_x * den_y) ** 0.5)

def detect_rsi_divergence(highs: List[float], rsi_values: List[float]) -> str:
    """
    Detects bearish RSI divergence.
    Looks for local high price peaks in the last 40 candles and compares with RSI peaks.
    """
    if len(highs) < 20 or len(rsi_values) < 20:
        return "Insufficient data"
    
    # Find local swing highs (peak high greater than adjacent 2 candles)
    peaks: List[int] = []
    for i in range(2, len(highs) - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2] and 
            highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            peaks.append(i)
            
    if len(peaks) < 2:
        return "None"
        
    # Get the 2 most recent peaks
    p1, p2 = peaks[-2], peaks[-1]
    
    # Bearish divergence: price forms Higher High (HH) while RSI forms Lower High (LH)
    if highs[p2] > highs[p1] and rsi_values[p2] < rsi_values[p1]:
        # Check if either peak was near/exiting overbought zone (>65)
        if rsi_values[p1] >= 65.0 or rsi_values[p2] >= 65.0:
            return f"Bearish Divergence (Price HH at {highs[p2]:.4f} vs {highs[p1]:.4f}, RSI LH at {rsi_values[p2]:.1f} vs {rsi_values[p1]:.1f})"
            
    return "None"

def detect_market_structure(lows: List[float], highs: List[float], closes: List[float]) -> str:
    """
    Detects Market Structure Breakdown (CHoCH).
    Looks for the most recent Higher Low (HL) and checks if subsequent close broke below it.
    """
    if len(lows) < 20:
        return "Insufficient data"
        
    # Find local swing lows (low lower than adjacent 2 candles)
    swing_lows: List[Tuple[int, float]] = []
    for i in range(2, len(lows) - 2):
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2] and 
            lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append((i, lows[i]))
            
    if len(swing_lows) < 2:
        return "Bullish continuation"
        
    # Check if swing lows were ascending (forming Higher Lows)
    hls = []
    for idx, (candle_idx, low_val) in enumerate(swing_lows):
        if idx == 0 or low_val > swing_lows[idx-1][1]:
            hls.append((candle_idx, low_val))
            
    if not hls:
        return "No clear Higher Low structure"
        
    # The most recent Higher Low (HL)
    hl_idx, hl_val = hls[-1]
    
    # Check if price closed below this HL in any candle after it
    broke = False
    breakdown_idx = -1
    for j in range(hl_idx + 1, len(closes)):
        if closes[j] < hl_val:
            broke = True
            breakdown_idx = j
            break
            
    if broke:
        # Check if we formed a Lower High (LH) after the breakdown
        lh_val = max(highs[breakdown_idx:])
        return f"Broken below {hl_val:.4f} HL, currently forming LH at {lh_val:.4f}"
        
    return "Bullish structure (Higher Lows holding)"

def detect_exhaustion_wick(highs: List[float], lows: List[float], opens: List[float], closes: List[float], volumes: List[float]) -> str:
    """
    Detects blow-off top / exhaustion wicks on large relative volume.
    """
    if len(highs) < 20:
        return "Insufficient data"
        
    # Find index of the highest high in the last 15 candles
    peak_idx = len(highs) - 15 + highs[-15:].index(max(highs[-15:]))
    if peak_idx < 10:
        return "None"
        
    high = highs[peak_idx]
    low = lows[peak_idx]
    op = opens[peak_idx]
    cl = closes[peak_idx]
    vol = volumes[peak_idx]
    
    # Average volume of 20 candles preceding the peak
    avg_vol = sum(volumes[peak_idx-10:peak_idx]) / 10.0
    if avg_vol == 0:
        avg_vol = 1.0
        
    upper_wick = high - max(op, cl)
    body = abs(cl - op)
    full_range = high - low
    
    # Check if volume is exceptionally high (>1.8x average) and upper shadow is long
    if vol >= 1.8 * avg_vol and upper_wick >= 1.5 * body and full_range > 0 and (upper_wick / full_range) >= 0.45:
        return f"Exhaustion Wick at {high:.4f} on {vol/avg_vol:.1f}x avg volume (wick: {upper_wick:.4f}, body: {body:.4f})"
        
    return "None"

def calculate_cvd_trend(closes: List[float], volumes: List[float], taker_buy_vols: List[float]) -> str:
    """
    Computes Cumulative Volume Delta (CVD) and compares price trend vs CVD trend.
    `delta = 2 * taker_buy_vol - total_vol`
    """
    if len(closes) < 15 or len(volumes) < 15 or len(taker_buy_vols) < 15:
        return "Insufficient data"
        
    cvd = 0.0
    cvd_series = []
    for i in range(len(volumes)):
        delta = 2 * taker_buy_vols[i] - volumes[i]
        cvd += delta
        cvd_series.append(cvd)
        
    # Check correlation between last 12 closes and CVD values
    corr = pearson_correlation(closes[-12:], cvd_series[-12:])
    
    # Check CVD trend on the last 5 candles
    recent_cvd_change = cvd_series[-1] - cvd_series[-5]
    recent_price_change = closes[-1] - closes[-5]
    
    if recent_price_change > 0 and recent_cvd_change <= 0:
        return f"Bearish CVD Divergence (Price rising, CVD flat/dropping, correlation: {corr:.2f})"
        
    return f"CVD trend aligned (correlation: {corr:.2f})"

# --- Binance Futures Data Fetcher ---

async def fetch_futures_data(symbol: str) -> Dict[str, Any]:
    """
    Fetches real-time futures metrics (klines, funding rate, open interest) from Binance.
    """
    base_url = "https://fapi.binance.com/fapi/v1"
    headers = {"Content-Type": "application/json"}
    
    async with httpx.AsyncClient() as client:
        # 1. Fetch 15m Klines (limit=80)
        k15_task = client.get(f"{base_url}/klines", params={"symbol": symbol, "interval": "15m", "limit": 80}, headers=headers, timeout=10.0)
        # 2. Fetch 1h Klines (limit=50)
        k1h_task = client.get(f"{base_url}/klines", params={"symbol": symbol, "interval": "1h", "limit": 50}, headers=headers, timeout=10.0)
        # 3. Fetch 5m Klines (limit=80)
        k5m_task = client.get(f"{base_url}/klines", params={"symbol": symbol, "interval": "5m", "limit": 80}, headers=headers, timeout=10.0)
        # 4. Fetch Premium Index / Funding Rate
        funding_task = client.get(f"{base_url}/premiumIndex", params={"symbol": symbol}, headers=headers, timeout=10.0)
        # 5. Fetch Open Interest History
        oi_task = client.get(f"{base_url}/openInterestHist", params={"symbol": symbol, "period": "5m", "limit": 12}, headers=headers, timeout=10.0)
        
        results = await asyncio.gather(k15_task, k1h_task, k5m_task, funding_task, oi_task, return_exceptions=True)
        
        data = {
            "klines_15m": [],
            "klines_1h": [],
            "klines_5m": [],
            "funding_rate": 0.0,
            "oi_trend": "Unknown"
        }
        
        # Parse 15m Klines
        r15 = results[0]
        if isinstance(r15, httpx.Response) and r15.status_code == 200:
            data["klines_15m"] = r15.json()
            
        # Parse 1h Klines
        r1h = results[1]
        if isinstance(r1h, httpx.Response) and r1h.status_code == 200:
            data["klines_1h"] = r1h.json()
            
        # Parse 5m Klines
        r5m = results[2]
        if isinstance(r5m, httpx.Response) and r5m.status_code == 200:
            data["klines_5m"] = r5m.json()
            
        # Parse Funding Rate
        rfund = results[3]
        if isinstance(rfund, httpx.Response) and rfund.status_code == 200:
            funding_data = rfund.json()
            if isinstance(funding_data, dict):
                data["funding_rate"] = float(funding_data.get("lastFundingRate", 0.0))
            elif isinstance(funding_data, list) and funding_data:
                data["funding_rate"] = float(funding_data[0].get("lastFundingRate", 0.0))
                
        # Parse Open Interest History
        roi = results[4]
        if isinstance(roi, httpx.Response) and roi.status_code == 200:
            oi_data = roi.json()
            if isinstance(oi_data, list) and len(oi_data) >= 2:
                try:
                    oi_first = float(oi_data[0].get("sumOpenInterest", 0.0))
                    oi_last = float(oi_data[-1].get("sumOpenInterest", 0.0))
                    if oi_last < oi_first * 0.98:
                        data["oi_trend"] = "Declining from peak"
                    elif oi_last > oi_first * 1.02:
                        data["oi_trend"] = "Rising"
                    else:
                        data["oi_trend"] = "Flat"
                except Exception:
                    pass
                    
        return data

# --- Structured Market Payload Constructor ---

def build_market_payload(symbol: str, ticker_24h: dict, futures_data: dict) -> dict:
    """
    Computes all indicators and constructs the finalized payload for the analyst agent.
    """
    current_price = ticker_24h.get("lastPrice", 0.0)
    pct_change = ticker_24h.get("priceChangePercent", 0.0)
    high_24h = ticker_24h.get("highPrice", 0.0)
    
    # 15m Indicators
    k15 = futures_data.get("klines_15m", [])
    t15_rsi = 50.0
    t15_divergence = "None"
    t15_structure = "None"
    t15_wick = "None"
    t15_cvd = "None"
    
    if k15:
        closes_15m = [float(c[4]) for c in k15]
        highs_15m = [float(c[2]) for c in k15]
        lows_15m = [float(c[3]) for c in k15]
        opens_15m = [float(c[1]) for c in k15]
        vols_15m = [float(c[5]) for c in k15]
        taker_vols_15m = [float(c[9]) for c in k15]
        
        rsi_series = calculate_rsi(closes_15m)
        t15_rsi = rsi_series[-1]
        t15_divergence = detect_rsi_divergence(highs_15m, rsi_series)
        t15_wick = detect_exhaustion_wick(highs_15m, lows_15m, opens_15m, closes_15m, vols_15m)
        t15_cvd = calculate_cvd_trend(closes_15m, vols_15m, taker_vols_15m)
        t15_structure = detect_market_structure(lows_15m, highs_15m, closes_15m)

    # 1h Indicators
    k1h = futures_data.get("klines_1h", [])
    t1h_rsi = 50.0
    t1h_divergence = "None"
    t1h_wick = "None"
    
    if k1h:
        closes_1h = [float(c[4]) for c in k1h]
        highs_1h = [float(c[2]) for c in k1h]
        lows_1h = [float(c[3]) for c in k1h]
        opens_1h = [float(c[1]) for c in k1h]
        vols_1h = [float(c[5]) for c in k1h]
        
        rsi_series_1h = calculate_rsi(closes_1h)
        t1h_rsi = rsi_series_1h[-1]
        t1h_divergence = detect_rsi_divergence(highs_1h, rsi_series_1h)
        t1h_wick = detect_exhaustion_wick(highs_1h, lows_1h, opens_1h, closes_1h, vols_1h)
        
    return {
        "symbol": symbol,
        "24h_change_pct": pct_change,
        "current_price": current_price,
        "recent_high": high_24h,
        "timeframe_15m": {
            "rsi": round(t15_rsi, 1),
            "rsi_divergence": t15_divergence,
            "market_structure": t15_structure,
            "volume_wick_exhaustion": t15_wick,
            "cvd_trend": t15_cvd
        },
        "timeframe_1h": {
            "rsi": round(t1h_rsi, 1),
            "rsi_divergence": t1h_divergence,
            "volume_wick_exhaustion": t1h_wick
        },
        "funding_rate": futures_data.get("funding_rate", 0.0),
        "open_interest_trend": futures_data.get("oi_trend", "Unknown")
    }

# --- Fallback Mock Agent for Development ---

def generate_mock_decision(payload: dict) -> dict:
    """
    Executes local quantitative evaluation of criteria and generates a valid mock JSON response.
    Used when the google.antigravity SDK is not available.
    """
    symbol = payload["symbol"]
    price = payload["current_price"]
    
    # Evaluate criteria count
    criteria_met = []
    
    # 1. Volume & Exhaustion Wick
    wick_15m = payload["timeframe_15m"]["volume_wick_exhaustion"]
    wick_1h = payload["timeframe_1h"]["volume_wick_exhaustion"]
    if wick_15m != "None" or wick_1h != "None":
        criteria_met.append("Volume & Exhaustion Wick: Large blow-off wick on volume spike detected.")
        
    # 2. Market Structure Breakdown (CHoCH)
    structure = payload["timeframe_15m"]["market_structure"]
    if "Broken below" in structure:
        criteria_met.append("Market Structure Breakdown (CHoCH): Price broke latest Higher Low and forms Lower High.")
        
    # 3. Momentum Divergence
    div_15m = payload["timeframe_15m"]["rsi_divergence"]
    div_1h = payload["timeframe_1h"]["rsi_divergence"]
    if div_15m != "None" or div_1h != "None" or payload["timeframe_15m"]["rsi"] > 70.0:
        criteria_met.append("Momentum Divergence: Bearish RSI divergence or overbought RSI levels.")
        
    # 4. Derivative Confirmation
    fr = payload["funding_rate"]
    oi = payload["open_interest_trend"]
    cvd = payload["timeframe_15m"]["cvd_trend"]
    if fr > 0.0005 or oi == "Declining from peak" or "Bearish CVD Divergence" in cvd:
        criteria_met.append(f"Derivative Confirmation: Funding rate: {fr*100:.3f}%, OI: {oi}, CVD: {cvd}.")

    # Make decision based on confluence
    confidence = 0
    decision = "NO_TRADE"
    
    if len(criteria_met) >= 3:
        decision = "ENTER_SHORT"
        confidence = min(60 + len(criteria_met) * 10, 95)
    elif len(criteria_met) == 2 or payload["timeframe_15m"]["rsi"] > 68.0:
        decision = "WAIT_FOR_EXHAUSTION"
        confidence = 50
    else:
        decision = "NO_TRADE"
        confidence = 20

    # Calculate stop loss (strictly above recent high wick with 1% buffer)
    stop_loss = round(payload["recent_high"] * 1.01, 4)
    tp1 = round(price * 0.95, 4)
    tp2 = round(price * 0.92, 4)
    tp3 = round(price * 0.88, 4)
    
    risk_reward = "1:2.5"
    if stop_loss > price:
        risk = stop_loss - price
        reward = price - tp2
        risk_reward = f"1:{reward / risk:.1f}"

    reasons = criteria_met if criteria_met else ["No clear bearish reversion triggers detected."]
    
    alert = (
        f"⚡ *CRITICAL SHORT ALERT: {symbol}* ⚡\n\n"
        f"• *Decision*: {decision} (Confidence: {confidence}%)\n"
        f"• *Current Price*: ${price:,.4f}\n"
        f"• *Entry Range*: ${price*0.995:,.4f} - ${price*1.005:,.4f}\n"
        f"• *Stop Loss*: ${stop_loss:,.4f} (Above recent high)\n"
        f"• *Take Profits*: ${tp1:,.4f} | ${tp2:,.4f} | ${tp3:,.4f}\n"
        f"• *Risk Reward*: {risk_reward}\n\n"
        f"🔍 *Confluences*:\n" + "\n".join([f"  - {r}" for r in reasons])
    )
    # Escape underscores to prevent Telegram Markdown parsing entities error
    alert = alert.replace('_', '\\_')

    return {
        "symbol": symbol,
        "decision": decision,
        "confidence_score": confidence,
        "reasons": reasons,
        "trade_parameters": {
            "entry_range": [round(price * 0.995, 4), round(price * 1.005, 4)],
            "invalidation_stop_loss": stop_loss,
            "take_profit_targets": [tp1, tp2, tp3],
            "risk_reward_ratio": risk_reward
        },
        "telegram_alert": alert
    }

# --- Main Evaluation Entrypoint ---

async def evaluate_gainer(ticker_data: dict) -> dict:
    """
    Evaluates a daily gainer ticker.
    Fetches futures data, builds payload, runs the AI analyst agent, and returns the signal decision.
    """
    symbol = ticker_data["symbol"]
    logger.info(f"Evaluating {symbol} for Mean Reversion Short signal...")
    
    try:
        # Fetch futures metrics
        futures_data = await fetch_futures_data(symbol)
        
        # Build quantitative market payload
        payload = build_market_payload(symbol, ticker_data, futures_data)
        logger.info(f"Market payload built for {symbol}: {json.dumps(payload, indent=2)}")
        
        if not HAS_ANTIGRAVITY:
            # Run local mock decision generator
            return generate_mock_decision(payload)
            
        # Run real Antigravity AI Agent
        config = LocalAgentConfig(
            system_instructions=SYSTEM_INSTRUCTIONS,
            capabilities=CapabilitiesConfig(),
        )
        
        prompt = f"""
        Analyze the following market data for {payload['symbol']}:
        {json.dumps(payload, indent=2)}
        """
        
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            
            # Extract raw response text handling stream vs string objects
            if hasattr(response, "text"):
                if asyncio.iscoroutinefunction(response.text):
                    raw_text = await response.text()
                else:
                    raw_text = response.text()
            else:
                tokens = []
                async for token in response:
                    tokens.append(token)
                raw_text = "".join(tokens)
                
            # Parse the returned JSON response
            raw_text = raw_text.strip()
            # Remove any markdown code wrappers if model returned them
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if lines[0].startswith("```json") or lines[0].startswith("```"):
                    raw_text = "\n".join(lines[1:-1]).strip()
                    
            result = json.loads(raw_text)
            if "telegram_alert" in result and isinstance(result["telegram_alert"], str):
                # Escape underscores to prevent Telegram Markdown parsing entities error
                result["telegram_alert"] = result["telegram_alert"].replace('_', '\\_')
            return result
            
    except Exception as e:
        logger.error(f"Error evaluating gainer {symbol} with agent: {e}", exc_info=True)
        # fallback to mock model on any errors
        try:
            return generate_mock_decision(payload)
        except Exception as mock_err:
            logger.error(f"Fallback mock generator also failed for {symbol}: {mock_err}")
            return {
                "symbol": symbol,
                "decision": "NO_TRADE",
                "confidence_score": 0,
                "reasons": [f"Evaluation error: {str(e)}"],
                "trade_parameters": {
                    "entry_range": [0, 0],
                    "invalidation_stop_loss": 0,
                    "take_profit_targets": [0, 0, 0],
                    "risk_reward_ratio": "1:0"
                },
                "telegram_alert": f"⚠️ Error evaluating {symbol}: {str(e)}".replace('_', '\\_')
            }
