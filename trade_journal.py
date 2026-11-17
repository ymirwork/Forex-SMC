from datetime import datetime, timezone
import logging
import os
import sqlite3
from typing import Dict, List, Optional

logger = logging.getLogger("ForexSMC.TradeJournal")


class TradeJournal:
    """
    SQLite-backed institutional trade journal and statistical repository.
    Tracks executed trades, confluence ratings, realized R, and Max R (MFE).
    """

    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL,
                    sl_price REAL,
                    tp_price REAL,
                    realized_r REAL DEFAULT 0.0,
                    max_r REAL DEFAULT 0.0,
                    confluence_score INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'CLOSED',
                    chart_path TEXT,
                    mt5_ticket INTEGER,
                    comment TEXT
                )
            """)
            conn.commit()

        # Check if table is empty, seed initial sample trades if so
        self._seed_initial_data_if_empty()

    def _seed_initial_data_if_empty(self):
        """Pre-seeds the initial 11 historical trades from user's trading sheet if database is fresh."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades")
            count = cur.fetchone()[0]
            if count == 0:
                sample_data = [
                    ("XAUUSD", "SELL", 3850.0, 3860.0, 3830.0, -1.0, 0.0, 75, "LOSS", "Initial test"),
                    ("XAUUSD", "BUY",  3835.0, 3825.0, 3855.0,  1.0, 1.8, 85, "WIN",  "M15 CHoCH pullback"),
                    ("XAUUSD", "BUY",  3840.0, 3830.0, 3860.0,  1.0, 2.0, 80, "WIN",  "Golden Pocket OTE"),
                    ("XAUUSD", "BUY",  3845.0, 3835.0, 3875.0,  1.0, 2.8, 90, "WIN",  "Asian Sweep + Fibo"),
                    ("XAUUSD", "SELL", 3865.0, 3875.0, 3845.0, -1.0, 0.0, 70, "LOSS", "CHoCH failure"),
                    ("XAUUSD", "BUY",  3850.0, 3840.0, 3870.0,  1.0, 1.0, 75, "WIN",  "Quick scalp target"),
                    ("XAUUSD", "BUY",  3842.0, 3832.0, 3876.0,  1.0, 3.4, 85, "WIN",  "High confluence run"),
                    ("XAUUSD", "BUY",  3838.0, 3828.0, 3865.0,  0.6, 2.78, 80, "WIN",  "Early manual exit"),
                    ("XAUUSD", "SELL", 3870.0, 3880.0, 3850.0, -1.0, 0.0, 70, "LOSS", "Stopped out"),
                    ("XAUUSD", "BUY",  3840.0, 3830.0, 3890.0,  1.3, 5.0, 95, "WIN",  "Massive runner"),
                    ("XAUUSD", "BUY",  3848.0, 3838.0, 3868.0,  1.0, 1.0, 75, "WIN",  "TP1 reached"),
                ]
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                for item in sample_data:
                    cur.execute("""
                        INSERT INTO trades (timestamp, symbol, direction, entry_price, sl_price, tp_price, realized_r, max_r, confluence_score, status, comment)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (now_str, item[0], item[1], item[2], item[3], item[4], item[5], item[6], item[7], item[8], item[9]))
                conn.commit()
                logger.info(f"Seeded {len(sample_data)} benchmark trades into trades.db")

    def add_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        realized_r: float = 0.0,
        max_r: float = 0.0,
        confluence_score: int = 0,
        status: str = "OPEN",
        chart_path: Optional[str] = None,
        mt5_ticket: Optional[int] = None,
        comment: str = "",
    ) -> int:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades (
                    timestamp, symbol, direction, entry_price, sl_price, tp_price,
                    realized_r, max_r, confluence_score, status, chart_path, mt5_ticket, comment
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_str, symbol.upper(), direction.upper(), entry_price, sl_price, tp_price,
                realized_r, max_r, confluence_score, status, chart_path, mt5_ticket, comment
            ))
            conn.commit()
            return cur.lastrowid

    def update_trade_result(self, trade_id: int, realized_r: float, max_r: float, status: str = "CLOSED"):
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE trades
                SET realized_r = ?, max_r = ?, status = ?
                WHERE id = ?
            """, (realized_r, max_r, status, trade_id))
            conn.commit()

    def get_all_trades(self) -> List[Dict]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    def get_recent_trades(self, limit: int = 5) -> List[Dict]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    def get_summary_stats(self) -> Dict:
        trades = self.get_all_trades()
        if not trades:
            return {"total": 0, "wins": 0, "losses": 0, "winrate": 0.0, "net_r": 0.0, "avg_r": 0.0}

        total = len(trades)
        wins = sum(1 for t in trades if t["realized_r"] > 0)
        losses = sum(1 for t in trades if t["realized_r"] <= 0)
        net_r = sum(t["realized_r"] for t in trades)
        avg_r = (net_r / total) if total > 0 else 0.0
        winrate = (wins / total * 100) if total > 0 else 0.0

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "winrate": round(winrate, 1),
            "net_r": round(net_r, 2),
            "avg_r": round(avg_r, 2),
        }
