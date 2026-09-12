import io
import logging
import datetime
from typing import Optional, Tuple, List
import httpx

# Headless Agg backend for server/bot execution
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.gridspec import GridSpec

logger = logging.getLogger(__name__)

def format_crypto_price(price: float) -> str:
    """Formats crypto price dynamically based on magnitude."""
    if price is None:
        return "$0.00"
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:,.4f}"
    elif price >= 0.01:
        return f"${price:.4f}"
    elif price >= 0.0001:
        return f"${price:.6f}"
    else:
        return f"${price:.8f}"

async def fetch_klines(symbol: str, interval: str = "15m", limit: int = 48) -> List[list]:
    """
    Fetches recent klines for symbol from Binance Spot or Futures API.
    Returns list of [open_time, open, high, low, close, volume, close_time, quote_volume, ...]
    """
    urls = [
        "https://api.binance.com/api/v3/klines",
        "https://fapi.binance.com/fapi/v1/klines"
    ]
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    async with httpx.AsyncClient() as client:
        for url in urls:
            try:
                resp = await client.get(url, params=params, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data
            except Exception as e:
                logger.warning(f"Error fetching klines from {url} for {symbol}: {e}")
    return []

def calculate_surge_multiplier(klines: List[list]) -> Optional[float]:
    """
    Calculates the volume surge multiplier by comparing recent volume (last 4 candles = 1h)
    against the baseline average volume of preceding candles.
    """
    if len(klines) < 12:
        return None
    try:
        quote_vols = [float(k[7]) for k in klines]  # quote volume (USDT)
        recent_chunk = quote_vols[-4:]
        baseline_chunk = quote_vols[:-4]
        
        recent_avg = sum(recent_chunk) / len(recent_chunk)
        baseline_avg = sum(baseline_chunk) / len(baseline_chunk)
        
        if baseline_avg > 0:
            mult = recent_avg / baseline_avg
            return round(mult, 1)
    except Exception as e:
        logger.debug(f"Could not calculate volume surge multiplier: {e}")
    return None

def calculate_ema(prices: List[float], period: int = 9) -> List[Optional[float]]:
    """Calculates Exponential Moving Average (EMA)."""
    if not prices:
        return []
    ema = [None] * len(prices)
    k = 2 / (period + 1)
    # Simple moving average for initial value
    if len(prices) >= period:
        sma = sum(prices[:period]) / period
        ema[period - 1] = sma
        for i in range(period, len(prices)):
            ema[i] = prices[i] * k + ema[i - 1] * (1 - k)
    return ema

def render_chart_image(
    symbol: str,
    klines: List[list],
    current_pct: float,
    current_price: float,
    high_24h: float,
    low_24h: float,
    volume_24h: float,
    multiplier: Optional[float] = None
) -> io.BytesIO:
    """
    Renders an institutional, dark-themed crypto candlestick + volume chart.
    Optimized for Telegram photo display with high DPI and dark mode contrast.
    """
    times = []
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []
    
    for k in klines:
        t = datetime.datetime.fromtimestamp(k[0] / 1000.0, tz=datetime.timezone.utc)
        times.append(t)
        opens.append(float(k[1]))
        highs.append(float(k[2]))
        lows.append(float(k[3]))
        closes.append(float(k[4]))
        volumes.append(float(k[7]))  # quote volume (USDT)

    n_candles = len(times)
    ema_values = calculate_ema(closes, period=9)

    # Dark Theme Colors
    BG_COLOR = "#0B1118"
    PANEL_BG = "#111A24"
    GRID_COLOR = "#1B2533"
    TEXT_MAIN = "#F8FAFC"
    TEXT_MUTED = "#94A3B8"
    GREEN = "#00E676"
    RED = "#FF5252"
    EMA_COLOR = "#38BDF8"  # Cyan/Sky blue 9-EMA
    
    # Create Figure with GridSpec (Price on top, Volume on bottom)
    fig = plt.figure(figsize=(10, 6.2), facecolor=BG_COLOR)
    gs = GridSpec(nrows=2, ncols=1, height_ratios=[3.6, 1.2], hspace=0.06)
    
    ax_price = fig.add_subplot(gs[0, 0], facecolor=PANEL_BG)
    ax_vol = fig.add_subplot(gs[1, 0], facecolor=PANEL_BG, sharex=ax_price)
    
    # Style axes
    for ax in [ax_price, ax_vol]:
        ax.set_facecolor(PANEL_BG)
        ax.grid(True, linestyle="--", linewidth=0.5, color=GRID_COLOR, alpha=0.7)
        for spine in ax.spines.values():
            spine.set_color("#1E2D3D")
            spine.set_linewidth(1.0)
        ax.tick_params(colors=TEXT_MUTED, labelsize=9)

    # Plot Candlesticks
    bar_width = 0.65
    for i in range(n_candles):
        o = opens[i]
        c = closes[i]
        h = highs[i]
        l = lows[i]
        v = volumes[i]
        
        is_up = c >= o
        color = GREEN if is_up else RED
        
        # High/low wick
        ax_price.plot([i, i], [l, h], color=color, linewidth=1.2, solid_capstyle="round", zorder=2)
        
        # Candle body
        lower = min(o, c)
        height = abs(c - o)
        if height == 0:
            height = (h - l) * 0.02 if (h - l) > 0 else 0.000001
            
        rect = patches.Rectangle(
            (i - bar_width / 2.0, lower),
            bar_width,
            height,
            facecolor=color,
            edgecolor=color,
            linewidth=0.8,
            zorder=3
        )
        ax_price.add_patch(rect)
        
        # Volume bar
        ax_vol.bar(
            i, 
            v, 
            width=bar_width, 
            color=color, 
            alpha=0.75, 
            zorder=2
        )

    # Plot 9-EMA line
    valid_ema_indices = [i for i, v in enumerate(ema_values) if v is not None]
    if valid_ema_indices:
        valid_ema_vals = [ema_values[i] for i in valid_ema_indices]
        ax_price.plot(
            valid_ema_indices, 
            valid_ema_vals, 
            color=EMA_COLOR, 
            linewidth=1.4, 
            linestyle="-", 
            alpha=0.85, 
            label="9 EMA",
            zorder=4
        )

    # 24h High & Low Reference Lines
    ax_price.axhline(
        high_24h, 
        color=GREEN, 
        linestyle=":", 
        linewidth=1.0, 
        alpha=0.65, 
        zorder=1
    )
    ax_price.text(
        0.5, 
        high_24h, 
        f"  24h High: {format_crypto_price(high_24h)}", 
        color=GREEN, 
        fontsize=8.5, 
        fontweight="bold",
        va="bottom", 
        ha="left", 
        alpha=0.9
    )

    ax_price.axhline(
        low_24h, 
        color="#64748B", 
        linestyle=":", 
        linewidth=1.0, 
        alpha=0.65, 
        zorder=1
    )
    ax_price.text(
        0.5, 
        low_24h, 
        f"  24h Low: {format_crypto_price(low_24h)}", 
        color="#94A3B8", 
        fontsize=8.5, 
        va="top", 
        ha="left", 
        alpha=0.9
    )

    # Current Price Marker on latest candle
    last_idx = n_candles - 1
    actual_last_price = current_price if current_price else closes[-1]
    
    # Horizontal line from last candle to right border
    ax_price.plot(
        [last_idx, last_idx + 1.2], 
        [actual_last_price, actual_last_price], 
        color=GREEN, 
        linestyle="--", 
        linewidth=1.0, 
        alpha=0.8,
        zorder=5
    )
    ax_price.plot(
        last_idx, 
        actual_last_price, 
        marker="o", 
        markersize=6, 
        color=GREEN, 
        markeredgecolor="#FFFFFF", 
        markeredgewidth=1.2, 
        zorder=6
    )
    
    # Current Price Badge on right margin
    bbox_props = dict(boxstyle="round,pad=0.4", fc=GREEN, ec="#FFFFFF", lw=0.9, alpha=0.95)
    ax_price.text(
        last_idx + 1.4, 
        actual_last_price, 
        f" {format_crypto_price(actual_last_price)} ", 
        color="#041F12", 
        fontsize=9, 
        fontweight="bold",
        va="center", 
        bbox=bbox_props,
        zorder=7
    )

    # Adjust price limits for margins
    all_prices = lows + highs + [actual_last_price, high_24h, low_24h]
    p_min = min(all_prices)
    p_max = max(all_prices)
    pad = (p_max - p_min) * 0.12 if p_max > p_min else p_min * 0.05
    ax_price.set_ylim(p_min - pad, p_max + pad)
    ax_price.set_xlim(-1, n_candles + 5)  # extra room on right for price badge

    # Format Y-axis prices
    def price_formatter(x, pos):
        return format_crypto_price(x)
    ax_price.yaxis.set_major_formatter(plt.FuncFormatter(price_formatter))
    
    # Format Volume Y-axis
    def vol_formatter(x, pos):
        if x >= 1_000_000_000:
            return f"${x/1_000_000_000:.2f}B"
        elif x >= 1_000_000:
            return f"${x/1_000_000:.1f}M"
        elif x >= 1_000:
            return f"${x/1_000:.0f}K"
        return f"${x:.0f}"
    ax_vol.yaxis.set_major_formatter(plt.FuncFormatter(vol_formatter))
    ax_vol.set_ylabel("Vol (USDT)", color=TEXT_MUTED, fontsize=8)

    # Format X-axis Time labels
    step = max(1, n_candles // 6)
    x_ticks = list(range(0, n_candles, step))
    x_labels = [times[i].strftime("%H:%M") for i in x_ticks]
    ax_vol.set_xticks(x_ticks)
    ax_vol.set_xticklabels(x_labels, color=TEXT_MUTED, fontsize=8.5)
    
    # Hide X ticks on top price chart
    plt.setp(ax_price.get_xticklabels(), visible=False)

    # Header: Pair, Timeframe, 9-EMA badge
    fig.text(
        0.05, 0.95, 
        f"#{symbol}", 
        color=TEXT_MAIN, 
        fontsize=15, 
        fontweight="bold"
    )
    
    vol_24h_str = ""
    if volume_24h >= 1_000_000:
        vol_24h_str = f" · 24h Vol: ${volume_24h/1_000_000:.2f}M USDT"
    elif volume_24h >= 1_000:
        vol_24h_str = f" · 24h Vol: ${volume_24h/1_000:.1f}K USDT"
        
    fig.text(
        0.05, 0.915, 
        f"15M Momentum Breakout{vol_24h_str}", 
        color=TEXT_MUTED, 
        fontsize=9
    )

    # Percentage Surge Badge (Top Right)
    pct_text = f"+{current_pct:.2f}%" if current_pct >= 0 else f"{current_pct:.2f}%"
    mult_tag = f" ({multiplier:.1f}x Surge)" if multiplier and multiplier >= 1.2 else ""
    badge_content = f"  {pct_text}{mult_tag}  "
    
    fig.text(
        0.95, 0.935, 
        badge_content, 
        color="#041F12", 
        fontsize=11, 
        fontweight="bold",
        ha="right", 
        va="center",
        bbox=dict(boxstyle="round,pad=0.45", fc=GREEN, ec="#A7F3D0", lw=1.0)
    )

    # Watermark / Footer
    fig.text(
        0.95, 0.02, 
        "CryptoPulse VIP Momentum Bot", 
        color="#475569", 
        fontsize=8, 
        ha="right"
    )

    # Save to in-memory bytes
    buf = io.BytesIO()
    fig.savefig(
        buf, 
        format="png", 
        dpi=150, 
        bbox_inches="tight", 
        facecolor=fig.get_facecolor(), 
        edgecolor="none"
    )
    plt.close(fig)
    buf.seek(0)
    return buf

async def generate_pump_chart(
    symbol: str,
    current_pct: float,
    current_price: float,
    high_24h: float,
    low_24h: float,
    volume_24h: float
) -> Tuple[Optional[io.BytesIO], Optional[float]]:
    """
    Main asynchronous entrypoint to fetch klines and generate the pump chart image.
    Returns (BytesIO PNG, multiplier) or (None, None) on failure.
    """
    try:
        klines = await fetch_klines(symbol, interval="15m", limit=48)
        if not klines or len(klines) < 5:
            klines = await fetch_klines(symbol, interval="1h", limit=24)
            
        if not klines or len(klines) < 5:
            logger.warning(f"Insufficient kline data to generate chart for {symbol}")
            return None, None

        multiplier = calculate_surge_multiplier(klines)
        
        buf = render_chart_image(
            symbol=symbol,
            klines=klines,
            current_pct=current_pct,
            current_price=current_price,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h=volume_24h,
            multiplier=multiplier
        )
        return buf, multiplier
    except Exception as e:
        logger.error(f"Failed to generate pump chart for {symbol}: {e}", exc_info=True)
        return None, None
