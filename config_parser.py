import re
from typing import Optional, Dict, Any

def parse_message_command(text: str) -> Optional[Dict[str, Any]]:
    """
    Parses a text message to check if it contains a configuration command.
    Returns a dictionary of command arguments if parsed successfully, or None.
    
    Supported Commands:
    1. Watch list of coins:
       /watch XRP, ADA, UBUSDT, BTC
       CONFIG WATCH XRP, ADA, UBUSDT, BTC
       
    2. Step threshold:
       /set_step BTC 500
       CONFIG STEP BTC 500
       
    3. Target price alerts:
       /set_target BTC 65000 ABOVE
       /set_target BTC 65000 BELOW
       /set_target BTC 65000 (auto-detects ABOVE/BELOW relative to current price)
       CONFIG TARGET BTC 65000
       
    4. Remove Step threshold:
       /remove_step BTC
       /clear_step BTC
       CONFIG REMOVE_STEP BTC

    5. Remove Target price alerts:
       /remove_target BTC
       /clear_targets BTC
       CONFIG REMOVE_TARGET BTC

    6. Status check:
       /status
       CONFIG STATUS

    7. Help instructions:
       /help
       /start
       CONFIG HELP
    """
    text = text.strip()
    if not text:
        return None

    # 1. Watch Command
    # Matches: /watch XRP, ADA, BTC or CONFIG WATCH XRP, ADA, BTC
    watch_match = re.search(r"^(?:/watch|CONFIG\s+WATCH)\s+(.+)$", text, re.IGNORECASE)
    if watch_match:
        raw_list = watch_match.group(1)
        # Split by comma and strip whitespaces, ignore empty entries
        coins = [c.strip() for c in re.split(r",", raw_list) if c.strip()]
        return {"type": "watch", "coins": coins}

    # 2. Step Tracking Command
    # Matches: /set_step BTC 500 or CONFIG STEP BTC 500
    step_match = re.search(r"^(?:/set_step|CONFIG\s+STEP)\s+(\S+)\s+(\d+(?:\.\d+)?)$", text, re.IGNORECASE)
    if step_match:
        symbol = step_match.group(1).upper()
        step_interval = float(step_match.group(2))
        return {
            "type": "step",
            "symbol": symbol,
            "step_interval": step_interval
        }

    # 3. Target Alerts Command
    # Matches: /set_target BTC 65000 ABOVE or CONFIG TARGET BTC 65000
    target_match = re.search(
        r"^(?:/set_target|CONFIG\s+TARGET)\s+(\S+)\s+(\d+(?:\.\d+)?)(?:\s+(ABOVE|BELOW))?$", 
        text, 
        re.IGNORECASE
    )
    if target_match:
        symbol = target_match.group(1).upper()
        target_price = float(target_match.group(2))
        condition = target_match.group(3)
        if condition:
            condition = condition.upper()
        return {
            "type": "target",
            "symbol": symbol,
            "target_price": target_price,
            "condition": condition
        }

    # 4. Remove Step Tracking Command
    # Matches: /remove_step BTC or /clear_step BTC or CONFIG REMOVE_STEP BTC
    remove_step_match = re.search(
        r"^(?:/remove_step|/clear_step|CONFIG\s+(?:REMOVE_STEP|CLEAR_STEP))\s+(\S+)$",
        text,
        re.IGNORECASE
    )
    if remove_step_match:
        return {
            "type": "remove_step",
            "symbol": remove_step_match.group(1).upper()
        }

    # 5. Remove Target Price Alerts Command
    # Matches: /remove_target BTC or /clear_targets BTC or CONFIG REMOVE_TARGET BTC
    remove_target_match = re.search(
        r"^(?:/remove_target|/clear_targets|CONFIG\s+(?:REMOVE_TARGET|CLEAR_TARGETS))\s+(\S+)$",
        text,
        re.IGNORECASE
    )
    if remove_target_match:
        return {
            "type": "remove_target",
            "symbol": remove_target_match.group(1).upper()
        }

    # 5. Status Check Command
    # Matches: /status [SYMBOL] or CONFIG STATUS [SYMBOL] (optional SYMBOL)
    status_match = re.search(r"^(?:/status|CONFIG\s+STATUS)(?:\s+(\S+))?$", text, re.IGNORECASE)
    if status_match:
        symbol = status_match.group(1)
        if symbol:
            symbol = symbol.upper()
        return {"type": "status", "symbol": symbol}

    # Matches: /short_status SYMBOL or /evaluate SYMBOL or CONFIG SHORT_STATUS SYMBOL or CONFIG EVALUATE SYMBOL
    short_status_match = re.search(
        r"^(?:/short_status|/evaluate|CONFIG\s+(?:SHORT_STATUS|EVALUATE))\s+(\S+)$", 
        text, 
        re.IGNORECASE
    )
    if short_status_match:
        return {
            "type": "short_status",
            "symbol": short_status_match.group(1).upper()
        }

    # 6. Set Average Alert Command
    # Matches: /set_avg_alert [SYMBOL] [HIGH|LOW|MIDPOINT] or CONFIG AVG_ALERT [SYMBOL] [HIGH|LOW|MIDPOINT]
    avg_alert_match = re.search(
        r"^(?:/set_avg_alert|CONFIG\s+AVG_ALERT)\s+(\S+)\s+(HIGH|LOW|MIDPOINT)$", 
        text, 
        re.IGNORECASE
    )
    if avg_alert_match:
        return {
            "type": "set_avg_alert",
            "symbol": avg_alert_match.group(1).upper(),
            "metric_type": avg_alert_match.group(2).upper()
        }

    # 7. Remove Average Alert Command
    # Matches: /remove_avg_alert [SYMBOL] [HIGH|LOW|MIDPOINT] or CONFIG REMOVE_AVG_ALERT [SYMBOL] [HIGH|LOW|MIDPOINT]
    remove_avg_alert_match = re.search(
        r"^(?:/remove_avg_alert|CONFIG\s+REMOVE_AVG_ALERT)\s+(\S+)(?:\s+(HIGH|LOW|MIDPOINT))?$", 
        text, 
        re.IGNORECASE
    )
    if remove_avg_alert_match:
        metric = remove_avg_alert_match.group(2)
        if metric:
            metric = metric.upper()
        return {
            "type": "remove_avg_alert",
            "symbol": remove_avg_alert_match.group(1).upper(),
            "metric_type": metric
        }

    # 8. Gainers Command
    # Matches: /gainers [MIN_PERCENT] or CONFIG GAINERS [MIN_PERCENT]
    gainers_match = re.search(r"^(?:/gainers|CONFIG\s+GAINERS)(?:\s+(\d+(?:\.\d+)?))?$", text, re.IGNORECASE)
    if gainers_match:
        min_pct = gainers_match.group(1)
        return {
            "type": "gainers",
            "min_percent": float(min_pct) if min_pct else None
        }

    # 9. Set Gainer Threshold Command
    # Matches: /set_gainer_threshold [PERCENT] or CONFIG GAINER_THRESHOLD [PERCENT]
    threshold_match = re.search(r"^(?:/set_gainer_threshold|CONFIG\s+GAINER_THRESHOLD)\s+(\d+(?:\.\d+)?)$", text, re.IGNORECASE)
    if threshold_match:
        return {
            "type": "set_gainer_threshold",
            "percent": float(threshold_match.group(1))
        }

    # 10. Gainer Scanner Toggle Command
    # Matches: /gainer_scanner [ON|OFF] or CONFIG GAINER_SCANNER [ON|OFF]
    scanner_match = re.search(r"^(?:/gainer_scanner|CONFIG\s+GAINER_SCANNER)\s+(ON|OFF)$", text, re.IGNORECASE)
    if scanner_match:
        return {
            "type": "gainer_scanner",
            "status": scanner_match.group(1).upper()
        }

    # 11. Set Recurring Update Command
    # Matches: /set_update BTC 2 or CONFIG UPDATE BTC 2
    update_match = re.search(
        r"^(?:/set_update|CONFIG\s+UPDATE)\s+(\S+)\s+(\d+)$", 
        text, 
        re.IGNORECASE
    )
    if update_match:
        return {
            "type": "set_update",
            "symbol": update_match.group(1).upper(),
            "interval_minutes": int(update_match.group(2))
        }

    # 12. Remove Recurring Update Command
    # Matches: /remove_update BTC or /clear_update BTC or CONFIG REMOVE_UPDATE BTC
    remove_update_match = re.search(
        r"^(?:/remove_update|/clear_update|CONFIG\s+(?:REMOVE_UPDATE|CLEAR_UPDATE))\s+(\S+)$",
        text,
        re.IGNORECASE
    )
    if remove_update_match:
        return {
            "type": "remove_update",
            "symbol": remove_update_match.group(1).upper()
        }

    # 13. Help / Start Command
    # Matches: /help, /start or CONFIG HELP
    if re.search(r"^(?:/help|/start|CONFIG\s+HELP)$", text, re.IGNORECASE):
        return {"type": "help"}

    return None
