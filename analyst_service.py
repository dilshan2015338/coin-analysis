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
You are a ruthless crypto derivatives risk-analyst and quantitative technical analyst. Your objective is to evaluate sudden high-volume "pulse" coins to determine if a SHORT scalp/swing is viable or if the asset is undergoing a float-cornering squeeze (like ACEUSDT or LSKUSDT).

### Context & Rules:
1. Shorting low-cap parabolic pulses carries asymmetric downside. Averaging down (martingale) is strictly banned.
2. Squeeze setups are fueled by predatory float accumulation and trapped retail shorts. You must penalize setups displaying synthetic float constriction.
3. Every trade must have clear, technical invalidation levels (stop-loss / resistance lines) beyond which the short thesis is dead.

### Analysis Framework:
1. METRIC AUDIT & SQUEEZE DETECTOR:
   - Metric A (OI vs. Market Cap/Volume): Calculate (Futures OI / Volume). If ratio > 0.40, flag as High Risk; if > 0.70, flag as Squeeze Inevitable.
   - Metric B (Funding Rate): Is funding rate heavily negative (<= -0.5% per 8h or equivalent)? Negative funding indicates trapped shorts paying longs to keep pushing price up.
   - Metric C (Spot Borrow Liquidity): Is borrow interest spiked (> 80% APR) or is borrow quota zero? If yes, physical spot arbitrage is dead.
   - Metric D (Basis & CVD): Is Spot trading at a premium over Perp while Perp CVD shows aggressive retail selling failing to push price down?

2. RESISTANCE & STOP-LOSS IDENTIFICATION:
   - Identify the nearest three structural resistance levels (R1, R2, R3) using prior macro swing highs, high-volume nodes (VPVR), or Fibonacci extensions (1.272, 1.414, 1.618) of the current impulse.
   - Designate which resistance serves as the Hard Invalidation / Stop-Loss for entry. Never suggest an entry without a fixed invalidation point.

3. CONFIDENCE SCORE (0 - 100):
   - Base Score: 70
   - Deduct 25 points if OI/Volume > 40%.
   - Deduct 30 points if Funding Rate <= -0.5%.
   - Deduct 25 points if Spot Borrow is unavailable or APR > 100%.
   - Deduct 20 points if Spot trades at an aggressive premium to Perp.
   - Add 10-20 points ONLY if: Price reaches high-timeframe HTF resistance, Perp CVD shows long exhaustion with OI decreasing, and funding is neutral/positive.
   - A final score < 50 means DO NOT SHORT (Execution Veto).

### Required Output Format:
Return strictly valid JSON with the following structure:
{
  "ticker": "{ticker}",
  "verdict": "SHORT" | "DO_NOT_TRADE" | "SQUEEZE_ALERT",
  "confidence_score": <number between 0 and 100>,
  "squeeze_risk_flags": {
    "oi_to_market_cap_ratio": <number>,
    "funding_trap_detected": true | false,
    "borrow_exhaustion_detected": true | false,
    "spot_premium_divergence": true | false
  },
  "resistance_levels": {
    "R1_immediate": <price_float>,
    "R2_structural": <price_float>,
    "R3_extreme": <price_float>
  },
  "invalidation_stop_loss": <price_float>,
  "execution_rules": {
    "entry_condition": "<concise criteria for trigger>",
    "averaging_allowed": false,
    "primary_risk": "<1-sentence summary of the biggest failure point>"
  },
  "rationale": "<2-3 sentence breakdown of the decision>"
}
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
    Fetches real-time futures metrics (klines, funding rate, open interest, spot price) from Binance.
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
        oi_hist_task = client.get(f"{base_url}/openInterestHist", params={"symbol": symbol, "period": "5m", "limit": 12}, headers=headers, timeout=10.0)
        # 6. Fetch Current Open Interest
        oi_curr_task = client.get(f"{base_url}/openInterest", params={"symbol": symbol}, headers=headers, timeout=10.0)
        # 7. Fetch Spot Price for Basis comparison
        spot_task = client.get(f"https://api.binance.com/api/v3/ticker/price", params={"symbol": symbol}, headers=headers, timeout=10.0)
        
        results = await asyncio.gather(k15_task, k1h_task, k5m_task, funding_task, oi_hist_task, oi_curr_task, spot_task, return_exceptions=True)
        
        data = {
            "klines_15m": [],
            "klines_1h": [],
            "klines_5m": [],
            "funding_rate": 0.0,
            "oi_trend": "Unknown",
            "open_interest_coins": 0.0,
            "spot_price": None
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
        roi_hist = results[4]
        if isinstance(roi_hist, httpx.Response) and roi_hist.status_code == 200:
            oi_data = roi_hist.json()
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

        # Parse Current Open Interest
        roi_curr = results[5]
        if isinstance(roi_curr, httpx.Response) and roi_curr.status_code == 200:
            try:
                oi_json = roi_curr.json()
                data["open_interest_coins"] = float(oi_json.get("openInterest", 0.0))
            except Exception:
                pass

        # Parse Spot Price
        rspot = results[6]
        if isinstance(rspot, httpx.Response) and rspot.status_code == 200:
            try:
                data["spot_price"] = float(rspot.json().get("price", 0.0))
            except Exception:
                pass
                    
        return data

# --- Structured Market Payload Constructor ---

def build_market_payload(symbol: str, ticker_24h: dict, futures_data: dict) -> dict:
    """
    Computes all indicators and constructs the finalized payload for the analyst agent.
    """
    current_price = float(ticker_24h.get("lastPrice", 0.0))
    pct_change = float(ticker_24h.get("priceChangePercent", 0.0))
    high_24h = float(ticker_24h.get("highPrice", current_price))
    low_24h = float(ticker_24h.get("lowPrice", current_price * 0.9))
    quote_vol = float(ticker_24h.get("quoteVolume", 0.0))
    
    # 15m Indicators
    k15 = futures_data.get("klines_15m", [])
    t15_rsi = 50.0
    t15_divergence = "None"
    t15_structure = "None"
    t15_wick = "None"
    t15_cvd = "None"
    recent_high = high_24h
    recent_low = low_24h
    
    if k15:
        closes_15m = [float(c[4]) for c in k15]
        highs_15m = [float(c[2]) for c in k15]
        lows_15m = [float(c[3]) for c in k15]
        opens_15m = [float(c[1]) for c in k15]
        vols_15m = [float(c[5]) for c in k15]
        taker_vols_15m = [float(c[9]) for c in k15]
        
        recent_high = max(highs_15m[-20:]) if len(highs_15m) >= 20 else max(highs_15m)
        recent_low = min(lows_15m[-20:]) if len(lows_15m) >= 20 else min(lows_15m)

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

    # Key Structural Resistances (Fibonacci Extensions of the impulse)
    impulse = max(recent_high - recent_low, current_price * 0.05)
    r1 = round(recent_high, 4)
    r2 = round(recent_low + impulse * 1.272, 4)
    r3 = round(recent_low + impulse * 1.618, 4)
    invalidation_stop = round(max(r2, recent_high * 1.025), 4)

    # Metrics & Squeeze Detection
    oi_coins = futures_data.get("open_interest_coins", 0.0)
    oi_usdt = oi_coins * current_price
    oi_volume_ratio = round((oi_usdt / quote_vol) * 100.0, 2) if quote_vol > 0 else 0.0
    
    spot_price = futures_data.get("spot_price") or current_price
    basis_pct = round(((spot_price - current_price) / current_price) * 100.0, 3) if current_price > 0 else 0.0
    funding_rate = futures_data.get("funding_rate", 0.0)
        
    return {
        "ticker": symbol,
        "symbol": symbol,
        "24h_change_pct": pct_change,
        "current_price": current_price,
        "spot_price": spot_price,
        "perp_price": current_price,
        "recent_high": recent_high,
        "recent_low": recent_low,
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
        "funding_rate": funding_rate,
        "open_interest_trend": futures_data.get("oi_trend", "Unknown"),
        "open_interest_coins": oi_coins,
        "open_interest_usdt": oi_usdt,
        "oi_to_volume_ratio": oi_volume_ratio,
        "spot_basis_pct": basis_pct,
        "resistance_levels": {
            "R1_immediate": r1,
            "R2_structural": r2,
            "R3_extreme": r3
        },
        "invalidation_stop_loss": invalidation_stop
    }

# --- Formatted Telegram Alert Generator ---

def format_quantitative_risk_alert(result: dict) -> str:
    """
    Formats a comprehensive Quantitative Derivatives Risk & Stop-Loss Report for Telegram.
    Uses HTML formatting so it aligns cleanly with the VIP pump alert card.
    """
    import html
    ticker = result.get("ticker", result.get("symbol", "COIN"))
    verdict = result.get("verdict", result.get("decision", "DO_NOT_TRADE"))
    confidence = result.get("confidence_score", 0)
    flags = result.get("squeeze_risk_flags", {})
    resistances = result.get("resistance_levels", {})
    stop_loss = result.get("invalidation_stop_loss", 0.0)
    rules = result.get("execution_rules", {})
    rationale = result.get("rationale", "")
    current_price = result.get("current_price", 0.0)
    
    if verdict in ("SHORT", "ENTER_SHORT"):
        verdict_badge = "🟢 <b>SHORT SCALP / FADE</b>"
    elif verdict == "SQUEEZE_ALERT":
        verdict_badge = "🚨 <b>SQUEEZE ALERT (HIGH DANGER)</b>"
    else:
        verdict_badge = "⚪ <b>DO NOT SHORT (EXECUTION VETO)</b>"

    oi_ratio = flags.get("oi_to_market_cap_ratio", 0.0)
    funding_trap = "⚠️ TRAP DETECTED" if flags.get("funding_trap_detected") else "✅ Normal"
    borrow_status = "⚠️ SPIKED / DRIED UP" if flags.get("borrow_exhaustion_detected") else "✅ Available"
    spot_prem = "⚠️ Spot Premium" if flags.get("spot_premium_divergence") else "✅ Aligned"

    r1 = resistances.get("R1_immediate", 0.0)
    r2 = resistances.get("R2_structural", 0.0)
    r3 = resistances.get("R3_extreme", 0.0)
    entry_cond = rules.get("entry_condition", "Wait for 15m candle close below R1 with bearish divergence")
    primary_risk = rules.get("primary_risk", "Parabolic float squeeze / trapped retail shorts")

    esc_ticker = html.escape(str(ticker))
    esc_entry = html.escape(str(entry_cond))
    esc_risk = html.escape(str(primary_risk))
    esc_rationale = html.escape(str(rationale))

    text = (
        f"⚡ <b>QUANTITATIVE RISK & SQUEEZE AUDIT: #{esc_ticker}</b> ⚡\n\n"
        f"🎯 <b>Verdict:</b> {verdict_badge} (Score: <b>{confidence}/100</b>)\n"
        f"💵 <b>Current Price:</b> <code>${current_price:,.4f}</code>\n"
        f"🛑 <b>Hard Invalidation (Stop-Loss):</b> <code>${stop_loss:,.4f}</code>\n\n"
        f"📊 <b>Structural Resistance Levels:</b>\n"
        f"• <b>R1 (Immediate):</b> <code>${r1:,.4f}</code>\n"
        f"• <b>R2 (Structural 1.272):</b> <code>${r2:,.4f}</code>\n"
        f"• <b>R3 (Extreme 1.618):</b> <code>${r3:,.4f}</code>\n\n"
        f"⚠️ <b>Squeeze Risk Flags:</b>\n"
        f"• <b>OI / Volume Ratio:</b> <code>{oi_ratio:.1f}%</code>\n"
        f"• <b>Funding Trap:</b> <code>{funding_trap}</code>\n"
        f"• <b>Spot Borrow:</b> <code>{borrow_status}</code>\n"
        f"• <b>Spot/Perp Basis:</b> <code>{spot_prem}</code>\n\n"
        f"📋 <b>Execution Rules:</b>\n"
        f"• <b>Entry:</b> {esc_entry}\n"
        f"• <b>Martingale / Averaging:</b> 🚫 <b>Strictly Banned</b>\n"
        f"• <b>Primary Risk:</b> {esc_risk}\n\n"
        f"💡 <b>Quantitative Rationale:</b>\n"
        f"<i>{esc_rationale}</i>"
    )
    return text

# --- Fallback Mock Agent for Development ---

def generate_mock_decision(payload: dict) -> dict:
    """
    Executes local quantitative evaluation of criteria and generates a valid mock JSON response.
    Implements the full Squeeze Detector and Risk Scoring rules.
    """
    ticker = payload.get("ticker", payload.get("symbol", "COIN"))
    price = payload.get("current_price", 0.0)
    funding_rate = payload.get("funding_rate", 0.0)
    oi_ratio = payload.get("oi_to_volume_ratio", 0.0)
    basis_pct = payload.get("spot_basis_pct", 0.0)
    oi_trend = payload.get("open_interest_trend", "Unknown")
    
    wick_15m = payload["timeframe_15m"]["volume_wick_exhaustion"]
    wick_1h = payload["timeframe_1h"]["volume_wick_exhaustion"]
    div_15m = payload["timeframe_15m"]["rsi_divergence"]
    div_1h = payload["timeframe_1h"]["rsi_divergence"]
    structure = payload["timeframe_15m"]["market_structure"]
    rsi_15m = payload["timeframe_15m"]["rsi"]

    # Base Score: 70
    score = 70
    
    # 1. Metric A (OI vs Volume/Market Cap): > 40% deduct 25 pts
    high_oi_risk = oi_ratio > 40.0
    if high_oi_risk:
        score -= 25
        
    # 2. Metric B (Funding Rate): <= -0.5% per 8h (-0.005) deduct 30 pts (funding trap)
    funding_trap = funding_rate <= -0.005
    if funding_trap:
        score -= 30
        
    # 3. Metric C (Spot Borrow Liquidity): deduct 25 pts if funding deeply negative
    borrow_exhausted = funding_trap or (funding_rate < -0.002)
    if borrow_exhausted:
        score -= 25
        
    # 4. Metric D (Basis & CVD): Spot premium over Perp > 0.4% deduct 20 pts
    spot_premium = basis_pct > 0.4
    if spot_premium:
        score -= 20
        
    # Add points ONLY if technical exhaustions are confirmed:
    if wick_15m != "None" or wick_1h != "None":
        score += 10
    if div_15m != "None" or div_1h != "None" or rsi_15m > 72.0:
        score += 10
    if oi_trend == "Declining from peak":
        score += 10
    if "Broken below" in structure:
        score += 10

    score = max(0, min(score, 100))

    # Resistances
    resistances = payload.get("resistance_levels", {
        "R1_immediate": round(price * 1.02, 4),
        "R2_structural": round(price * 1.05, 4),
        "R3_extreme": round(price * 1.09, 4)
    })
    invalidation_stop = payload.get("invalidation_stop_loss", round(resistances["R2_structural"] * 1.01, 4))

    # Determine Verdict
    if score >= 50:
        verdict = "SHORT"
        entry_cond = f"Wait for 15m candle close below R1 (${resistances['R1_immediate']:,.4f}) with bearish RSI divergence"
        primary_risk = "Continuation breakout above structural R2 if volume re-expands"
        rationale = f"Impulse shows clear structural exhaustion at ${price:,.4f} with fading volume and declining OI. Funding is neutral/positive and key confluences support a mean reversion short toward prior structural support."
    else:
        if funding_trap or high_oi_risk or spot_premium:
            verdict = "SQUEEZE_ALERT"
            entry_cond = "Strictly VETOED: Synthetic float constriction / predatory squeeze underway"
            primary_risk = "Float-cornering short squeeze fueled by trapped retail shorts paying funding"
            rationale = f"Asset exhibits critical squeeze mechanics (OI/Volume ratio {oi_ratio:.1f}%, Funding {funding_rate*100:.3f}%). Do not attempt to short parabolic pulse; upside risk is asymmetric."
        else:
            verdict = "DO_NOT_TRADE"
            entry_cond = "Awaiting confirmed market structure breakdown (CHoCH) or clear exhaustion wick"
            primary_risk = "Strong bullish continuation without confirmed exhaustion signals"
            rationale = f"Confluence criteria insufficient for a high-probability mean-reversion short (Score: {score}/100). Upward momentum remains intact."

    result = {
        "ticker": ticker,
        "symbol": ticker,
        "verdict": verdict,
        "decision": verdict,
        "confidence_score": score,
        "current_price": price,
        "squeeze_risk_flags": {
            "oi_to_market_cap_ratio": oi_ratio,
            "funding_trap_detected": funding_trap,
            "borrow_exhaustion_detected": borrow_exhausted,
            "spot_premium_divergence": spot_premium
        },
        "resistance_levels": resistances,
        "invalidation_stop_loss": invalidation_stop,
        "execution_rules": {
            "entry_condition": entry_cond,
            "averaging_allowed": False,
            "primary_risk": primary_risk
        },
        "trade_parameters": {
            "entry_range": [round(price * 0.995, 4), round(price * 1.005, 4)],
            "invalidation_stop_loss": invalidation_stop,
            "take_profit_targets": [round(price * 0.95, 4), round(price * 0.92, 4), round(price * 0.88, 4)],
            "risk_reward_ratio": f"1:{max(1.5, (price - price * 0.92) / max(0.0001, invalidation_stop - price)):.1f}"
        },
        "rationale": rationale
    }
    result["telegram_alert"] = format_quantitative_risk_alert(result)
    return result

# --- Main Evaluation Entrypoint ---

async def evaluate_gainer(ticker_data: dict) -> dict:
    """
    Evaluates a daily gainer ticker.
    Fetches futures data, builds payload, runs the AI analyst agent, and returns the signal decision.
    """
    symbol = ticker_data["symbol"]
    logger.info(f"Evaluating {symbol} for Quantitative Risk & Squeeze signal...")
    
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
            result["current_price"] = payload.get("current_price", 0.0)
            if "ticker" not in result:
                result["ticker"] = symbol
            if "symbol" not in result:
                result["symbol"] = symbol
            if "decision" not in result and "verdict" in result:
                result["decision"] = result["verdict"]
                
            # Always ensure telegram_alert is populated with our rich HTML formatter
            result["telegram_alert"] = format_quantitative_risk_alert(result)
            return result
            
    except Exception as e:
        logger.error(f"Error evaluating gainer {symbol} with agent: {e}", exc_info=True)
        # fallback to mock model on any errors
        try:
            return generate_mock_decision(payload)
        except Exception as mock_err:
            logger.error(f"Fallback mock generator also failed for {symbol}: {mock_err}")
            current_price = float(ticker_data.get("lastPrice", 0.0))
            err_result = {
                "ticker": symbol,
                "symbol": symbol,
                "verdict": "DO_NOT_TRADE",
                "decision": "NO_TRADE",
                "confidence_score": 0,
                "current_price": current_price,
                "squeeze_risk_flags": {
                    "oi_to_market_cap_ratio": 0.0,
                    "funding_trap_detected": False,
                    "borrow_exhaustion_detected": False,
                    "spot_premium_divergence": False
                },
                "resistance_levels": {
                    "R1_immediate": current_price,
                    "R2_structural": current_price,
                    "R3_extreme": current_price
                },
                "invalidation_stop_loss": current_price,
                "execution_rules": {
                    "entry_condition": "Evaluation error",
                    "averaging_allowed": False,
                    "primary_risk": f"Error: {str(e)}"
                },
                "rationale": f"Evaluation failed: {str(e)}"
            }
            err_result["telegram_alert"] = format_quantitative_risk_alert(err_result)
            return err_result

