import os
import io
import logging
from typing import Optional, Tuple, List
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(BASE_DIR, "assets", "fonts")
ICONS_DIR = os.path.join(BASE_DIR, "assets", "icons")

def format_card_price(price: Optional[float]) -> str:
    """Formats crypto price dynamically based on magnitude."""
    if price is None:
        return "$0.00"
    if price >= 1000.0:
        return f"${price:,.2f}"
    elif price >= 1.0:
        formatted = f"${price:,.4f}".rstrip('0').rstrip('.')
        return formatted if "." in formatted else f"${price:,.2f}"
    elif price >= 0.01:
        return f"${price:.4f}"
    elif price >= 0.0001:
        return f"${price:.6f}"
    else:
        return f"${price:.8f}"

def format_card_volume(vol: Optional[float]) -> str:
    """Formats 24h quote volume into clean K/M/B abbreviations."""
    if vol is None:
        return "$0.00 USDT"
    if vol >= 1_000_000_000:
        return f"${vol / 1_000_000_000:.2f}B USDT"
    elif vol >= 1_000_000:
        return f"${vol / 1_000_000:.2f}M USDT"
    elif vol >= 1_000:
        return f"${vol / 1_000:.2f}K USDT"
    else:
        return f"${vol:.2f} USDT"

def _load_font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    """Loads a font from assets/fonts or falls back to system font/default."""
    custom_path = os.path.join(FONTS_DIR, filename)
    if os.path.exists(custom_path):
        try:
            return ImageFont.truetype(custom_path, size)
        except Exception as e:
            logger.debug(f"Failed loading font from {custom_path}: {e}")
            
    # System font fallbacks
    fallbacks = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
    ]
    for fb in fallbacks:
        if os.path.exists(fb):
            try:
                return ImageFont.truetype(fb, size)
            except Exception:
                continue
    return ImageFont.load_default()

def _get_icon(name: str, size: int) -> Optional[Image.Image]:
    """Loads and resizes an icon from assets/icons."""
    icon_path = os.path.join(ICONS_DIR, f"{name}.png")
    if os.path.exists(icon_path):
        try:
            ico = Image.open(icon_path).convert("RGBA")
            return ico.resize((size, size), Image.Resampling.LANCZOS)
        except Exception as e:
            logger.debug(f"Error loading icon {name}: {e}")
    return None

def render_pump_card(
    symbol: str,
    current_pct: float,
    current_price: float,
    high_24h: float,
    low_24h: float,
    volume_24h: float,
    multiplier: Optional[float] = None
) -> io.BytesIO:
    """
    Renders the institutional, dark-themed CryptoPulse VIP alert card as a PNG image.
    Matches the sleek dark card aesthetic with neon green accents, glowing indicators,
    2x2 metric tiles, and NO alert count.
    """
    width, height = 940, 520

    # Color Palette
    bg_color = (11, 17, 24, 255)       # Outer frame: #0B1118
    card_bg = (17, 26, 36, 255)        # Card interior: #111A24
    card_border = (30, 41, 59, 255)    # Card border: #1E293B
    banner_bg = (21, 32, 44, 255)      # Top banner: #15202C
    tile_bg = (23, 35, 49, 255)        # Metric tiles: #172331
    tile_border = (30, 45, 61, 255)    # Tile border: #1E2D3D

    neon_green = (0, 230, 118, 255)    # Accent green: #00E676
    cyan_blue = (56, 189, 248, 255)    # Coin highlight: #38BDF8
    text_white = (248, 250, 252, 255)  # Heading / values: #F8FAFC
    text_muted = (148, 163, 184, 255)  # Labels / muted: #94A3B8
    bot_badge_bg = (13, 51, 40, 255)   # Bot pill bg: #0D3328

    img = Image.new("RGBA", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    card_x0, card_y0 = 24, 20
    card_x1, card_y1 = width - 24, height - 20

    # Draw main card container
    draw.rounded_rectangle(
        [card_x0, card_y0, card_x1, card_y1],
        radius=22,
        fill=card_bg,
        outline=card_border,
        width=1
    )

    # Load typography
    f_title = _load_font("Roboto-Bold.ttf", 21)
    f_badge = _load_font("Roboto-Bold.ttf", 11)
    f_banner_h = _load_font("Roboto-Bold.ttf", 18)
    f_banner_sub = _load_font("Inter-Regular.ttf", 14)
    f_banner_coin = _load_font("Roboto-Bold.ttf", 15)
    f_label = _load_font("Inter-Regular.ttf", 13)
    f_val_large = _load_font("Roboto-Bold.ttf", 25)
    f_val_med = _load_font("Roboto-Bold.ttf", 17)
    f_footer = _load_font("Inter-Regular.ttf", 13)

    def paste_icon(name: str, x: int, y: int, size: int):
        ico = _get_icon(name, size)
        if ico:
            img.alpha_composite(ico, (x, y))

    # 1. Header: "CryptoPulse VIP Bot" [BOT] (Alert count removed)
    header_x = card_x0 + 30
    header_y = card_y0 + 26
    draw.text((header_x, header_y), "CryptoPulse VIP Bot", font=f_title, fill=neon_green)

    title_w = draw.textlength("CryptoPulse VIP Bot", font=f_title)
    badge_x = int(header_x + title_w + 12)
    badge_y = header_y + 4
    draw.rounded_rectangle([badge_x, badge_y, badge_x + 44, badge_y + 20], radius=6, fill=bot_badge_bg)
    draw.text((badge_x + 9, badge_y + 3), "BOT", font=f_badge, fill=neon_green)

    # 2. Callout / Alert Banner
    banner_x0 = card_x0 + 30
    banner_y0 = header_y + 40
    banner_x1 = card_x1 - 30
    banner_y1 = banner_y0 + 78
    draw.rounded_rectangle([banner_x0, banner_y0, banner_x1, banner_y1], radius=12, fill=banner_bg)

    # Neon green accent strip on left
    draw.rounded_rectangle([banner_x0, banner_y0, banner_x0 + 4, banner_y1], radius=2, fill=neon_green)

    # Banner header text with sirens
    siren_size = 20
    paste_icon("siren", banner_x0 + 18, banner_y0 + 14, siren_size)
    banner_h_text = "PUMP ALERT: 24h Momentum Surge"
    draw.text((banner_x0 + 18 + siren_size + 8, banner_y0 + 14), banner_h_text, font=f_banner_h, fill=text_white)
    h_w = draw.textlength(banner_h_text, font=f_banner_h)
    paste_icon("siren", int(banner_x0 + 18 + siren_size + 8 + h_w + 8), banner_y0 + 14, siren_size)

    # Banner subtext: Pair: #SYMBOL · Top Gainer Radar
    sub_y = banner_y0 + 45
    pair_str = "Pair: "
    draw.text((banner_x0 + 18, sub_y), pair_str, font=f_banner_sub, fill=text_muted)
    p_w = draw.textlength(pair_str, font=f_banner_sub)

    coin_str = f"#{symbol}"
    draw.text((int(banner_x0 + 18 + p_w), sub_y), coin_str, font=f_banner_coin, fill=cyan_blue)
    c_w = draw.textlength(coin_str, font=f_banner_coin)

    draw.text((int(banner_x0 + 18 + p_w + c_w), sub_y), " · Top Gainer Radar", font=f_banner_sub, fill=text_muted)

    # 3. 2x2 Metric Grid
    grid_y0 = banner_y1 + 18
    tile_h = 110
    gap = 16
    tile_w = (banner_x1 - banner_x0 - gap) // 2
    icon_sm = 18

    # --- Tile 1: 24h Change ---
    t1_x0 = banner_x0
    t1_y0 = grid_y0
    draw.rounded_rectangle([t1_x0, t1_y0, t1_x0 + tile_w, t1_y0 + tile_h], radius=12, fill=tile_bg, outline=tile_border, width=1)
    paste_icon("chart_up", t1_x0 + 20, t1_y0 + 18, icon_sm)
    draw.text((t1_x0 + 20 + icon_sm + 8, t1_y0 + 18), "24h Change", font=f_label, fill=text_muted)

    pct_text = f"+{current_pct:.2f}%" if current_pct >= 0 else f"{current_pct:.2f}%"
    draw.text((t1_x0 + 20, t1_y0 + 52), pct_text, font=f_val_large, fill=neon_green)
    pct_w = draw.textlength(pct_text, font=f_val_large)
    paste_icon("green_circle", int(t1_x0 + 20 + pct_w + 12), t1_y0 + 55, 22)

    # --- Tile 2: Current Price ---
    t2_x0 = t1_x0 + tile_w + gap
    t2_y0 = grid_y0
    draw.rounded_rectangle([t2_x0, t2_y0, t2_x0 + tile_w, t2_y0 + tile_h], radius=12, fill=tile_bg, outline=tile_border, width=1)
    paste_icon("money", t2_x0 + 20, t2_y0 + 18, icon_sm)
    draw.text((t2_x0 + 20 + icon_sm + 8, t2_y0 + 18), "Current Price", font=f_label, fill=text_muted)
    draw.text((t2_x0 + 20, t2_y0 + 52), format_card_price(current_price), font=f_val_large, fill=text_white)

    # --- Tile 3: 24h Range (Low → High) ---
    t3_x0 = banner_x0
    t3_y0 = grid_y0 + tile_h + gap
    draw.rounded_rectangle([t3_x0, t3_y0, t3_x0 + tile_w, t3_y0 + tile_h], radius=12, fill=tile_bg, outline=tile_border, width=1)
    paste_icon("bar_chart", t3_x0 + 20, t3_y0 + 18, icon_sm)
    draw.text((t3_x0 + 20 + icon_sm + 8, t3_y0 + 18), "24h Range (Low → High)", font=f_label, fill=text_muted)
    
    p_low_str = format_card_price(low_24h)
    p_high_str = format_card_price(high_24h)
    arrow_str = " → "
    f_arrow = _load_font("Inter-Regular.ttf", 17)

    draw.text((t3_x0 + 20, t3_y0 + 56), p_low_str, font=f_val_med, fill=text_white)
    w_low = draw.textlength(p_low_str, font=f_val_med)
    draw.text((int(t3_x0 + 20 + w_low), t3_y0 + 56), arrow_str, font=f_arrow, fill=text_muted)
    w_arrow = draw.textlength(arrow_str, font=f_arrow)
    draw.text((int(t3_x0 + 20 + w_low + w_arrow), t3_y0 + 56), p_high_str, font=f_val_med, fill=text_white)

    # --- Tile 4: 24h Volume (Surge) ---
    t4_x0 = t2_x0
    t4_y0 = t3_y0
    draw.rounded_rectangle([t4_x0, t4_y0, t4_x0 + tile_w, t4_y0 + tile_h], radius=12, fill=tile_bg, outline=tile_border, width=1)
    paste_icon("droplet", t4_x0 + 20, t4_y0 + 18, icon_sm)
    draw.text((t4_x0 + 20 + icon_sm + 8, t4_y0 + 18), "24h Volume (Surge)", font=f_label, fill=text_muted)

    vol_text = format_card_volume(volume_24h)
    draw.text((t4_x0 + 20, t4_y0 + 56), vol_text, font=f_val_med, fill=text_white)
    if multiplier and multiplier >= 1.2:
        v_w = draw.textlength(vol_text, font=f_val_med)
        draw.text((int(t4_x0 + 20 + v_w + 8), t4_y0 + 56), f"({multiplier:.1f}x)", font=f_val_med, fill=neon_green)

    # 4. Footer
    footer_y = card_y1 - 32
    draw.text((banner_x0, footer_y), "• High Momentum Runner · Enter Bet", font=f_footer, fill=neon_green)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

async def generate_vip_card(
    symbol: str,
    current_pct: float,
    current_price: float,
    high_24h: float,
    low_24h: float,
    volume_24h: float,
    multiplier: Optional[float] = None
) -> Tuple[Optional[io.BytesIO], Optional[float]]:
    """
    Asynchronously generates the VIP pump alert card PNG buffer.
    If multiplier is None, attempts to compute it via recent klines.
    Returns (BytesIO PNG, multiplier).
    """
    try:
        if multiplier is None:
            try:
                import chart_service
                klines = await chart_service.fetch_klines(symbol, interval="15m", limit=48)
                if klines:
                    multiplier = chart_service.calculate_surge_multiplier(klines)
            except Exception as ke:
                logger.debug(f"Could not calculate surge multiplier for {symbol}: {ke}")

        buf = render_pump_card(
            symbol=symbol,
            current_pct=current_pct,
            current_price=current_price,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h=volume_24h,
            multiplier=multiplier
        )
        return buf, multiplier
    except Exception as e:
        logger.error(f"Failed to generate VIP card for {symbol}: {e}", exc_info=True)
        return None, multiplier

def combine_card_and_chart(card_buf: io.BytesIO, chart_buf: io.BytesIO) -> io.BytesIO:
    """
    Combines the VIP Alert Card and the 15M Candlestick Chart into a single,
    seamless vertical graphic on dark theme #0B1118.
    """
    card_img = Image.open(card_buf).convert("RGBA")
    chart_img = Image.open(chart_buf).convert("RGBA")

    target_width = 1000

    # Scale card to target width
    card_ratio = target_width / card_img.width
    card_scaled_h = int(card_img.height * card_ratio)
    card_scaled = card_img.resize((target_width, card_scaled_h), Image.Resampling.LANCZOS)

    # Scale chart to target width
    chart_ratio = target_width / chart_img.width
    chart_scaled_h = int(chart_img.height * chart_ratio)
    chart_scaled = chart_img.resize((target_width, chart_scaled_h), Image.Resampling.LANCZOS)

    spacing = 16
    total_height = card_scaled_h + spacing + chart_scaled_h

    combined = Image.new("RGBA", (target_width, total_height), (11, 17, 24, 255))
    combined.alpha_composite(card_scaled, (0, 0))
    combined.alpha_composite(chart_scaled, (0, card_scaled_h + spacing))

    buf = io.BytesIO()
    combined.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

async def generate_combined_alert(
    symbol: str,
    current_pct: float,
    current_price: float,
    high_24h: float,
    low_24h: float,
    volume_24h: float,
    multiplier: Optional[float] = None
) -> Tuple[Optional[io.BytesIO], Optional[float]]:
    """
    Generates a unified graphic combining the VIP Alert Card (top) and the 15M Candlestick Chart (bottom).
    Falls back to VIP Card if chart generation is unavailable.
    Returns (BytesIO PNG, multiplier).
    """
    try:
        import chart_service

        # 1. Generate chart
        chart_buf, mult = await chart_service.generate_pump_chart(
            symbol=symbol,
            current_pct=current_pct,
            current_price=current_price,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h=volume_24h
        )
        if multiplier is None and mult is not None:
            multiplier = mult

        # 2. Generate card
        card_buf = render_pump_card(
            symbol=symbol,
            current_pct=current_pct,
            current_price=current_price,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h=volume_24h,
            multiplier=multiplier
        )

        # 3. Combine if chart available
        if chart_buf:
            combined_buf = combine_card_and_chart(card_buf, chart_buf)
            return combined_buf, multiplier
        return card_buf, multiplier
    except Exception as e:
        logger.error(f"Error generating combined alert for {symbol}: {e}", exc_info=True)
        try:
            card_buf = render_pump_card(
                symbol=symbol,
                current_pct=current_pct,
                current_price=current_price,
                high_24h=high_24h,
                low_24h=low_24h,
                volume_24h=volume_24h,
                multiplier=multiplier
            )
            return card_buf, multiplier
        except Exception:
            return None, multiplier
