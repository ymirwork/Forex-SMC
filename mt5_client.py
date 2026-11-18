import logging
from typing import Dict, List, Optional, Set, Tuple
import pandas as pd
import MetaTrader5 as mt5

from config import (
    DEFAULT_RISK_PERCENT,
    MAX_RISK_DEVIATION_TOLERANCE,
    PROP_FIRM_MODE,
    PROP_INITIAL_BALANCE,
    PROP_MAX_DRAWDOWN_PERCENT,
    PROP_SAFETY_BUFFER_PERCENT,
    PROP_RISK_PER_TRADE_PERCENT,
    PROP_MAX_OPEN_TRADES,
    PROP_MAX_SAME_CURRENCY_TRADES,
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

        # 2. Search matching symbols from broker list
        all_symbols = mt5.symbols_get()
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
            risk_percent = PROP_RISK_PER_TRADE_PERCENT

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

    def get_busy_symbols(self) -> Set[str]:
        """
        Returns set of uppercase symbols (both broker names and clean base names)
        that currently have an active open position or pending order in MT5.
        """
        if not self.ensure_connection():
            return set()

        busy = set()
        from config import DEFAULT_SYMBOLS

        # 1. Open market positions
        positions = mt5.positions_get()
        if positions:
            for p in positions:
                busy.add(p.symbol.upper())
                clean = p.symbol.upper().replace("/", "").replace("_", "").replace("-", "").replace(".", "")
                for base in DEFAULT_SYMBOLS:
                    if base.upper() in clean:
                        busy.add(base.upper())

        # 2. Pending limit / stop orders
        orders = mt5.orders_get()
        if orders:
            for o in orders:
                busy.add(o.symbol.upper())
                clean = o.symbol.upper().replace("/", "").replace("_", "").replace("-", "").replace(".", "")
                for base in DEFAULT_SYMBOLS:
                    if base.upper() in clean:
                        busy.add(base.upper())

        return busy

    def get_closed_deal_info(self, ticket: int) -> Optional[Dict]:
        """
        Checks MT5 deal history for a closed position ticket.
        Returns deal outcome info if closed, or None if still open.
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
                "time": exit_deal.time,
            }

        return None

    def move_sl_to_breakeven(self, ticket: int) -> Tuple[bool, str]:
        """
        Moves the Stop Loss of an active position to its entry price (Break-Even).
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

        logger.info(f"Stop Loss for #{ticket} ({p.symbol}) moved to Break-Even: {be_price}")
        return True, f"✅ Stop Loss dla pozycji #{ticket} ({p.symbol}) przestawiony na Break-Even ({be_price})!"

    @staticmethod
    def _parse_currencies(symbol_clean: str) -> Tuple[str, str]:
        """
        Parses base and quote currency from standard Forex/Metals pairs.
        E.g. EURUSD -> ('EUR', 'USD'), XAUUSD -> ('XAU', 'USD'), USDJPY -> ('USD', 'JPY')
        """
        sym = symbol_clean.upper()
        for sfx in [".PRO", ".RAW", ".ECN", "PRO.", "RAW.", "M."]:
            sym = sym.replace(sfx, "")
        sym = sym.replace("/", "").replace("_", "").replace("-", "")

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

    def can_open_new_trade(self, symbol: str, sl_distance: float = 0.0) -> Tuple[bool, str]:
        """
        Enforces Prop Firm risk boundaries and Scalable Lot Risk:
        1. Static Max Drawdown Breach
        2. Safety Halt threshold
        3. Max open concurrent trades
        4. Currency correlation limit
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

        if PROP_MAX_SAME_CURRENCY_TRADES > 0:
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

