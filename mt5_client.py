from datetime import datetime
import logging
import time
from typing import Dict, List, Optional, Set, Tuple
import pandas as pd
import MetaTrader5 as mt5

from config import (
    ADD_SPREAD_TO_SL,
    AUTO_BE_OFFSET_PIPS,
    DEFAULT_RISK_PERCENT,
    DEFAULT_SPREAD_PIPS,
    ENABLE_PYRAMIDING,
    MAX_PYRAMID_LEVEL,
    MAX_RISK_DEVIATION_TOLERANCE,
    MAX_RISK_PERCENT,
    PROP_FIRM_MODE,
    PROP_INITIAL_BALANCE,
    PROP_MAX_DRAWDOWN_PERCENT,
    PROP_MAX_OPEN_TRADES,
    PROP_MAX_SAME_CURRENCY_TRADES,
    PROP_RISK_PER_TRADE_PERCENT,
    PROP_SAFETY_BUFFER_PERCENT,
    PYRAMID_GRADE_WEIGHTS,
    PYRAMID_LEVEL_DECAY,
    PYRAMID_LOCK_PREVIOUS_PROFIT,
    PYRAMID_MAX_RISK_FINANCE_RATIO,
    PYRAMID_MIN_PROFIT_R,
    PYRAMID_MIN_RR_TO_TP,
    clean_symbol,
)

logger = logging.getLogger("ForexSMC.MT5Client")


class MT5Client:
    """Robust MetaTrader 5 API client for data retrieval, symbol resolution, and order execution."""

    TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
        "MN1": mt5.TIMEFRAME_MN1,
    }

    def __init__(
        self,
        path: Optional[str] = None,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
    ):
        self.path = path
        self.login = login
        self.password = password
        self.server = server
        self.is_connected = False
        self._symbol_cache: Dict[str, str] = {}
        self._all_broker_symbols = None

    def connect(self) -> bool:
        """Connects to MetaTrader 5 terminal."""
        kwargs = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login and self.password and self.server:
            kwargs["login"] = self.login
            kwargs["password"] = self.password
            kwargs["server"] = self.server

        logger.info("Initializing MetaTrader 5 connection...")
        if kwargs:
            res = mt5.initialize(**kwargs)
        else:
            res = mt5.initialize()

        if not res:
            err = mt5.last_error()
            logger.error(f"MT5 initialization failed: {err}")
            self.is_connected = False
            return False

        term_info = mt5.terminal_info()
        acc_info = mt5.account_info()
        if acc_info is None:
            logger.warning("MT5 connected to terminal, but no trading account is logged in.")
        else:
            logger.info(
                f"MT5 connected successfully to {acc_info.company} | Login: {acc_info.login} "
                f"| Balance: {acc_info.balance:.2f} {acc_info.currency}"
            )

        self.is_connected = True
        return True

    def disconnect(self):
        """Disconnects from MT5."""
        if self.is_connected:
            mt5.shutdown()
            self.is_connected = False
            self._all_broker_symbols = None
            logger.info("MT5 disconnected.")

    def ensure_connection(self) -> bool:
        """Checks connection health, reconnects if needed."""
        term_info = mt5.terminal_info()
        if term_info is None or not term_info.connected:
            logger.warning("MT5 connection lost or uninitialized, reconnecting...")
            return self.connect()
        return True

    def resolve_symbol(self, base_symbol: str) -> Optional[str]:
        """
        Dynamically resolves broker-specific symbol names:
        - Detects suffixes (e.g., EURUSD.pro, XAUUSDp, GBPUSD.f, USDJPY_i)
        - Detects prefixes (e.g., pro.EURUSD, m.XAUUSD, ecn_GBPUSD)
        - Detects combined prefix + suffix (e.g., pro.EURUSD.raw)
        - Prioritizes active, tradeable instruments over disabled archives
        - Adds the resolved symbol to Market Watch automatically
        """
        if base_symbol in self._symbol_cache:
            return self._symbol_cache[base_symbol]

        if not self.ensure_connection():
            return None

        # 1. Direct check
        info = mt5.symbol_info(base_symbol)
        if info is not None and info.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED:
            mt5.symbol_select(base_symbol, True)
            self._symbol_cache[base_symbol] = base_symbol
            return base_symbol

        # 2. Search matching symbols from broker list (cached per session)
        if self._all_broker_symbols is None:
            self._all_broker_symbols = mt5.symbols_get()
        all_symbols = self._all_broker_symbols
        if not all_symbols:
            logger.error("Failed to retrieve symbol list from MT5.")
            return None

        clean_base = base_symbol.upper().replace("/", "").replace("_", "").replace("-", "").replace(".", "")

        exact_candidates = []
        prefix_suffix_candidates = []

        for s in all_symbols:
            name_upper = s.name.upper()
            name_clean = name_upper.replace("/", "").replace("_", "").replace("-", "").replace(".", "")
            is_tradeable = (s.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED)

            if name_clean == clean_base:
                exact_candidates.append((s.name, is_tradeable, len(s.name)))
            elif clean_base in name_clean:
                # Avoid false positives like stock 'GOLDMANS' when searching for 'GOLD'
                if clean_base == "GOLD" and "GOLDMAN" in name_upper:
                    continue
                prefix_suffix_candidates.append((s.name, is_tradeable, len(s.name)))

        # Priority: Exact match first, then prefix/suffix. Always prefer tradeable instruments, then shortest name.
        pool = exact_candidates if exact_candidates else prefix_suffix_candidates
        if pool:
            pool.sort(key=lambda x: (not x[1], x[2]))
            best_match = pool[0][0]
            mt5.symbol_select(best_match, True)
            self._symbol_cache[base_symbol] = best_match
            logger.info(f"Resolved symbol '{base_symbol}' -> broker symbol '{best_match}'")
            return best_match

        logger.warning(f"Could not resolve broker symbol for '{base_symbol}'")
        return None

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe_str: str = "M15",
        n_bars: int = 300,
    ) -> Optional[pd.DataFrame]:
        """Fetches OHLCV candlestick data as a clean pandas DataFrame."""
        if not self.ensure_connection():
            return None

        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return None

        tf = self.TIMEFRAME_MAP.get(timeframe_str.upper(), mt5.TIMEFRAME_M15)
        rates = mt5.copy_rates_from_pos(resolved, tf, 0, n_bars)

        if rates is None or len(rates) == 0:
            logger.error(f"Failed to fetch rates for {resolved} ({timeframe_str}): {mt5.last_error()}")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        # Rename tick_volume to volume for smc compatibility
        df = df.rename(columns={"tick_volume": "volume"})
        return df

    def get_account_info(self) -> Optional[Dict]:
        """Returns account balance, equity, margin and currency."""
        if not self.ensure_connection():
            return None

        acc = mt5.account_info()
        if acc is None:
            return None

        return {
            "login": acc.login,
            "name": acc.name,
            "company": acc.company,
            "server": acc.server,
            "balance": acc.balance,
            "equity": acc.equity,
            "margin_free": acc.margin_free,
            "currency": acc.currency,
            "leverage": acc.leverage,
        }

    def calculate_lot_size(
        self,
        symbol: str,
        risk_percent: float,
        sl_price: float,
        entry_price: Optional[float] = None,
    ) -> float:
        """
        Calculates exact lot size based on account balance and stop loss distance.
        Uses MT5 native trade_tick_value and trade_tick_size.
        """
        if not self.ensure_connection():
            return 0.01

        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return 0.01

        acc = mt5.account_info()
        sym = mt5.symbol_info(resolved)
        if acc is None or sym is None:
            return 0.01

        if entry_price is None or entry_price <= 0:
            tick = mt5.symbol_info_tick(resolved)
            if tick is None:
                return sym.volume_min
            entry_price = tick.ask if sl_price < tick.ask else tick.bid

        sl_distance = abs(entry_price - sl_price)
        if sl_distance <= 0:
            return sym.volume_min

        tick_size = sym.trade_tick_size if sym.trade_tick_size > 0 else sym.point
        tick_value = sym.trade_tick_value if sym.trade_tick_value > 0 else 1.0

        ticks = sl_distance / tick_size
        loss_per_one_lot = ticks * tick_value

        if loss_per_one_lot <= 0:
            return sym.volume_min

        if PROP_FIRM_MODE:
            risk_percent = min(risk_percent, PROP_RISK_PER_TRADE_PERCENT)
        elif risk_percent > MAX_RISK_PERCENT:
            logger.warning(
                f"Requested risk {risk_percent}% exceeds MAX_RISK_PERCENT ({MAX_RISK_PERCENT}%). Capping risk."
            )
            risk_percent = MAX_RISK_PERCENT

        risk_amount = acc.balance * (risk_percent / 100.0)
        raw_lot = risk_amount / loss_per_one_lot

        # Step and limits alignment
        vol_step = sym.volume_step if sym.volume_step > 0 else 0.01
        vol_min = sym.volume_min if sym.volume_min > 0 else 0.01
        vol_max = sym.volume_max if sym.volume_max > 0 else 100.0

        steps = round(raw_lot / vol_step)
        calculated_lot = round(steps * vol_step, 2)
        final_lot = max(vol_min, min(calculated_lot, vol_max))

        logger.info(
            f"Lot calculated for {resolved}: Balance={acc.balance} {acc.currency}, Risk={risk_percent}%, "
            f"SL Distance={sl_distance:.5f} ({ticks:.1f} ticks), Lot={final_lot}"
        )
        return final_lot

    def _determine_filling_mode(self, symbol_info) -> int:
        """
        Determines supported filling mode for broker based on symbol_info.filling_mode bitmask:
        Bit 0 (1): SYMBOL_FILLING_FOK -> ORDER_FILLING_FOK (0)
        Bit 1 (2): SYMBOL_FILLING_IOC -> ORDER_FILLING_IOC (1)
        Default/Fallback: ORDER_FILLING_RETURN (2)
        """
        filling_mode = getattr(symbol_info, "filling_mode", 0)
        if filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        elif filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        else:
            return mt5.ORDER_FILLING_RETURN

    def execute_market_order(
        self,
        symbol: str,
        direction: str,  # "BUY" or "SELL"
        volume: float,
        sl: float,
        tp: float,
        comment: str = "SMC SemiAuto",
    ) -> Tuple[bool, str, Optional[int], float]:
        """Executes a market order in MT5 with SL/TP and automatic filling mode fallback."""
        if not self.ensure_connection():
            return False, "Not connected to MT5", None, 0.0

        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return False, f"Unknown symbol: {symbol}", None, 0.0

        sym_info = mt5.symbol_info(resolved)
        tick = mt5.symbol_info_tick(resolved)
        if sym_info is None or tick is None:
            return False, f"Failed to get tick info for {resolved}", None, 0.0

        is_buy = direction.upper() == "BUY"
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid

        # Validate volume bounds
        volume = max(sym_info.volume_min, min(volume, sym_info.volume_max))

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": resolved,
            "volume": volume,
            "type": order_type,
            "price": round(price, sym_info.digits),
            "sl": round(sl, sym_info.digits),
            "tp": round(tp, sym_info.digits),
            "deviation": 20,
            "magic": 777001,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        # Try candidate filling modes if broker rejects with retcode 10030
        candidates = [
            self._determine_filling_mode(sym_info),
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN,
        ]
        seen = set()
        fill_modes = [f for f in candidates if not (f in seen or seen.add(f))]

        result = None
        for fill_mode in fill_modes:
            request["type_filling"] = fill_mode
            result = mt5.order_send(request)
            if result is None:
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                break
            if result.retcode != 10030:
                break

        if result is None:
            err = mt5.last_error()
            return False, f"order_send() returned None: {err}", None, 0.0

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"Execution failed: retcode={result.retcode} ({result.comment})", None, 0.0

        fill_price = float(result.price) if getattr(result, "price", 0.0) > 0 else float(price)
        logger.info(
            f"Market {direction} executed on {resolved}! Ticket: {result.order}, Volume: {volume}, Price: {fill_price}"
        )
        return True, f"Success! Order #{result.order} opened at {fill_price}", result.order, fill_price

    def execute_limit_order(
        self,
        symbol: str,
        direction: str,  # "BUY" or "SELL"
        price: float,
        volume: float,
        sl: float,
        tp: float,
        comment: str = "SMC Limit",
    ) -> Tuple[bool, str, Optional[int], float]:
        """Places a pending limit order at the specified entry price with filling fallback."""
        if not self.ensure_connection():
            return False, "Not connected to MT5", None, 0.0

        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return False, f"Unknown symbol: {symbol}", None, 0.0

        sym_info = mt5.symbol_info(resolved)
        if sym_info is None:
            return False, f"Failed to get symbol info for {resolved}", None, 0.0

        is_buy = direction.upper() == "BUY"
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT

        volume = max(sym_info.volume_min, min(volume, sym_info.volume_max))

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": resolved,
            "volume": volume,
            "type": order_type,
            "price": round(price, sym_info.digits),
            "sl": round(sl, sym_info.digits),
            "tp": round(tp, sym_info.digits),
            "deviation": 20,
            "magic": 777002,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        candidates = [
            self._determine_filling_mode(sym_info),
            mt5.ORDER_FILLING_RETURN,
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
        ]
        seen = set()
        fill_modes = [f for f in candidates if not (f in seen or seen.add(f))]

        result = None
        for fill_mode in fill_modes:
            request["type_filling"] = fill_mode
            result = mt5.order_send(request)
            if result is None:
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                break
            if result.retcode != 10030:
                break

        if result is None:
            err = mt5.last_error()
            return False, f"order_send() returned None: {err}", None, 0.0

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"Pending order failed: retcode={result.retcode} ({result.comment})", None, 0.0

        logger.info(
            f"Limit {direction} placed on {resolved} at {price}! Ticket: {result.order}, Volume: {volume}"
        )
        return True, f"Limit order #{result.order} placed at {price}", result.order, float(price)

    def get_open_positions(self) -> List[Dict]:
        """Retrieves list of active open positions."""
        if not self.ensure_connection():
            return []

        positions = mt5.positions_get()
        if positions is None:
            return []

        res = []
        for p in positions:
            res.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "comment": p.comment,
                "time": p.time,
            })
        return res

    def get_busy_symbols(self) -> Set[str]:
        """
        Returns set of uppercase symbols (both broker names and clean base names)
        that currently have an active open position or pending order in MT5.
        """
        if not self.ensure_connection():
            return set()

        busy = set()
        from config import DEFAULT_SYMBOLS, SYMBOLS
        all_tracked = set(s.upper() for s in (DEFAULT_SYMBOLS + SYMBOLS))

        # 1. Open market positions
        positions = mt5.positions_get()
        if positions:
            for p in positions:
                busy.add(p.symbol.upper())
                clean = p.symbol.upper().replace("/", "").replace("_", "").replace("-", "").replace(".", "")
                for base in all_tracked:
                    if base in clean:
                        busy.add(base)

        # 2. Pending limit / stop orders
        orders = mt5.orders_get()
        if orders:
            for o in orders:
                busy.add(o.symbol.upper())
                clean = o.symbol.upper().replace("/", "").replace("_", "").replace("-", "").replace(".", "")
                for base in all_tracked:
                    if base in clean:
                        busy.add(base)

        return busy

    def get_closed_deal_info(self, ticket: int) -> Optional[Dict]:
        """
        Checks MT5 deal history for a closed position ticket.
        Returns deal outcome info if closed, cancellation info if cancelled pending order, or None if still open.
        """
        if not self.ensure_connection():
            return None

        # Check if still in open positions
        pos = mt5.positions_get(ticket=ticket)
        if pos is not None and len(pos) > 0:
            return None  # Still open

        # Retrieve closing deal from history
        deals = mt5.history_deals_get(position=ticket)
        if not deals:
            # Check if this was a pending limit order that was cancelled, expired or rejected
            hist_orders = mt5.history_orders_get(ticket=ticket)
            if hist_orders and len(hist_orders) > 0:
                ord_state = getattr(hist_orders[0], "state", -1)
                # mt5.ORDER_STATE_CANCELED = 2, mt5.ORDER_STATE_REJECTED = 5, mt5.ORDER_STATE_EXPIRED = 6
                if ord_state in (
                    mt5.ORDER_STATE_CANCELED,
                    mt5.ORDER_STATE_REJECTED,
                    mt5.ORDER_STATE_EXPIRED,
                ):
                    return {
                        "ticket": ticket,
                        "deal_ticket": ticket,
                        "status": "CANCELLED",
                        "profit": 0.0,
                        "price_close": 0.0,
                        "reason": ord_state,
                        "comment": f"Pending order cancelled (state={ord_state})",
                        "time_open": getattr(hist_orders[0], "time_setup", 0),
                        "time_close": getattr(hist_orders[0], "time_done", 0),
                        "time": getattr(hist_orders[0], "time_done", 0),
                    }
            return None

        # Find the exit deal (entry == DEAL_ENTRY_OUT)
        exit_deal = None
        for d in deals:
            if getattr(d, "entry", -1) == mt5.DEAL_ENTRY_OUT:
                exit_deal = d
                break

        if exit_deal is None and len(deals) >= 2:
            exit_deal = deals[-1]

        if exit_deal:
            return {
                "ticket": ticket,
                "deal_ticket": exit_deal.ticket,
                "profit": exit_deal.profit,
                "price_close": exit_deal.price,
                "reason": exit_deal.reason,  # 4 is SL, 5 is TP, 0/1/3 is Client/Mobile/Expert
                "comment": exit_deal.comment,
                "time_open": deals[0].time if len(deals) > 0 else exit_deal.time,
                "time_close": exit_deal.time,
                "time": exit_deal.time,
            }

        return None

    def get_initial_sl(self, ticket: int) -> float:
        """Returns the original Stop Loss set when the position was opened."""
        if not self.ensure_connection():
            return 0.0
        orders = mt5.history_orders_get(position=ticket)
        if orders and len(orders) > 0 and orders[0].sl > 0:
            return float(orders[0].sl)
        pos = mt5.positions_get(ticket=ticket)
        if pos and len(pos) > 0:
            p0 = pos[0]
            val = getattr(p0, "sl", None)
            if val is None and isinstance(p0, dict):
                val = p0.get("sl", 0.0)
            return float(val or 0.0)
        return 0.0

    def get_position_mfe(
        self,
        symbol: str,
        is_buy: bool,
        entry_price: float,
        initial_sl_price: float,
        time_open: int,
        time_close: Optional[int] = None,
    ) -> float:
        """
        Calculates the Maximum Favorable Excursion (Max R) reached by a position
        between time_open and time_close (or current tick time).
        """
        if not self.ensure_connection():
            return 0.0

        risk_dist = abs(entry_price - initial_sl_price)
        if risk_dist <= 0:
            return 0.0

        resolved = self.resolve_symbol(symbol)
        if time_close is None:
            tick = mt5.symbol_info_tick(resolved)
            time_close = tick.time if tick else int(datetime.now().timestamp())

        if time_open >= time_close:
            return 0.0

        rates = mt5.copy_rates_range(resolved, mt5.TIMEFRAME_M5, time_open, time_close)
        if rates is None or len(rates) == 0:
            rates = mt5.copy_rates_range(resolved, mt5.TIMEFRAME_M1, time_open, time_close)

        if rates is not None and len(rates) > 0:
            if is_buy:
                peak_price = float(max(r["high"] for r in rates))
                favorable_move = max(0.0, peak_price - entry_price)
            else:
                peak_price = float(min(r["low"] for r in rates))
                favorable_move = max(0.0, entry_price - peak_price)
            return round(favorable_move / risk_dist, 2)

        return 0.0

    @staticmethod
    def get_pip_size(symbol: str) -> float:
        """
        Returns pip value in price units:
        - Forex standard (5 digits): 0.0001
        - JPY pairs (3 digits): 0.01
        - Gold (XAU/GOLD): 0.10 (1 pip = $0.10 / 10 cents)
        """
        clean = symbol.upper()
        if "XAU" in clean or "GOLD" in clean:
            return 0.10
        if "JPY" in clean:
            return 0.01
        return 0.0001

    def get_symbol_spread(self, symbol: str) -> float:
        """
        Returns current live spread in price units (Ask - Bid).
        Falls back to DEFAULT_SPREAD_PIPS * pip_size if MT5 is unavailable.
        """
        if self.ensure_connection():
            resolved = self.resolve_symbol(symbol)
            if resolved:
                s_info = mt5.symbol_info(resolved)
                if s_info and s_info.ask > 0 and s_info.bid > 0:
                    spread_price = s_info.ask - s_info.bid
                    if spread_price > 0:
                        return spread_price

        pip_size = self.get_pip_size(symbol)
        return DEFAULT_SPREAD_PIPS * pip_size

    def move_sl_to_breakeven(self, ticket: int, offset_pips: Optional[float] = None) -> Tuple[bool, str]:
        """
        Moves the Stop Loss of an active position to its entry price + offset_pips into profit.
        offset_pips covers broker commission, swap, and spread costs so the trade exits risk-free in slight profit.
        """
        if not self.ensure_connection():
            return False, "Brak połączenia z MT5"

        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return False, f"Pozycja #{ticket} nie została znaleziona w otwartych pozycjach."

        p = pos[0]
        sym_info = mt5.symbol_info(p.symbol)
        if not sym_info:
            return False, f"Brak danych symbolu {p.symbol}"

        if offset_pips is None:
            offset_pips = AUTO_BE_OFFSET_PIPS

        pip_size = self.get_pip_size(p.symbol)
        offset_distance = offset_pips * pip_size

        # Clamp offset so it never exceeds 20% of initial risk distance on tight stops
        orig_sl = self.get_initial_sl(ticket)
        if orig_sl > 0 and p.price_open > 0:
            initial_dist = abs(p.price_open - orig_sl)
            offset_distance = min(offset_distance, initial_dist * 0.20)

        is_buy = p.type == mt5.ORDER_TYPE_BUY

        # Calculate BE with profit offset to cover costs
        if is_buy:
            be_price = round(p.price_open + offset_distance, sym_info.digits)
            # Safety checks: cannot place SL at or above current market Bid
            if be_price >= p.price_current:
                be_price = round(p.price_open, sym_info.digits)
        else:
            be_price = round(p.price_open - offset_distance, sym_info.digits)
            # Safety checks: cannot place SL at or below current market Ask
            if be_price <= p.price_current:
                be_price = round(p.price_open, sym_info.digits)

        # Minimum distance from market check (trade_stops_level)
        stops_level = getattr(sym_info, "trade_stops_level", 0) or getattr(sym_info, "stops_level", 0)
        min_stop_points = stops_level if stops_level > 0 else 5
        min_dist = (min_stop_points + 2) * sym_info.point
        if is_buy and (p.price_current - be_price) < min_dist:
            be_price = round(p.price_open, sym_info.digits)
        elif not is_buy and (be_price - p.price_current) < min_dist:
            be_price = round(p.price_open, sym_info.digits)

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "symbol": p.symbol,
            "sl": be_price,
            "tp": p.tp,
        }

        result = mt5.order_send(request)
        if result is None:
            err = mt5.last_error()
            return False, f"Błąd modyfikacji: {err}"

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"Nie udało się przestawić na BE: retcode={result.retcode} ({result.comment})"

        offset_desc = f" (+{offset_pips:.1f} pips na pokrycie kosztów)" if offset_pips > 0 else ""
        logger.info(f"Stop Loss for #{ticket} ({p.symbol}) moved to Break-Even: {be_price}{offset_desc}")
        return True, f"✅ Stop Loss dla pozycji #{ticket} ({p.symbol}) przestawiony na Break-Even ({be_price}){offset_desc}!"

    @staticmethod
    def is_position_at_be(
        entry: float,
        sl: float,
        is_buy: bool,
        initial_sl_dist: float,
        tolerance_ratio: float = 0.15,
    ) -> bool:
        """
        Checks whether Stop Loss is at Break-Even or in profit.
        Requires sl > 0 for both BUY and SELL.
        """
        if sl <= 0 or entry <= 0 or initial_sl_dist <= 0:
            return False

        if is_buy:
            return sl >= (entry - (initial_sl_dist * tolerance_ratio))
        else:
            return sl <= (entry + (initial_sl_dist * tolerance_ratio))

    @staticmethod
    def _parse_currencies(symbol_clean: str) -> Tuple[str, str]:
        """
        Parses base and quote currency from standard Forex/Metals pairs.
        E.g. EURUSD -> ('EUR', 'USD'), XAUUSD -> ('XAU', 'USD'), USDJPY -> ('USD', 'JPY')
        """
        sym = clean_symbol(symbol_clean)

        if len(sym) == 6:
            return sym[:3], sym[3:]
        elif "XAU" in sym:
            return "XAU", sym.replace("XAU", "")
        elif "XAG" in sym:
            return "XAG", sym.replace("XAG", "")
        return sym, ""

    def get_prop_metrics(self) -> dict:
        """
        Calculates Prop Firm status based on Static Drawdown rules.
        """
        if not self.ensure_connection():
            return {}
        acc = mt5.account_info()
        if acc is None:
            return {}

        initial_bal = PROP_INITIAL_BALANCE if PROP_INITIAL_BALANCE > 0 else acc.balance
        max_dd_pct = PROP_MAX_DRAWDOWN_PERCENT
        safety_dd_pct = PROP_SAFETY_BUFFER_PERCENT

        max_loss_usd = initial_bal * (max_dd_pct / 100.0)
        breach_level_usd = max(0.0, initial_bal - max_loss_usd)

        current_equity = acc.equity
        current_balance = acc.balance

        # Static Drawdown: loss relative to initial balance
        current_loss_usd = max(0.0, initial_bal - current_equity)
        current_dd_pct = (current_loss_usd / initial_bal) * 100.0 if initial_bal > 0 else 0.0

        # Remaining buffer to breach (in USD and %)
        remaining_buffer_usd = max(0.0, current_equity - breach_level_usd)
        remaining_buffer_pct = (remaining_buffer_usd / initial_bal) * 100.0 if initial_bal > 0 else 0.0

        is_breached = current_equity <= breach_level_usd
        is_safety_halt = current_dd_pct >= safety_dd_pct

        open_positions = self.get_open_positions()

        return {
            "prop_mode": PROP_FIRM_MODE,
            "initial_balance": initial_bal,
            "current_balance": current_balance,
            "current_equity": current_equity,
            "currency": acc.currency,
            "max_dd_pct": max_dd_pct,
            "safety_dd_pct": safety_dd_pct,
            "max_loss_usd": max_loss_usd,
            "breach_level_usd": breach_level_usd,
            "current_loss_usd": current_loss_usd,
            "current_dd_pct": current_dd_pct,
            "remaining_buffer_usd": remaining_buffer_usd,
            "remaining_buffer_pct": remaining_buffer_pct,
            "is_breached": is_breached,
            "is_safety_halt": is_safety_halt,
            "open_positions_count": len(open_positions),
            "max_open_trades": PROP_MAX_OPEN_TRADES,
            "risk_per_trade_pct": PROP_RISK_PER_TRADE_PERCENT,
        }

    def can_open_new_trade(
        self,
        symbol: str,
        sl_distance: float = 0.0,
        is_pyramid: bool = False,
    ) -> Tuple[bool, str]:
        """
        Enforces Prop Firm risk boundaries and Scalable Lot Risk:
        1. Static Max Drawdown Breach
        2. Safety Halt threshold
        3. Max open concurrent trades
        4. Currency correlation limit (bypassed if scaling into existing position)
        5. Scalable % Risk vs Minimum Lot Constraint
        """
        if not PROP_FIRM_MODE:
            return True, "OK"

        metrics = self.get_prop_metrics()
        if not metrics:
            return True, "OK"

        if metrics["is_breached"]:
            return (
                False,
                f"🚨 PROG DRAWDOWNU PRZEKROCZONY! Equity ({metrics['current_equity']:.2f} {metrics['currency']}) "
                f"spadło poniżej limitu ({metrics['breach_level_usd']:.2f} {metrics['currency']}).",
            )

        if metrics["is_safety_halt"]:
            return (
                False,
                f"🛡️ PROG BEZPIECZEŃSTWA PROP FIRM! Strata: {metrics['current_dd_pct']:.2f}% "
                f"(Próg ostrzegawczy: {metrics['safety_dd_pct']}%, Max: {metrics['max_dd_pct']}%). "
                f"Nowe zlecenia zostały zablokowane.",
            )

        open_positions = self.get_open_positions()
        if len(open_positions) >= PROP_MAX_OPEN_TRADES:
            return (
                False,
                f"⚠️ Osiągnięto limit otwartych pozycji: {len(open_positions)}/{PROP_MAX_OPEN_TRADES}. "
                f"Poczekaj na zamknięcie lub przestawienie pozycji na BE.",
            )

        # Correlation shield is only for BRAND NEW currency exposure.
        # If scaling into an existing trade (pyramid), we are not adding new currency pairs.
        if PROP_MAX_SAME_CURRENCY_TRADES > 0 and not is_pyramid:
            base_curr, quote_curr = self._parse_currencies(symbol)
            base_count = 0
            quote_count = 0
            for p in open_positions:
                p_base, p_quote = self._parse_currencies(p["symbol"])
                if base_curr and base_curr in (p_base, p_quote):
                    base_count += 1
                if quote_curr and quote_curr in (p_base, p_quote):
                    quote_count += 1

            if base_curr and base_count >= PROP_MAX_SAME_CURRENCY_TRADES:
                return (
                    False,
                    f"⚠️ Tarcza korelacji: Waluta {base_curr} występuje już w {base_count} pozycjach (Max: {PROP_MAX_SAME_CURRENCY_TRADES}).",
                )
            if quote_curr and quote_count >= PROP_MAX_SAME_CURRENCY_TRADES:
                return (
                    False,
                    f"⚠️ Tarcza korelacji: Waluta {quote_curr} występuje już w {quote_count} pozycjach (Max: {PROP_MAX_SAME_CURRENCY_TRADES}).",
                )

        # 5. Scalable Minimum Lot Risk Check (% scaling)
        if sl_distance > 0:
            resolved = self.resolve_symbol(symbol)
            sym_info = mt5.symbol_info(resolved) if resolved else None
            acc_info = mt5.account_info()
            if sym_info and acc_info and acc_info.balance > 0:
                vol_min = sym_info.volume_min if sym_info.volume_min > 0 else 0.01
                tick_size = sym_info.trade_tick_size if sym_info.trade_tick_size > 0 else sym_info.point
                tick_val = sym_info.trade_tick_value if sym_info.trade_tick_value > 0 else 1.0

                ticks = sl_distance / tick_size
                loss_at_min_lot = ticks * tick_val * vol_min

                target_risk_pct = PROP_RISK_PER_TRADE_PERCENT if PROP_FIRM_MODE else DEFAULT_RISK_PERCENT
                target_risk_usd = acc_info.balance * (target_risk_pct / 100.0)
                max_tolerated_risk_usd = target_risk_usd * MAX_RISK_DEVIATION_TOLERANCE

                if loss_at_min_lot > max_tolerated_risk_usd:
                    actual_risk_pct = (loss_at_min_lot / acc_info.balance) * 100.0
                    return (
                        False,
                        f"🛡️ Ryzyko SL zbyt duże dla salda ({acc_info.balance:.0f} {acc_info.currency}): "
                        f"min. lot ({vol_min}) dałby stratę {loss_at_min_lot:.2f} {acc_info.currency} ({actual_risk_pct:.2f}% kapitału), "
                        f"przekraczając dopuszczalny limit {target_risk_pct}% (max z tolerancją: {max_tolerated_risk_usd:.2f} {acc_info.currency}).",
                    )

        return True, "OK"

    def get_pyramid_cluster_status(self, symbol: str) -> dict:
        """
        Checks if an active position cluster on `symbol` qualifies for risk-neutral pyramiding:
        1. Open positions count < MAX_PYRAMID_LEVEL.
        2. No pending orders for this symbol (avoids duplicate scale-in entries).
        3. Every existing position in the cluster has reached at least +PYRAMID_MIN_PROFIT_R
           and has its SL at or better than Break-Even (0 risk).
        """
        if not ENABLE_PYRAMIDING or not self.ensure_connection():
            return {"is_eligible": False, "count": 0}

        clean_sym = clean_symbol(symbol)
        open_positions = self.get_open_positions()

        sym_positions = [
            p for p in open_positions
            if clean_symbol(p["symbol"]) == clean_sym
        ]

        # Check pending orders for this symbol (Audit & Review fix)
        pending_orders = mt5.orders_get()
        if pending_orders:
            sym_pending = [
                o for o in pending_orders
                if clean_symbol(o.symbol) == clean_sym
            ]
            if sym_pending:
                return {
                    "is_eligible": False,
                    "count": len(sym_positions),
                    "has_pending": True,
                    "pending_ticket": sym_pending[0].ticket,
                    "reason": f"Oczekuje na realizację zlecenia limit/stop #{sym_pending[0].ticket}",
                }

        count = len(sym_positions)
        if count == 0:
            return {"is_eligible": False, "count": 0}

        if count >= MAX_PYRAMID_LEVEL:
            return {
                "is_eligible": False,
                "count": count,
                "reason": f"Osiągnięto limit piramidy ({count}/{MAX_PYRAMID_LEVEL})"
            }

        # Sort positions chronologically by open time (L1 = base)
        sym_positions.sort(key=lambda p: p.get("time", 0))

        base_pos = sym_positions[0]
        direction = base_pos["type"]
        is_buy = direction == "BUY"

        # Verify all existing positions meet profit and BE requirements
        for idx, pos in enumerate(sym_positions):
            ticket = pos["ticket"]
            entry = pos["price_open"]
            curr = pos["price_current"]
            sl = pos["sl"]

            orig_sl = self.get_initial_sl(ticket)
            if orig_sl <= 0 and sl > 0:
                orig_sl = sl
            if orig_sl <= 0 or entry <= 0:
                return {"is_eligible": False, "count": count, "reason": f"Brak SL na pozycji L{idx+1} #{ticket}"}

            initial_sl_dist = abs(entry - orig_sl)
            if initial_sl_dist <= 0:
                return {"is_eligible": False, "count": count, "reason": f"Nieprawidłowy dystans SL na #{ticket}"}

            favorable_move = (curr - entry) if is_buy else (entry - curr)
            pos_profit_r = favorable_move / initial_sl_dist
            pos["profit_r"] = round(pos_profit_r, 2)

            is_be = self.is_position_at_be(entry, sl, is_buy, initial_sl_dist, tolerance_ratio=0.15)
            if pos_profit_r < PYRAMID_MIN_PROFIT_R or not is_be:
                return {
                    "is_eligible": False,
                    "count": count,
                    "direction": direction,
                    "profit_r": round(pos_profit_r, 2),
                    "is_be": is_be,
                    "reason": f"Pozycja L{idx+1} #{ticket} ({pos_profit_r:.1f}R, BE={is_be}) < wymagane {PYRAMID_MIN_PROFIT_R}R na BE",
                }

        # Master TP: take from base trade if set, else furthest TP in profit direction
        master_tp = base_pos.get("tp", 0.0)
        if master_tp <= 0:
            all_tps = [p.get("tp", 0.0) for p in sym_positions if p.get("tp", 0.0) > 0]
            if all_tps:
                master_tp = max(all_tps) if is_buy else min(all_tps)

        cluster_tickets = [p["ticket"] for p in sym_positions]
        total_profit_usd = sum(p["profit"] for p in sym_positions)

        return {
            "is_eligible": True,
            "count": count,
            "current_level": count,
            "next_level": count + 1,
            "direction": direction,
            "base_ticket": base_pos["ticket"],
            "base_entry": base_pos["price_open"],
            "base_sl": base_pos["sl"],
            "master_tp": master_tp,
            "cluster_tickets": cluster_tickets,
            "positions": sym_positions,
            "profit_r": sym_positions[0]["profit_r"],
            "profit_usd": total_profit_usd,
        }

    def get_pyramid_status(self, symbol: str) -> dict:
        """Backward-compatibility alias for get_pyramid_cluster_status."""
        return self.get_pyramid_cluster_status(symbol)

    def calculate_pyramid_position_size(
        self,
        symbol: str,
        entry_price: float,
        sl_price: float,
        setup_grade: str,
        cluster_status: dict,
    ) -> Tuple[float, float, float, str]:
        """
        Calculates mathematically risk-neutral lot size for scale-in:
        1. Evaluates new locked profit when earlier positions are trailed to sl_price (new_sl).
        2. Requires new_sl to strictly protect base entry (for BUY: new_sl > base_entry + buffer).
        3. Caps risk at PYRAMID_MAX_RISK_FINANCE_RATIO (75%) of locked profit.
        4. Scales by level decay (L2=50%, L3=25%) and setup grade (A+=100%, A=75%, B=50%).
        Returns: (lot, locked_profit_usd, allowed_risk_usd, reason)
        """
        if not self.ensure_connection():
            return 0.0, 0.0, 0.0, "Brak połączenia z MT5"

        resolved = self.resolve_symbol(symbol)
        sym_info = mt5.symbol_info(resolved)
        acc_info = mt5.account_info()
        if not sym_info or not acc_info:
            return 0.0, 0.0, 0.0, "Brak danych symbolu lub konta"

        positions = cluster_status.get("positions", [])
        if not positions:
            return 0.0, 0.0, 0.0, "Brak pozycji klastra"

        is_buy = cluster_status.get("direction") == "BUY"
        pip_size = self.get_pip_size(resolved)
        buffer = pip_size * AUTO_BE_OFFSET_PIPS

        # Check: new_sl must protect base entry! (Review finding 2)
        base_pos = positions[0]
        base_entry = base_pos["price_open"]
        if is_buy and sl_price <= (base_entry + buffer * 0.5):
            return 0.0, 0.0, 0.0, f"Nowy SL ({sl_price}) nie chroni wejścia pozycji bazowej ({base_entry})"
        elif not is_buy and sl_price >= (base_entry - buffer * 0.5):
            return 0.0, 0.0, 0.0, f"Nowy SL ({sl_price}) nie chroni wejścia pozycji bazowej ({base_entry})"

        # Calculate locked profit across positions when trailed to sl_price (Review finding 1)
        total_locked_usd = 0.0
        tick_value = getattr(sym_info, "trade_tick_value", 1.0) or 1.0
        tick_size = getattr(sym_info, "trade_tick_size", sym_info.point) or sym_info.point
        point = sym_info.point

        for p in positions:
            p_entry = p["price_open"]
            p_vol = p["volume"]
            if is_buy:
                gain_pts = max(0.0, sl_price - p_entry)
            else:
                gain_pts = max(0.0, p_entry - sl_price)
            if tick_size > 0:
                p_locked_usd = (gain_pts / tick_size) * tick_value * p_vol
            else:
                p_locked_usd = (gain_pts / point) * tick_value * p_vol
            total_locked_usd += p_locked_usd

        max_risk_usd = total_locked_usd * PYRAMID_MAX_RISK_FINANCE_RATIO

        # Target risk based on account equity, decay and grade
        level = cluster_status.get("next_level", 2)
        decay = PYRAMID_LEVEL_DECAY.get(level, 0.25)
        grade_weight = PYRAMID_GRADE_WEIGHTS.get(setup_grade.upper(), 0.75)

        base_risk_pct = PROP_RISK_PER_TRADE_PERCENT if PROP_FIRM_MODE else DEFAULT_RISK_PERCENT
        standard_risk_usd = (acc_info.balance * (base_risk_pct / 100.0))
        target_risk_usd = standard_risk_usd * decay * grade_weight

        allowed_risk_usd = min(max_risk_usd, target_risk_usd)
        if allowed_risk_usd <= 0:
            return 0.0, total_locked_usd, 0.0, "Zablokowany zysk nie wystarcza na sfinansowanie ryzyka dokładki"

        # Convert allowed_risk_usd to lot size
        sl_distance = abs(entry_price - sl_price)
        if sl_distance <= 0:
            return 0.0, total_locked_usd, allowed_risk_usd, "Nieprawidłowy dystans SL"

        if tick_size > 0:
            loss_per_lot = (sl_distance / tick_size) * tick_value
        else:
            loss_per_lot = (sl_distance / point) * tick_value

        if loss_per_lot <= 0:
            return 0.0, total_locked_usd, allowed_risk_usd, "Błąd kalkulacji straty na lot"

        calc_lot = allowed_risk_usd / loss_per_lot

        # Round to broker lot step
        vol_min = sym_info.volume_min
        vol_max = sym_info.volume_max
        vol_step = sym_info.volume_step if sym_info.volume_step > 0 else 0.01

        # Strict risk check: if minimum lot exceeds allowed risk, reject pyramid scale-in
        if (vol_min * loss_per_lot) > allowed_risk_usd:
            return 0.0, round(total_locked_usd, 2), round(allowed_risk_usd, 2), (
                f"Minimalny wolumen ({vol_min}) ryzykuje {vol_min * loss_per_lot:.2f} USD, "
                f"przekraczając dozwolone ryzyko dokładki ({allowed_risk_usd:.2f} USD)"
            )

        calc_lot = round(calc_lot / vol_step) * vol_step
        calc_lot = max(vol_min, min(vol_max, calc_lot))
        calc_lot = round(calc_lot, 2)

        return calc_lot, round(total_locked_usd, 2), round(allowed_risk_usd, 2), "OK"

    def sync_cluster_stop_loss(
        self,
        symbol: str,
        new_sl: float,
        cluster_tickets: Optional[List[int]] = None,
    ) -> List[Tuple[int, bool, str]]:
        """
        Synchronizes Stop Loss across all open positions in a pyramid cluster:
        - Checks stops_level to avoid rejection near market price.
        - Moves SL only if new_sl strictly improves the trade's protection.
        """
        results = []
        if not self.ensure_connection():
            return [(0, False, "Brak połączenia z MT5")]

        clean_sym = clean_symbol(symbol)
        open_positions = self.get_open_positions()
        sym_positions = [
            p for p in open_positions
            if clean_symbol(p["symbol"]) == clean_sym
        ]

        if cluster_tickets:
            sym_positions = [p for p in sym_positions if p["ticket"] in cluster_tickets]

        if not sym_positions:
            return [(0, True, "Brak pozycji do synchronizacji")]

        for p in sym_positions:
            ticket = p["ticket"]
            sym_info = mt5.symbol_info(p["symbol"])
            if not sym_info:
                results.append((ticket, False, f"Brak danych symbolu {p['symbol']}"))
                continue

            is_buy = p["type"] == "BUY"
            curr_sl = p["sl"]
            curr_price = p["price_current"]
            target_sl = round(new_sl, sym_info.digits)

            # Check if target_sl improves protection
            should_update = (target_sl > curr_sl) if is_buy else (target_sl < curr_sl or curr_sl <= 0)
            if not should_update:
                results.append((ticket, True, "SL jest już na lepszym poziomie"))
                continue

            # Verify stops_level minimum distance from current market price (Review finding 5)
            stops_level = getattr(sym_info, "trade_stops_level", 0) or getattr(sym_info, "stops_level", 0) or 5
            min_dist = (stops_level + 2) * sym_info.point
            if is_buy and (curr_price - target_sl) < min_dist:
                results.append((ticket, False, f"Zbyt blisko ceny rynkowej (min. {stops_level} pkt)"))
                continue
            elif not is_buy and (target_sl - curr_price) < min_dist:
                results.append((ticket, False, f"Zbyt blisko ceny rynkowej (min. {stops_level} pkt)"))
                continue

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol": p["symbol"],
                "sl": target_sl,
                "tp": p["tp"],
            }
            res = mt5.order_send(request)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"🔒 [CLUSTER SYNC] Pozycja #{ticket} ({p['symbol']}) SL zsynchronizowany na {target_sl}")
                results.append((ticket, True, f"SL przestawiony na {target_sl}"))
            else:
                err_msg = res.comment if res else str(mt5.last_error())
                logger.warning(f"Błąd synchronizacji SL #{ticket}: {err_msg}")
                results.append((ticket, False, err_msg))

        return results

    def trail_sl_to_profit(self, ticket: int, profit_r: float = 0.5) -> Tuple[bool, str]:
        """
        Trails the SL of a parent trade to lock in guaranteed profit (e.g. +0.5R or +1.0R)
        when a pyramid scale-in position is opened.
        """
        if not self.ensure_connection():
            return False, "Brak połączenia z MT5"

        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return False, f"Pozycja #{ticket} nie została znaleziona"

        p = pos[0]
        sym_info = mt5.symbol_info(p.symbol)
        if not sym_info:
            return False, f"Brak danych symbolu {p.symbol}"

        is_buy = p.type == mt5.POSITION_TYPE_BUY
        entry = p.price_open
        current_sl = p.sl

        orig_sl = self.get_initial_sl(ticket)
        initial_dist = abs(entry - orig_sl) if (orig_sl > 0 and entry > 0) else abs(entry - current_sl)
        if initial_dist <= 0:
            initial_dist = sym_info.point * 100

        lock_dist = initial_dist * profit_r

        target_sl = (entry + lock_dist) if is_buy else (entry - lock_dist)
        target_sl = round(target_sl, sym_info.digits)

        # Only update if target_sl improves the current SL
        should_update = (target_sl > current_sl) if is_buy else (target_sl < current_sl or current_sl <= 0)
        if not should_update:
            return True, "SL jest już na lepszym poziomie zabezpieczenia"

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "symbol": p.symbol,
            "sl": target_sl,
            "tp": p.tp,
        }

        res = mt5.order_send(request)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Pozycja #{ticket} ({p.symbol}) zabezpieczona na zysku +{profit_r}R ({target_sl})")
            return True, f"Zabezpieczono zysk +{profit_r}R na pozycji #{ticket} (SL: {target_sl})"
        return False, f"Błąd modyfikacji SL: {res.comment if res else mt5.last_error()}"

