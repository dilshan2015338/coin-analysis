import sqlite3
import os
from typing import List, Tuple, Dict, Any, Optional

DB_FILE = os.getenv("DATABASE_PATH", "alerts.db")

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
    """Initializes the database schema."""
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
