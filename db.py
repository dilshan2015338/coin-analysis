import sqlite3
import os
import datetime
import logging
from typing import List, Tuple, Dict, Any, Optional

DB_FILE = os.getenv("DATABASE_PATH", "alerts.db")
logger = logging.getLogger(__name__)

def get_connection() -> sqlite3.Connection:
    """Returns a connection to the SQLite database with Foreign Keys enabled."""
    db_dir = os.path.dirname(DB_FILE)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database schema and performs schema migration if necessary."""
    # Perform migration check first
    if os.path.exists(DB_FILE):
        try:
            with get_connection() as conn:
                cursor = conn.execute("PRAGMA table_info(daily_klines);")
                columns = [row["name"] for row in cursor.fetchall()]
                # If the table exists but lacks the 'open' column, drop tables to trigger recreation
                if columns and "open" not in columns:
                    logger.info("Migrating database schema: dropping old tables daily_klines and average_metrics...")
                    conn.execute("DROP TABLE IF EXISTS daily_klines;")
                    conn.execute("DROP TABLE IF EXISTS average_metrics;")
                    conn.commit()
        except sqlite3.OperationalError:
            pass

    with get_connection() as conn:
        # Create watched_coins table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watched_coins (
                symbol TEXT PRIMARY KEY,
                user_symbol TEXT NOT NULL
            );
        """)
        
        # Create target_alerts table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                target_price REAL NOT NULL,
                condition TEXT NOT NULL,
                triggered INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(symbol) REFERENCES watched_coins(symbol) ON DELETE CASCADE
            );
        """)
        
        # Create step_alerts table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS step_alerts (
                symbol TEXT PRIMARY KEY,
                step_interval REAL NOT NULL,
                baseline_price REAL NOT NULL,
                last_triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(symbol) REFERENCES watched_coins(symbol) ON DELETE CASCADE
            );
        """)

        # Create daily_klines table (with open price column)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_klines (
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY (symbol, date),
                FOREIGN KEY(symbol) REFERENCES watched_coins(symbol) ON DELETE CASCADE
            );
        """)

        # Create average_metrics table (storing percentage deviations from open)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS average_metrics (
                symbol TEXT PRIMARY KEY,
                avg_high_pct REAL NOT NULL,
                avg_low_pct REAL NOT NULL,
                max_high_pct REAL NOT NULL,
                max_low_pct REAL NOT NULL,
                total_days INTEGER NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(symbol) REFERENCES watched_coins(symbol) ON DELETE CASCADE
            );
        """)

        # Create average_alerts table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS average_alerts (
                symbol TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                triggered INTEGER DEFAULT 0,
                last_triggered_at TIMESTAMP,
                PRIMARY KEY (symbol, metric_type),
                FOREIGN KEY(symbol) REFERENCES watched_coins(symbol) ON DELETE CASCADE
            );
        """)

        # Create gainer_alerts_history table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gainer_alerts_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                price_change_pct REAL NOT NULL,
                price_at_alert REAL NOT NULL,
                alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Create settings table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        
        # Insert default settings if they don't exist
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gainer_threshold', '50.0');")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gainer_scanner_enabled', '1');")
        conn.commit()

# --- Watched Coins Operations ---

def update_watched_coins(coins: List[Tuple[str, str]]):
    """
    Syncs the database watched coins with the provided list.
    Removes any coins not in the list (cascading to their alerts),
    and inserts new ones.
    coins is a list of tuples: (resolved_symbol, user_symbol)
    """
    with get_connection() as conn:
        if not coins:
            # If empty, delete everything
            conn.execute("DELETE FROM watched_coins;")
            conn.commit()
            return
        
        # Keep track of active resolved symbols
        active_symbols = [c[0] for c in coins]
        
        # Delete coins not in the new list
        placeholders = ",".join("?" for _ in active_symbols)
        conn.execute(f"DELETE FROM watched_coins WHERE symbol NOT IN ({placeholders});", active_symbols)
        
        # Insert or replace new coins
        for symbol, user_symbol in coins:
            conn.execute(
                "INSERT OR REPLACE INTO watched_coins (symbol, user_symbol) VALUES (?, ?);",
                (symbol, user_symbol)
            )
        conn.commit()

def get_watched_coins() -> List[Dict[str, str]]:
    """Returns all currently watched coins."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT symbol, user_symbol FROM watched_coins;")
        return [dict(row) for row in cursor.fetchall()]

# --- Target Alerts Operations ---

def add_target_alert(symbol: str, target_price: float, condition: str) -> int:
    """Adds a new target alert for a symbol."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO target_alerts (symbol, target_price, condition, triggered) VALUES (?, ?, ?, 0);",
            (symbol, target_price, condition)
        )
        conn.commit()
        return cursor.lastrowid

def get_active_target_alerts() -> List[Dict[str, Any]]:
    """Returns all active (untriggered) target price alerts."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT id, symbol, target_price, condition FROM target_alerts WHERE triggered = 0;"
        )
        return [dict(row) for row in cursor.fetchall()]

def mark_target_alert_triggered(alert_id: int):
    """Marks a specific target alert as triggered."""
    with get_connection() as conn:
        conn.execute("UPDATE target_alerts SET triggered = 1 WHERE id = ?;", (alert_id,))
        conn.commit()

def delete_target_alert(alert_id: int):
    """Deletes a target alert by ID."""
    with get_connection() as conn:
        conn.execute("DELETE FROM target_alerts WHERE id = ?;", (alert_id,))
        conn.commit()

def clear_target_alerts(symbol: str):
    """Deletes all target alerts for a specific symbol."""
    with get_connection() as conn:
        conn.execute("DELETE FROM target_alerts WHERE symbol = ?;", (symbol,))
        conn.commit()

# --- Step Alerts Operations ---

def set_step_alert(symbol: str, step_interval: float, baseline_price: float):
    """Inserts or updates a step alert configuration for a symbol."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO step_alerts (symbol, step_interval, baseline_price, last_triggered_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP);
            """,
            (symbol, step_interval, baseline_price)
        )
        conn.commit()

def get_step_alerts() -> List[Dict[str, Any]]:
    """Returns all configured step alerts."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT symbol, step_interval, baseline_price FROM step_alerts;")
        return [dict(row) for row in cursor.fetchall()]

def update_step_baseline(symbol: str, new_baseline: float):
    """Updates the baseline price and trigger timestamp for a step alert."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE step_alerts
            SET baseline_price = ?, last_triggered_at = CURRENT_TIMESTAMP
            WHERE symbol = ?;
            """,
            (new_baseline, symbol)
        )
        conn.commit()

def remove_step_alert(symbol: str):
    """Removes a step alert configuration for a symbol."""
    with get_connection() as conn:
        conn.execute("DELETE FROM step_alerts WHERE symbol = ?;", (symbol,))
        conn.commit()

# --- Daily Klines Operations ---

def insert_daily_klines(symbol: str, klines: List[Tuple[str, str, float, float, float, float, float]]):
    """
    Inserts a list of 1D candlestick data into the database.
    klines element structure: (symbol, date, open, high, low, close, volume)
    """
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_klines (symbol, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            klines
        )
        conn.commit()

def get_last_kline_date(symbol: str) -> Optional[str]:
    """Returns the latest kline date (YYYY-MM-DD) for a symbol."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT MAX(date) FROM daily_klines WHERE symbol = ?;", (symbol,))
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else None

# --- Average Metrics Operations ---

def get_average_metrics(symbol: str) -> Optional[Dict[str, Any]]:
    """Returns the cached YTD average metrics for a symbol."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT symbol, avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days, last_updated 
            FROM average_metrics 
            WHERE symbol = ?;
            """,
            (symbol,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

def update_average_metrics(symbol: str, avg_high_pct: float, avg_low_pct: float, max_high_pct: float, max_low_pct: float, total_days: int):
    """Caches recalculated average metrics for a symbol."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO average_metrics (symbol, avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
            """,
            (symbol, avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days)
        )
        conn.commit()

def recalculate_average_metrics(symbol: str) -> Optional[Tuple[float, float, float, float, int]]:
    """
    Computes avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, and total_days from stored daily_klines.
    Returns (avg_high_pct, avg_low_pct, max_high_pct, max_low_pct, total_days) or None if no klines exist.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT 
                AVG((high - open) / open), 
                AVG((open - low) / open), 
                MAX((high - open) / open), 
                MAX((open - low) / open), 
                COUNT(date)
            FROM daily_klines 
            WHERE symbol = ?;
            """,
            (symbol,)
        )
        row = cursor.fetchone()
        if row and row[4] > 0:
            return float(row[0]), float(row[1]), float(row[2]), float(row[3]), int(row[4])
        return None

# --- Average Alerts Operations ---

def set_average_alert(symbol: str, metric_type: str):
    """Enables alert monitoring for an average metric type (HIGH, LOW, MIDPOINT)."""
    metric_type = metric_type.upper()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO average_alerts (symbol, metric_type, triggered, last_triggered_at)
            VALUES (?, ?, 0, NULL);
            """,
            (symbol, metric_type)
        )
        conn.commit()

def get_active_average_alerts() -> List[Dict[str, Any]]:
    """
    Returns all enabled average alerts combined with their cached percentage metrics.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT aa.symbol, aa.metric_type, aa.last_triggered_at,
                   am.avg_high_pct, am.avg_low_pct, am.max_high_pct, am.max_low_pct
            FROM average_alerts aa
            JOIN average_metrics am ON aa.symbol = am.symbol;
            """
        )
        return [dict(row) for row in cursor.fetchall()]

def update_average_alert_triggered(symbol: str, metric_type: str):
    """Updates the triggering timestamp for debouncing / cooldown."""
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE average_alerts 
            SET triggered = 1, last_triggered_at = ? 
            WHERE symbol = ? AND metric_type = ?;
            """,
            (now_str, symbol, metric_type.upper())
        )
        conn.commit()

def remove_average_alert(symbol: str, metric_type: str):
    """Disables alert monitoring for a specific average metric type."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM average_alerts WHERE symbol = ? AND metric_type = ?;",
            (symbol, metric_type.upper())
        )
        conn.commit()

def clear_average_alerts(symbol: str):
    """Disables all average alert monitoring for a symbol."""
    with get_connection() as conn:
        conn.execute("DELETE FROM average_alerts WHERE symbol = ?;", (symbol,))
        conn.commit()

def get_recent_closes(symbol: str, limit: int = 20) -> List[float]:
    """Returns the close prices of the most recent daily klines ordered by date descending."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT close FROM daily_klines WHERE symbol = ? ORDER BY date DESC LIMIT ?;",
            (symbol, limit)
        )
        return [row["close"] for row in cursor.fetchall()]

def get_setting(key: str, default: str) -> str:
    """Gets a configuration setting value from the database settings table."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT value FROM settings WHERE key = ?;", (key,))
        row = cursor.fetchone()
        return row[0] if row else default

def set_setting(key: str, value: str):
    """Updates or inserts a configuration setting value in the settings table."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);",
            (key, str(value))
        )
        conn.commit()

def get_last_gainer_alert(symbol: str) -> Optional[Dict[str, Any]]:
    """Retrieves the most recent alert for a symbol from gainer_alerts_history."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT price_change_pct, price_at_alert, alerted_at FROM gainer_alerts_history WHERE symbol = ? ORDER BY alerted_at DESC LIMIT 1;",
            (symbol,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

def insert_gainer_alert(symbol: str, price_change_pct: float, price_at_alert: float):
    """Inserts a new gainer alert record with timezone-aware timestamp string."""
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO gainer_alerts_history (symbol, price_change_pct, price_at_alert, alerted_at) VALUES (?, ?, ?, ?);",
            (symbol, price_change_pct, price_at_alert, now_str)
        )
        conn.commit()
