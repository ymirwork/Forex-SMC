from contextlib import contextmanager
from datetime import datetime, timezone
import functools
import gc
import logging
import os
import shutil
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ForexSMC.TradeJournal")


def _auto_recover_on_corruption(method):
    """
    Decorator that intercepts SQLite corruption / malformed disk image errors,
    triggers automatic database self-healing, and retries the operation.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except sqlite3.DatabaseError as e:
            err_msg = str(e).lower()
            if any(term in err_msg for term in ("malformed", "corrupt", "file is not a database", "disk i/o error")):
                logger.critical(
                    f"SQLite corruption detected during {method.__name__}: {e}. "
                    f"Initiating automatic database self-healing..."
                )
                recovered = self._recover_database()
                if recovered:
                    logger.info(f"Database recovered successfully. Retrying {method.__name__}...")
                    try:
                        return method(self, *args, **kwargs)
                    except Exception as retry_err:
                        logger.error(f"Method {method.__name__} failed after auto-recovery: {retry_err}")
                        raise
                raise
            raise
    return wrapper


class TradeJournal:
    """
    SQLite-backed institutional trade journal and statistical repository.
    Tracks executed trades, confluence ratings, realized R, and Max R (MFE).
    Features automated self-healing, integrity verification, and crash recovery.
    """

    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self.check_and_repair()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
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
                    notion_page_id TEXT,
                    parent_ticket INTEGER,
                    is_pyramid INTEGER DEFAULT 0,
                    strategy TEXT DEFAULT 'SMC',
                    session_name TEXT DEFAULT '',
                    is_killzone INTEGER DEFAULT 0,
                    setup_type TEXT DEFAULT '',
                    confluences_json TEXT DEFAULT '{}',
                    mae_r REAL DEFAULT 0.0,
                    closed_at TEXT DEFAULT '',
                    exit_reason TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticket ON trades(mt5_ticket);")
            for col, col_def in [
                ("notion_page_id", "TEXT"),
                ("parent_ticket", "INTEGER"),
                ("is_pyramid", "INTEGER DEFAULT 0"),
                ("strategy", "TEXT DEFAULT 'SMC'"),
                ("session_name", "TEXT DEFAULT ''"),
                ("is_killzone", "INTEGER DEFAULT 0"),
                ("setup_type", "TEXT DEFAULT ''"),
                ("confluences_json", "TEXT DEFAULT '{}'"),
                ("mae_r", "REAL DEFAULT 0.0"),
                ("closed_at", "TEXT DEFAULT ''"),
                ("exit_reason", "TEXT DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_def}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def check_and_repair(self) -> bool:
        """
        Validates database integrity. If corrupted or missing, initiates auto-recovery or clean init.
        Returns True if database is healthy or successfully repaired.
        """
        if not os.path.exists(self.db_path):
            self._init_db()
            return True

        needs_recovery = False
        try:
            with self._get_connection() as conn:
                res = conn.execute("PRAGMA quick_check;").fetchall()
                if not res or res[0][0] != "ok":
                    logger.warning(f"Database quick_check failed: {res}")
                    needs_recovery = True
        except sqlite3.DatabaseError as e:
            logger.warning(f"Database error during integrity check: {e}")
            needs_recovery = True

        if needs_recovery:
            logger.critical(f"Database '{self.db_path}' failed integrity check. Initiating auto-recovery...")
            return self._recover_database()

        # Database passed quick_check; ensure schema and columns exist
        try:
            self._init_db()
        except sqlite3.DatabaseError as e:
            logger.critical(f"Database error during schema init: {e}. Initiating auto-recovery...")
            return self._recover_database()

        return True

    def _recover_database(self) -> bool:
        """
        Repairs or rebuilds a corrupted SQLite database:
        1. Attempts in-place 'REINDEX' (fixes corrupted B-Tree indexes).
        2. If still malformed, archives corrupted files (*.db, *-wal, *-shm),
           salvages all readable rows, and rebuilds a fresh clean database.
        """
        gc.collect()

        # Step 1: Attempt in-place REINDEX
        try:
            with self._get_connection() as conn:
                conn.execute("REINDEX;")
                conn.commit()
                res = conn.execute("PRAGMA quick_check;").fetchall()
                if res and res[0][0] == "ok":
                    logger.info("Successfully repaired database corruption via in-place REINDEX.")
                    return True
        except Exception as e:
            logger.warning(f"In-place REINDEX failed ({e}). Proceeding to full archive & rebuild.")

        # Step 2: Backup corrupted files
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.dirname(os.path.abspath(self.db_path))
        base_name = os.path.basename(self.db_path)
        corrupt_backup = os.path.join(backup_dir, f"{base_name}.corrupt_{ts}")

        salvaged_trades: List[Dict[str, Any]] = []
        # Attempt to salvage readable rows before archiving
        ro_conn = None
        try:
            ro_conn = sqlite3.connect(f"file:{os.path.abspath(self.db_path)}?mode=ro", uri=True, timeout=5.0)
            ro_conn.row_factory = sqlite3.Row
            cur = ro_conn.cursor()
            try:
                cur.execute("SELECT * FROM trades")
                while True:
                    try:
                        row = cur.fetchone()
                        if row is None:
                            break
                        salvaged_trades.append(dict(row))
                    except Exception:
                        break
            except Exception as query_err:
                logger.debug(f"Row iteration error during salvage: {query_err}")
        except Exception as conn_err:
            logger.warning(f"Could not open corrupted database for reading: {conn_err}")
        finally:
            if ro_conn:
                try:
                    ro_conn.close()
                except Exception:
                    pass

        # Move/archive corrupted files (.db, .db-wal, .db-shm)
        gc.collect()
        file_moved = True
        for ext in ["", "-wal", "-shm"]:
            src = f"{self.db_path}{ext}"
            if os.path.exists(src):
                dst = f"{corrupt_backup}{ext}"
                try:
                    shutil.move(src, dst)
                    logger.info(f"Archived corrupted file: {src} -> {dst}")
                except Exception as move_err:
                    logger.warning(f"Could not move {src}: {move_err}. Attempting copy.")
                    try:
                        shutil.copy2(src, dst)
                        os.remove(src)
                    except Exception as rm_err:
                        logger.warning(f"Could not delete {src} due to file lock: {rm_err}")
                        file_moved = False

        # If file could not be deleted due to lock, truncate it to clean 0-byte state
        if not file_moved and os.path.exists(self.db_path):
            try:
                with open(self.db_path, "wb") as f:
                    f.truncate(0)
            except Exception as trunc_err:
                logger.error(f"In-place file truncation failed: {trunc_err}")
            for ext in ["-wal", "-shm"]:
                wpath = f"{self.db_path}{ext}"
                if os.path.exists(wpath):
                    try:
                        with open(wpath, "wb") as f:
                            f.truncate(0)
                        os.remove(wpath)
                    except Exception:
                        pass

        # Step 3: Re-create fresh database and tables
        self._init_db()

        # Step 4: Re-insert salvaged trades
        if salvaged_trades:
            reinserted = 0
            try:
                with self._get_connection() as conn:
                    for t in salvaged_trades:
                        cols = [k for k in t.keys() if k != "id"]
                        if not cols:
                            continue
                        col_names = ", ".join(cols)
                        placeholders = ", ".join(["?"] * len(cols))
                        vals = [t[k] for k in cols]
                        try:
                            conn.execute(f"INSERT INTO trades ({col_names}) VALUES ({placeholders})", vals)
                            reinserted += 1
                        except Exception as ins_err:
                            logger.debug(f"Could not re-insert salvaged trade #{t.get('mt5_ticket')}: {ins_err}")
                    conn.commit()
                logger.info(f"Database auto-recovery successful: restored {reinserted}/{len(salvaged_trades)} trades into fresh {self.db_path}.")
            except Exception as e:
                logger.error(f"Error while re-inserting salvaged trades: {e}")
        else:
            logger.info(f"Created clean {self.db_path}. Active trades and history will be synced from MT5.")

        return True

    def checkpoint(self):
        """Forces a WAL checkpoint to truncate temporary WAL files and prevent corruption on exit."""
        try:
            with self._get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass

    @_auto_recover_on_corruption
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
        strategy: str = "SMC",
        session_name: str = "",
        is_killzone: int = 0,
        setup_type: str = "",
        confluences_json: str = "{}",
        mae_r: float = 0.0,
        exit_reason: str = "",
    ) -> int:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades (
                    timestamp, symbol, direction, entry_price, sl_price, tp_price,
                    realized_r, max_r, confluence_score, status, chart_path, mt5_ticket, comment,
                    parent_ticket, is_pyramid,
                    strategy, session_name, is_killzone, setup_type, confluences_json, mae_r, exit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_str, symbol.upper(), direction.upper(), entry_price, sl_price, tp_price,
                realized_r, max_r, confluence_score, status, chart_path, mt5_ticket, comment,
                parent_ticket, is_pyramid,
                strategy, session_name, is_killzone, setup_type, confluences_json, mae_r, exit_reason
            ))
            conn.commit()
            return cur.lastrowid

    @_auto_recover_on_corruption
    def update_trade_result(self, trade_id: int, realized_r: float, max_r: float, status: str = "CLOSED"):
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE trades
                SET realized_r = ?, max_r = ?, status = ?
                WHERE id = ?
            """, (realized_r, max_r, status, trade_id))
            conn.commit()

    @_auto_recover_on_corruption
    def get_open_trades(self) -> List[Dict]:
        """Returns all trades currently tracked as OPEN in the journal."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    @_auto_recover_on_corruption
    def set_notion_page_id_by_ticket(self, ticket: int, page_id: str):
        """Associates an MT5 ticket with its synced Notion Page ID."""
        with self._get_connection() as conn:
            conn.execute("UPDATE trades SET notion_page_id = ? WHERE mt5_ticket = ?", (page_id, ticket))
            conn.commit()

    @_auto_recover_on_corruption
    def get_trade_by_ticket(self, ticket: int) -> Optional[Dict]:
        """Retrieves a trade record by MT5 ticket."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE mt5_ticket = ?", (ticket,))
            row = cur.fetchone()
            return dict(row) if row else None

    @_auto_recover_on_corruption
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

    @_auto_recover_on_corruption
    def cancel_trade_by_ticket(self, mt5_ticket: int, comment: str = "Order cancelled in MT5") -> bool:
        """Marks a pending trade record as CANCELLED in trades.db."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE trades
                SET status = 'CANCELLED', comment = ?
                WHERE mt5_ticket = ? AND status = 'OPEN'
            """, (comment, mt5_ticket))
            conn.commit()
            return cur.rowcount > 0

    @_auto_recover_on_corruption
    def update_trade_max_r(self, mt5_ticket: int, max_r: float):
        """Updates the highest R reached (MFE) for an active trade."""
        with self._get_connection() as conn:
            conn.execute("UPDATE trades SET max_r = ? WHERE mt5_ticket = ?", (max_r, mt5_ticket))
            conn.commit()

    @_auto_recover_on_corruption
    def update_trade_close_telemetry(self, mt5_ticket: int, exit_reason: str = "", mae_r: float = 0.0):
        """Updates close-time telemetry fields for a trade."""
        closed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE trades SET exit_reason = ?, mae_r = ?, closed_at = ? WHERE mt5_ticket = ? AND status != 'OPEN'",
                (exit_reason, mae_r, closed_at, mt5_ticket),
            )
            conn.commit()

    @_auto_recover_on_corruption
    def get_closed_trades(self) -> List[Dict]:
        """Returns all closed trades (WIN, LOSS, BE, CLOSED) for analytics."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE status IN ('CLOSED', 'WIN', 'LOSS', 'BE') ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    @_auto_recover_on_corruption
    def get_all_trades(self) -> List[Dict]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    @_auto_recover_on_corruption
    def get_recent_trades(self, limit: int = 5) -> List[Dict]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    @_auto_recover_on_corruption
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
