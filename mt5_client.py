import logging
from typing import Dict, List, Optional, Tuple
import pandas as pd
import MetaTrader5 as mt5

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
        Resolves a symbol to the broker's exact instrument name (handles suffixes like EURUSD.f, XAUUSDp, etc.).
        """
        if base_symbol in self._symbol_cache:
            return self._symbol_cache[base_symbol]

        if not self.ensure_connection():
            return None

        # Direct check
        info = mt5.symbol_info(base_symbol)
        if info is not None:
            mt5.symbol_select(base_symbol, True)
            self._symbol_cache[base_symbol] = base_symbol
            return base_symbol

        # Search matching symbols
        all_symbols = mt5.symbols_get()
        if not all_symbols:
            logger.error("Failed to retrieve symbol list from MT5.")
            return None

        clean_base = base_symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        candidates = []
        for s in all_symbols:
            s_clean = s.name.upper()
            if s_clean.startswith(clean_base):
                candidates.append(s.name)

        if not candidates:
            # Fallback: substring match
            for s in all_symbols:
                if clean_base in s.name.upper():
                    candidates.append(s.name)

        if candidates:
            # Prefer shortest matching name (e.g., EURUSD.f over EURUSD_micro_spread)
            best_match = min(candidates, key=len)
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
    ) -> Tuple[bool, str, Optional[int]]:
        """Executes a market order in MT5 with SL/TP and automatic filling mode fallback."""
        if not self.ensure_connection():
            return False, "Not connected to MT5", None

        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return False, f"Unknown symbol: {symbol}", None

        sym_info = mt5.symbol_info(resolved)
        tick = mt5.symbol_info_tick(resolved)
        if sym_info is None or tick is None:
            return False, f"Failed to get tick info for {resolved}", None

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
            return False, f"order_send() returned None: {err}", None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"Execution failed: retcode={result.retcode} ({result.comment})", None

        logger.info(
            f"Market {direction} executed on {resolved}! Ticket: {result.order}, Volume: {volume}, Price: {result.price}"
        )
        return True, f"Success! Order #{result.order} opened at {result.price}", result.order

    def execute_limit_order(
        self,
        symbol: str,
        direction: str,  # "BUY" or "SELL"
        price: float,
        volume: float,
        sl: float,
        tp: float,
        comment: str = "SMC Limit",
    ) -> Tuple[bool, str, Optional[int]]:
        """Places a pending limit order at the specified entry price with filling fallback."""
        if not self.ensure_connection():
            return False, "Not connected to MT5", None

        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return False, f"Unknown symbol: {symbol}", None

        sym_info = mt5.symbol_info(resolved)
        if sym_info is None:
            return False, f"Failed to get symbol info for {resolved}", None

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
            return False, f"order_send() returned None: {err}", None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"Pending order failed: retcode={result.retcode} ({result.comment})", None

        logger.info(
            f"Limit {direction} placed on {resolved} at {price}! Ticket: {result.order}, Volume: {volume}"
        )
        return True, f"Limit order #{result.order} placed at {price}", result.order
        return True, f"Success! Limit Order #{result.order} placed at {price}", result.order

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
            })
        return res
