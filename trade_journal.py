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
                    comment TEXT,
                    notion_page_id TEXT
                )
            """)
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN notion_page_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN parent_ticket INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN is_pyramid INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.commit()

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
        parent_ticket: Optional[int] = None,
        is_pyramid: int = 0,
    ) -> int:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades (
                    timestamp, symbol, direction, entry_price, sl_price, tp_price,
                    realized_r, max_r, confluence_score, status, chart_path, mt5_ticket, comment,
                    parent_ticket, is_pyramid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_str, symbol.upper(), direction.upper(), entry_price, sl_price, tp_price,
                realized_r, max_r, confluence_score, status, chart_path, mt5_ticket, comment,
                parent_ticket, is_pyramid
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

    def get_open_trades(self) -> List[Dict]:
        """Returns all trades currently tracked as OPEN in the journal."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    def set_notion_page_id_by_ticket(self, ticket: int, page_id: str):
        """Associates an MT5 ticket with its synced Notion Page ID."""
        with self._get_connection() as conn:
            conn.execute("UPDATE trades SET notion_page_id = ? WHERE mt5_ticket = ?", (page_id, ticket))
            conn.commit()

    def get_trade_by_ticket(self, ticket: int) -> Optional[Dict]:
        """Retrieves a trade record by MT5 ticket."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE mt5_ticket = ?", (ticket,))
            row = cur.fetchone()
            return dict(row) if row else None

    def close_trade_by_ticket(
        self,
        mt5_ticket: int,
        realized_r: float,
        max_r: Optional[float] = None,
        status: str = "CLOSED",
        comment: str = "",
    ) -> bool:
        """Closes an open trade record in trades.db by its MT5 ticket number."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            if max_r is not None:
                cur.execute("""
                    UPDATE trades
                    SET realized_r = ?, max_r = ?, status = ?, comment = ?
                    WHERE mt5_ticket = ? AND status = 'OPEN'
                """, (realized_r, max_r, status, comment, mt5_ticket))
            else:
                cur.execute("""
                    UPDATE trades
                    SET realized_r = ?, status = ?, comment = ?
                    WHERE mt5_ticket = ? AND status = 'OPEN'
                """, (realized_r, status, comment, mt5_ticket))
            conn.commit()
            return cur.rowcount > 0

    def update_trade_max_r(self, mt5_ticket: int, max_r: float):
        """Updates the highest R reached (MFE) for an active trade."""
        with self._get_connection() as conn:
            conn.execute("UPDATE trades SET max_r = ? WHERE mt5_ticket = ?", (max_r, mt5_ticket))
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
            return {"total": 0, "closed": 0, "wins": 0, "losses": 0, "winrate": 0.0, "net_r": 0.0, "avg_r": 0.0}

        # Only closed trades count toward winrate and realized net R
        closed_trades = [t for t in trades if t.get("status") in ("CLOSED", "WIN", "LOSS", "BE")]
        if not closed_trades:
            return {"total": len(trades), "closed": 0, "wins": 0, "losses": 0, "winrate": 0.0, "net_r": 0.0, "avg_r": 0.0}

        total_closed = len(closed_trades)
        wins = sum(1 for t in closed_trades if t["realized_r"] > 0)
        losses = sum(1 for t in closed_trades if t["realized_r"] < 0)
        net_r = sum(t["realized_r"] for t in closed_trades)
        avg_r = (net_r / total_closed) if total_closed > 0 else 0.0
        winrate = (wins / total_closed * 100) if total_closed > 0 else 0.0

        return {
            "total": len(trades),
            "closed": total_closed,
            "wins": wins,
            "losses": losses,
            "winrate": round(winrate, 1),
            "net_r": round(net_r, 2),
            "avg_r": round(avg_r, 2),
        }
