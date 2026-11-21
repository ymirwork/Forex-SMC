from datetime import datetime
import logging
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from news_filter import NewsFilter
from rr_optimizer import RROptimizer
from session_manager import SessionManager
from smc_detector import (
    ConfluenceItem,
    TradeSetup,
    calculate_stop_loss,
    check_rejection_candle,
    get_symbol_pip_size,
)
from trade_journal import TradeJournal
from config import (
    ADD_SPREAD_TO_SL,
    BLOCK_EVENING_ENTRIES,
    BLOCK_ROLLOVER_SESSION,
    DEFAULT_RR_RATIO,
    LTF_TIMEFRAME,
    MIN_TRADES_FOR_OPTIMAL_RR,
    REQUIRE_REJECTION_CANDLE,
    SL_ATR_MULTIPLIER,
    SL_CALCULATION_MODE,
    SL_HYBRID_MIN_ATR_MULT,
    TIGHT_SL_MODE,
    USE_OPTIMAL_RR,
    ZUMIKO_ENABLE_POC_TARGETING,
    ZUMIKO_FAST_EMA,
    ZUMIKO_REQUIRE_H1,
    ZUMIKO_REQUIRE_VWAP_ALIGNMENT,
    ZUMIKO_RSI_MAX_BUY,
    ZUMIKO_RSI_MIN_SELL,
    ZUMIKO_SLOW_EMA,
    ZUMIKO_SWING_LEN,
    ZUMIKO_TREND_EMA,
    ZUMIKO_USE_HEIKIN_ASHI_FILTER,
)

logger = logging.getLogger("ForexSMC.ZumikoDetector")


class ZumikoDetector:
    """
    Detector for the official Zumiko KillMLM Institutional Strategy (Webinar & PDF):
    1. Trend Baseline: EMA 238 (Market Filter)
    2. Dynamic Ribbon: EMA Fast (9) vs EMA Slow (21) Ribbon Expansion / Pullback Bounce
    3. Institutional Anchor: Daily Volume-Weighted Average Price (VWAP)
    4. Volume Magnet: Point of Control (POC) as high-liquidity target / reaction level
    5. Momentum Filter: RSI (14) overextension shield
    6. Candlestick Smoothing: Heikin-Ashi momentum verification (flat bottom for BUY, flat top for SELL)
    7. Market Structure Swings: Higher Low (HL) for BUY / Lower High (LH) for SELL
    8. Multi-Timeframe Trend Matrix: Validates trend across M15, H1, and H4
    """

    def __init__(
        self,
        fast_ema_len: int = ZUMIKO_FAST_EMA,
        slow_ema_len: int = ZUMIKO_SLOW_EMA,
        trend_ema_len: int = ZUMIKO_TREND_EMA,
        swing_len: int = ZUMIKO_SWING_LEN,
        default_rr: float = DEFAULT_RR_RATIO,
        min_confluence_score: int = 70,
        use_optimal_rr: bool = USE_OPTIMAL_RR,
        min_trades_for_optimal: int = MIN_TRADES_FOR_OPTIMAL_RR,
        require_h1_alignment: bool = ZUMIKO_REQUIRE_H1,
        require_vwap: bool = ZUMIKO_REQUIRE_VWAP_ALIGNMENT,
        rsi_max_buy: float = ZUMIKO_RSI_MAX_BUY,
        rsi_min_sell: float = ZUMIKO_RSI_MIN_SELL,
        use_heikin_ashi: bool = ZUMIKO_USE_HEIKIN_ASHI_FILTER,
        enable_poc: bool = ZUMIKO_ENABLE_POC_TARGETING,
        journal: Optional[TradeJournal] = None,
        news_filter: Optional[NewsFilter] = None,
        fast_ema: Optional[int] = None,
        slow_ema: Optional[int] = None,
        trend_ema: Optional[int] = None,
    ):
        self.fast_ema_len = fast_ema or fast_ema_len
        self.slow_ema_len = slow_ema or slow_ema_len
        self.trend_ema_len = trend_ema or trend_ema_len
        self.swing_len = swing_len
        self.default_rr = default_rr
        self.min_confluence_score = min_confluence_score
        self.use_optimal_rr = use_optimal_rr
        self.min_trades_for_optimal = min_trades_for_optimal
        self.require_h1_alignment = require_h1_alignment
        self.require_vwap = require_vwap
        self.rsi_max_buy = rsi_max_buy
        self.rsi_min_sell = rsi_min_sell
        self.use_heikin_ashi = use_heikin_ashi
        self.enable_poc = enable_poc
        self.news_filter = news_filter or NewsFilter()
        self.journal = journal or TradeJournal()

    def get_effective_rr(self) -> Tuple[float, bool, str]:
        """Determines target RR dynamically using shared RROptimizer logic."""
        return RROptimizer.get_effective_rr(
            journal=self.journal,
            default_rr=self.default_rr,
            use_optimal_rr=self.use_optimal_rr,
            min_trades=self.min_trades_for_optimal,
        )

    def calculate_mtf_trend(
        self,
        df_m15: pd.DataFrame,
        df_h1: Optional[pd.DataFrame],
        df_h4: Optional[pd.DataFrame],
    ) -> Dict[str, str]:
        """
        Calculates Trend Direction (BUY / SELL) across M15, H1, H4 based on EMA 238 and EMA 10/21.
        Returns: {'M15': 'BUY', 'H1': 'BUY', 'H4': 'BUY'}
        """
        matrix: Dict[str, str] = {}

        # M15 Trend
        if df_m15 is not None and len(df_m15) >= 238:
            e238_m15 = df_m15["close"].ewm(span=self.trend_ema_len, adjust=False).mean().iloc[-1]
            c_m15 = df_m15["close"].iloc[-1]
            matrix["M15"] = "BUY" if c_m15 >= e238_m15 else "SELL"
        else:
            matrix["M15"] = "NEUTRAL"

        # H1 Trend
        if df_h1 is not None and len(df_h1) >= 238:
            e238_h1 = df_h1["close"].ewm(span=self.trend_ema_len, adjust=False).mean().iloc[-1]
            c_h1 = df_h1["close"].iloc[-1]
            matrix["H1"] = "BUY" if c_h1 >= e238_h1 else "SELL"
        elif df_h1 is not None and len(df_h1) >= 50:
            # Fallback if fewer bars: EMA 50 vs EMA 200
            e50 = df_h1["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            e200 = df_h1["close"].ewm(span=min(200, len(df_h1)), adjust=False).mean().iloc[-1]
            matrix["H1"] = "BUY" if e50 >= e200 else "SELL"
        else:
            matrix["H1"] = "NEUTRAL"

        # H4 Trend
        if df_h4 is not None and len(df_h4) >= 238:
            e238_h4 = df_h4["close"].ewm(span=self.trend_ema_len, adjust=False).mean().iloc[-1]
            c_h4 = df_h4["close"].iloc[-1]
            matrix["H4"] = "BUY" if c_h4 >= e238_h4 else "SELL"
        elif df_h4 is not None and len(df_h4) >= 50:
            e50 = df_h4["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            e200 = df_h4["close"].ewm(span=min(200, len(df_h4)), adjust=False).mean().iloc[-1]
            matrix["H4"] = "BUY" if e50 >= e200 else "SELL"
        else:
            matrix["H4"] = "NEUTRAL"

        return matrix

    def detect_swings(self, df: pd.DataFrame, swing_len: int = 10) -> Tuple[str, float, str, float]:
        """
        Detects local market structure pivots and classifies them into:
        Highs: HH (Higher High) or LH (Lower High)
        Lows:  HL (Higher Low)  or LL (Lower Low)
        Returns: (last_high_type, last_high_val, last_low_type, last_low_val)
        """
        if df is None or len(df) < (swing_len * 2):
            return "NONE", 0.0, "NONE", 0.0

        highs = df["high"].values
        lows = df["low"].values
        n = len(df)

        swing_highs: List[Tuple[int, float]] = []
        swing_lows: List[Tuple[int, float]] = []

        # Find pivots with swing_len left and at least 2 bars right
        for i in range(swing_len, n - 2):
            # Check high pivot
            is_high = True
            for j in range(max(0, i - swing_len), min(n, i + 3)):
                if j != i and highs[j] > highs[i]:
                    is_high = False
                    break
            if is_high:
                swing_highs.append((i, highs[i]))

            # Check low pivot
            is_low = True
            for j in range(max(0, i - swing_len), min(n, i + 3)):
                if j != i and lows[j] < lows[i]:
                    is_low = False
                    break
            if is_low:
                swing_lows.append((i, lows[i]))

        # Classify high
        last_high_type = "NONE"
        last_high_val = highs[-1]
        if len(swing_highs) >= 2:
            last_high_type = "HH" if swing_highs[-1][1] > swing_highs[-2][1] else "LH"
            last_high_val = swing_highs[-1][1]
        elif len(swing_highs) == 1:
            last_high_val = swing_highs[-1][1]

        # Classify low
        last_low_type = "NONE"
        last_low_val = lows[-1]
        if len(swing_lows) >= 2:
            last_low_type = "HL" if swing_lows[-1][1] > swing_lows[-2][1] else "LL"
            last_low_val = swing_lows[-1][1]
        elif len(swing_lows) == 1:
            last_low_val = swing_lows[-1][1]

        return last_high_type, last_high_val, last_low_type, last_low_val

    @staticmethod
    def calculate_vwap(df: pd.DataFrame) -> float:
        """
        Calculates daily Volume Weighted Average Price (VWAP) from session start.
        Uses typical price = (high + low + close) / 3 weighted by volume.
        Falls back to SMA if volume is 0 or unavailable.
        """
        if df is None or df.empty:
            return 0.0

        vol_col = "tick_volume" if "tick_volume" in df.columns else ("volume" if "volume" in df.columns else None)
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0

        # Group by current calendar day if timestamp column exists
        if "time" in df.columns and len(df) > 1:
            last_date = df["time"].iloc[-1]
            if hasattr(last_date, "date"):
                curr_date = last_date.date()
                mask = df["time"].apply(lambda t: t.date() == curr_date if hasattr(t, "date") else False)
                session_df = df[mask]
                session_tp = typical_price[mask]
            else:
                session_df = df.tail(32)
                session_tp = typical_price.tail(32)
        else:
            session_df = df.tail(32)
            session_tp = typical_price.tail(32)

        if session_df.empty:
            session_df = df.tail(32)
            session_tp = typical_price.tail(32)

        if vol_col and session_df[vol_col].sum() > 0:
            vol = session_df[vol_col].astype(float)
            vwap = (session_tp * vol).sum() / vol.sum()
        else:
            vwap = session_tp.mean()

        return float(vwap)

    @staticmethod
    def calculate_poc(df: pd.DataFrame, lookback: int = 40, bins: int = 30) -> float:
        """
        Calculates Point of Control (POC) - the price level with the highest traded volume
        in the recent lookback window (Volume Profile).
        Acts as a high-liquidity price magnet and key target/reaction zone.
        """
        if df is None or len(df) < 5:
            return 0.0

        sub = df.tail(lookback)
        low_min = float(sub["low"].min())
        high_max = float(sub["high"].max())
        if high_max <= low_min:
            return float(sub["close"].iloc[-1])

        vol_col = "tick_volume" if "tick_volume" in sub.columns else ("volume" if "volume" in sub.columns else None)
        volumes = sub[vol_col].values if (vol_col and sub[vol_col].sum() > 0) else np.ones(len(sub))

        bin_edges = np.linspace(low_min, high_max, bins + 1)
        bin_vols = np.zeros(bins)

        for idx, row in enumerate(sub.itertuples()):
            r_low = getattr(row, "low")
            r_high = getattr(row, "high")
            v = volumes[idx]
            for b_idx in range(bins):
                b_start = bin_edges[b_idx]
                b_end = bin_edges[b_idx + 1]
                if not (r_high < b_start or r_low > b_end):
                    bin_vols[b_idx] += v

        max_bin_idx = int(np.argmax(bin_vols))
        poc_price = (bin_edges[max_bin_idx] + bin_edges[max_bin_idx + 1]) / 2.0
        return float(poc_price)

    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
        """
        Calculates Relative Strength Index (RSI) using standard Wilder smoothing.
        """
        if df is None or len(df) <= period:
            return 50.0

        closes = df["close"]
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(com=period - 1, min_periods=period, adjust=False).mean().iloc[-1]
        avg_loss = loss.ewm(com=period - 1, min_periods=period, adjust=False).mean().iloc[-1]

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return float(rsi)

    @staticmethod
    def calculate_ribbon_expansion(
        fast_ema: pd.Series,
        slow_ema: pd.Series,
        atr: float = 0.0,
    ) -> Tuple[bool, float]:
        """
        Checks if the EMA 9 / EMA 21 Ribbon is expanding (accelerating trend).
        Ribbon width = |EMA 9 - EMA 21|.
        Expanding ribbon indicates institutional momentum, while narrowing ribbon
        signals trend exhaustion or transition into consolidation.
        """
        if len(fast_ema) < 3 or len(slow_ema) < 3:
            return False, 0.0

        w_curr = abs(fast_ema.iloc[-1] - slow_ema.iloc[-1])
        w_prev = abs(fast_ema.iloc[-2] - slow_ema.iloc[-2])
        w_prev2 = abs(fast_ema.iloc[-3] - slow_ema.iloc[-3])

        is_expanding = (w_curr >= w_prev) or (w_curr >= w_prev2)
        return is_expanding, float(w_curr)

    @staticmethod
    def check_heikin_ashi_momentum(df: pd.DataFrame, is_buy: bool) -> Tuple[bool, str]:
        """
        Evaluates Heikin-Ashi candle smoothing and wick structure (ZuMiKo Webinar & PDF).
        - Healthy HA BUY: solid bullish body, upper wick, negligible lower wick (flat bottom).
        - Healthy HA SELL: solid bearish body, lower wick, negligible upper wick (flat top).
        - Rejection check: warns against long opposing wicks on current candle.
        """
        if df is None or len(df) < 5:
            return True, "Brak danych HA"

        sub = df.tail(5).copy()
        ha_close = (sub["open"] + sub["high"] + sub["low"] + sub["close"]) / 4.0

        ha_open = np.zeros(len(sub))
        ha_open[0] = (sub["open"].iloc[0] + sub["close"].iloc[0]) / 2.0
        for i in range(1, len(sub)):
            ha_open[i] = (ha_open[i - 1] + ha_close.iloc[i - 1]) / 2.0

        ha_high = np.maximum.reduce([sub["high"].values, ha_open, ha_close.values])
        ha_low = np.minimum.reduce([sub["low"].values, ha_open, ha_close.values])

        curr_o = ha_open[-1]
        curr_c = ha_close.iloc[-1]
        curr_h = ha_high[-1]
        curr_l = ha_low[-1]
        candle_rng = curr_h - curr_l

        if candle_rng <= 0:
            return True, "Świeca HA bez zakresu"

        if is_buy:
            is_bullish = curr_c > curr_o
            bottom_wick = curr_o - curr_l
            flat_bottom = (bottom_wick / candle_rng) <= 0.15
            if is_bullish and flat_bottom:
                return True, "Zdrowa świeca HA (byczy korpus, płaski dołek)"
            elif is_bullish:
                return True, "Bycza świeca HA z umiarkowanym dolnym knotem"
            else:
                return False, "Świeca HA niezdecydowana/niedźwiedzia (brak byczego impetu)"
        else:
            is_bearish = curr_c < curr_o
            top_wick = curr_h - curr_o
            flat_top = (top_wick / candle_rng) <= 0.15
            if is_bearish and flat_top:
                return True, "Zdrowa świeca HA (niedźwiedzi korpus, płaski szczyt)"
            elif is_bearish:
                return True, "Niedźwiedzia świeca HA z umiarkowanym górnym knotem"
            else:
                return False, "Świeca HA niezdecydowana/bycza (brak niedźwiedziego impetu)"

    def scan_for_setups(
        self,
        symbol: str,
        df_m15: pd.DataFrame,
        df_h1: Optional[pd.DataFrame] = None,
        df_h4: Optional[pd.DataFrame] = None,
        spread: float = 0.0,
    ) -> List[TradeSetup]:
        setups: List[TradeSetup] = []
        if df_m15 is None or len(df_m15) < 240:
            return setups

        digits = 2 if "JPY" in symbol.upper() or "XAU" in symbol.upper() or "GOLD" in symbol.upper() else 4
        point_scale = get_symbol_pip_size(symbol, digits)
        atr = (df_m15["high"] - df_m15["low"]).tail(14).mean()
        buffer = max(atr * 0.2, point_scale * 5)

        # 1. EMAs on M15
        e10 = df_m15["close"].ewm(span=self.fast_ema_len, adjust=False).mean()
        e21 = df_m15["close"].ewm(span=self.slow_ema_len, adjust=False).mean()
        e238 = df_m15["close"].ewm(span=self.trend_ema_len, adjust=False).mean()

        current_candle = df_m15.iloc[-1]
        prev_candle = df_m15.iloc[-2]
        c = current_candle["close"]
        h = current_candle["high"]
        l = current_candle["low"]
        current_time = current_candle["time"]

        curr_e10, prev_e10 = e10.iloc[-1], e10.iloc[-2]
        curr_e21, prev_e21 = e21.iloc[-1], e21.iloc[-2]
        curr_e238 = e238.iloc[-1]

        # 2. Multi-Timeframe Alignment
        mtf = self.calculate_mtf_trend(df_m15, df_h1, df_h4)
        m15_dir = mtf.get("M15", "NEUTRAL")
        h1_dir = mtf.get("H1", "NEUTRAL")
        h4_dir = mtf.get("H4", "NEUTRAL")

        # 3. Market Structure Swings (HH, HL, LH, LL)
        last_high_type, last_high_val, last_low_type, last_low_val = self.detect_swings(df_m15, self.swing_len)

        # 4. Institutional ZuMiKo Indicators (Webinar & PDF)
        vwap = self.calculate_vwap(df_m15)
        poc = self.calculate_poc(df_m15, lookback=40, bins=30)
        rsi = self.calculate_rsi(df_m15, period=14)
        is_ribbon_expanding, _ = self.calculate_ribbon_expansion(e10, e21, atr)

        # 5. News & Session
        session_info = SessionManager.get_current_session(current_time)
        if BLOCK_ROLLOVER_SESSION and ("Rollover" in session_info.get("session_name", "") or session_info.get("is_rollover", False)):
            logger.info(f"🛡️ [FILTER] Pomijam {symbol}: Aktywna sesja {session_info.get('session_name')} (BLOCK_ROLLOVER_SESSION=True).")
            return setups
        if BLOCK_EVENING_ENTRIES and session_info.get("is_evening_blocked", False):
            logger.info(f"🛡️ [FILTER] Pomijam {symbol}: Blokada wejść wieczornych ({session_info.get('session_name')}, {session_info.get('time_utc')}).")
            return setups
        has_news, news_warn, _ = self.news_filter.check_news(symbol)

        # Target RR
        target_rr, is_optimal_rr, rr_source = self.get_effective_rr()

        for direction in ("BUY", "SELL"):
            setup = self._evaluate_zumiko_direction(
                direction=direction,
                symbol=symbol,
                df_m15=df_m15,
                c=c,
                h=h,
                l=l,
                curr_e10=curr_e10,
                curr_e21=curr_e21,
                curr_e238=curr_e238,
                prev_e10=prev_e10,
                prev_e21=prev_e21,
                e10=e10,
                e21=e21,
                last_high_val=last_high_val,
                last_high_type=last_high_type,
                last_low_val=last_low_val,
                last_low_type=last_low_type,
                h1_dir=h1_dir,
                h4_dir=h4_dir,
                mtf=mtf,
                target_rr=target_rr,
                is_optimal_rr=is_optimal_rr,
                rr_source=rr_source,
                session_info=session_info,
                news_warn=news_warn,
                current_time=current_time,
                digits=digits,
                point_scale=point_scale,
                buffer=buffer,
                spread=spread,
                vwap=vwap,
                poc=poc,
                rsi=rsi,
                is_ribbon_expanding=is_ribbon_expanding,
            )
            if setup:
                setups.append(setup)

        return setups

    def _evaluate_zumiko_direction(
        self,
        direction: str,
        symbol: str,
        df_m15: pd.DataFrame,
        c: float,
        h: float,
        l: float,
        curr_e10: float,
        curr_e21: float,
        curr_e238: float,
        prev_e10: float,
        prev_e21: float,
        e10: pd.Series,
        e21: pd.Series,
        last_high_val: float,
        last_high_type: str,
        last_low_val: float,
        last_low_type: str,
        h1_dir: str,
        h4_dir: str,
        mtf: dict,
        target_rr: float,
        is_optimal_rr: bool,
        rr_source: str,
        session_info: dict,
        news_warn: Optional[str],
        current_time: datetime,
        digits: int,
        point_scale: float,
        buffer: float,
        spread: float = 0.0,
        vwap: float = 0.0,
        poc: float = 0.0,
        rsi: float = 50.0,
        is_ribbon_expanding: bool = False,
    ) -> Optional[TradeSetup]:
        is_buy = direction == "BUY"

        # 1. Baseline
        baseline_ok = (c > curr_e238) if is_buy else (c < curr_e238)
        # 2. Momentum
        momentum_ok = (curr_e10 > curr_e21) if is_buy else (curr_e10 < curr_e21)
        # 3. Trigger
        fast_l = self.fast_ema_len
        slow_l = self.slow_ema_len
        if is_buy:
            is_fresh_cross = (curr_e10 > curr_e21) and (e10.iloc[-3] <= e21.iloc[-3] or prev_e10 <= prev_e21)
            is_pullback = (l <= curr_e21 + buffer) and (c >= curr_e10 - buffer)
            trigger_name = f"Złoty Krzyż EMA {fast_l}/{slow_l}" if is_fresh_cross else f"Odbicie od strefy EMA {fast_l}/{slow_l}"
        else:
            is_fresh_cross = (curr_e10 < curr_e21) and (e10.iloc[-3] >= e21.iloc[-3] or prev_e10 >= prev_e21)
            is_pullback = (h >= curr_e21 - buffer) and (c <= curr_e10 + buffer)
            trigger_name = f"Krzyż Śmierci EMA {fast_l}/{slow_l}" if is_fresh_cross else f"Odrzucenie od strefy EMA {fast_l}/{slow_l}"
        trigger_ok = is_fresh_cross or is_pullback

        # 4. H1 check
        h1_ok = (not self.require_h1_alignment) or (h1_dir == direction)

        if not (baseline_ok and momentum_ok and trigger_ok and h1_ok):
            return None

        # 5. ZuMiKo Institutional Filters (Webinar & PDF)
        # VWAP Institutional Anchor Filter
        if self.require_vwap and vwap > 0.0:
            vwap_aligned = (c >= vwap - buffer) if is_buy else (c <= vwap + buffer)
            if not vwap_aligned:
                logger.info(
                    f"🛡️ [FILTER] Odrzucono setup Zumiko {symbol} {direction}: "
                    f"cena ({c:.{digits}f}) niezgodna z VWAP ({vwap:.{digits}f})."
                )
                return None

        # RSI Overbought/Oversold Filter
        if is_buy and rsi > self.rsi_max_buy:
            logger.info(
                f"🛡️ [FILTER] Odrzucono setup Zumiko {symbol} {direction}: "
                f"RSI ({rsi:.1f}) wykupiony (> {self.rsi_max_buy})."
            )
            return None
        if not is_buy and rsi < self.rsi_min_sell:
            logger.info(
                f"🛡️ [FILTER] Odrzucono setup Zumiko {symbol} {direction}: "
                f"RSI ({rsi:.1f}) wyprzedany (< {self.rsi_min_sell})."
            )
            return None

        # Heikin-Ashi Momentum Filter
        ha_ok, ha_desc = self.check_heikin_ashi_momentum(df_m15, is_buy)
        if self.use_heikin_ashi and not ha_ok:
            logger.info(
                f"🛡️ [FILTER] Odrzucono setup Zumiko {symbol} {direction}: "
                f"Heikin-Ashi ({ha_desc})."
            )
            return None

        atr = (df_m15["high"] - df_m15["low"]).tail(14).mean()
        total_score = 0
        breakdown: List[ConfluenceItem] = []

        # Factor 1: Multi-Timeframe Alignment (30% max, or -15% penalty for opposing H4)
        h4_match = h4_dir == direction
        h4_neutral = h4_dir in ("NEUTRAL", None, "", "NONE")
        if h4_match and h1_dir == direction:
            w1 = 30
            mtf_desc = f"Pełna zgodność MTF: M15/H1/H4 zgodnie w {direction} (+30%)"
        elif h1_dir == direction and h4_neutral:
            w1 = 15
            mtf_desc = f"Zgodność M15/H1 w {direction}, H4 neutralny (+15%)"
        elif not h4_match and not h4_neutral:
            w1 = -15
            mtf_desc = f"H4 przeciwny ({h4_dir}) (-15% KARA)"
        else:
            w1 = 10
            mtf_desc = f"Częściowa zgodność MTF: H1={h1_dir}, H4={h4_dir} (+10%)"
        total_score += w1
        breakdown.append(ConfluenceItem(
            factor=f"Zgodność Multi-Timeframe ({direction})",
            weight=30,
            achieved=(w1 > 0),
            detail=mtf_desc
        ))

        # Factor 2: Trend Baseline EMA 238 & Overextension (20%)
        cmp_sym = ">" if is_buy else "<"
        dist_e238 = abs(c - curr_e238)
        is_overextended = (atr > 0 and dist_e238 > (3.0 * atr))
        if is_overextended:
            w2 = 10
            detail_e238 = f"Cena {c:.{digits}f} {cmp_sym} EMA 238 ({curr_e238:.{digits}f}), ale mocne rozciągnięcie >3xATR (+10%)"
        else:
            w2 = 20
            detail_e238 = f"Cena {c:.{digits}f} {cmp_sym} EMA 238 ({curr_e238:.{digits}f}) w optymalnym zasięgu (+20%)"
        total_score += w2
        breakdown.append(ConfluenceItem(
            factor=f"Trend EMA 238 ({direction})",
            weight=20,
            achieved=True,
            detail=detail_e238
        ))

        # Factor 3: Trigger Momentum EMA 10/21 (20%)
        if is_fresh_cross:
            w3 = 20
            trig_desc = f"Świeży {trigger_name} (początek impulsu) (+20%)"
        else:
            w3 = 12
            trig_desc = f"{trigger_name} (kontynuacja trendu) (+12%)"
        total_score += w3
        breakdown.append(ConfluenceItem(
            factor=f"Trigger Momentum EMA 10/21 ({direction})",
            weight=20,
            achieved=True,
            detail=trig_desc
        ))

        # Factor 4: Potwierdzenie Struktury Swing (HL / LH) (15%)
        has_struct = (last_low_type == "HL") if is_buy else (last_high_type == "LH")
        struct_label = "HL" if is_buy else "LH"
        struct_val = last_low_val if is_buy else last_high_val
        if has_struct:
            w4 = 15
            struct_desc = f"Potwierdzona struktura {struct_label} ({struct_val:.{digits}f}) (+15%)"
        else:
            w4 = 0
            struct_desc = f"Brak czystej struktury {struct_label} (0%)"
        total_score += w4
        breakdown.append(ConfluenceItem(
            factor=f"Potwierdzenie Struktury {struct_label} ({direction})",
            weight=15,
            achieved=has_struct,
            detail=struct_desc
        ))

        # Factor 5: Timing Sesyjny (Killzone) (15%)
        is_killzone = session_info.get("is_killzone", False)
        session_name = session_info.get("session_name", "Standard")
        if is_killzone:
            w5 = 15
            sess_desc = f"Aktywna sesja {session_name} (+15%)"
        elif any(k in session_name.lower() for k in ("rollover", "noc", "post-ny")):
            w5 = 0
            sess_desc = f"Sesja o niskiej płynności: {session_name} (0%)"
        else:
            w5 = 5
            sess_desc = f"Standardowa sesja dzienna: {session_name} (+5%)"
        total_score += w5
        breakdown.append(ConfluenceItem(
            factor="Timing Sesyjny (Killzone)",
            weight=15,
            achieved=is_killzone,
            detail=sess_desc
        ))

        # ZuMiKo Institutional Confluence Breakdown Items
        if vwap > 0.0:
            vwap_rel = "powyżej" if (c >= vwap) else "poniżej"
            breakdown.append(ConfluenceItem(
                factor="Kotwica VWAP (ZuMiKo)",
                weight=0,
                achieved=True,
                detail=f"Cena {vwap_rel} dziennego VWAP ({vwap:.{digits}f})"
            ))

        ribbon_desc = (
            f"Wstęga EMA {self.fast_ema_len}/{self.slow_ema_len} rozszerza się (silny impet)"
            if is_ribbon_expanding
            else f"Wstęga EMA {self.fast_ema_len}/{self.slow_ema_len} zwęża się (możliwa konsolidacja)"
        )
        breakdown.append(ConfluenceItem(
            factor=f"Wstęga EMA {self.fast_ema_len}/{self.slow_ema_len}",
            weight=0,
            achieved=is_ribbon_expanding,
            detail=ribbon_desc
        ))

        breakdown.append(ConfluenceItem(
            factor="Filtr RSI (14)",
            weight=0,
            achieved=True,
            detail=f"RSI={rsi:.1f} (w bezpiecznym zakresie momentum)"
        ))

        breakdown.append(ConfluenceItem(
            factor="Heikin-Ashi (ZuMiKo)",
            weight=0,
            achieved=ha_ok,
            detail=ha_desc
        ))

        total_score = max(0, min(100, total_score))

        if total_score < self.min_confluence_score:
            return None

        # Rejection Candle Filter: ensure price is reacting rather than plunging through
        if REQUIRE_REJECTION_CANDLE:
            rejection_ok, reject_reason = check_rejection_candle(df_m15, is_buy=is_buy)
            if not rejection_ok:
                logger.info(f"🛡️ [FILTER] Odrzucono setup Zumiko {symbol} {direction}: {reject_reason}")
                return None

        spread_buffer = spread if ADD_SPREAD_TO_SL else 0.0
        effective_buffer = buffer + spread_buffer

        entry = c
        actual_rr = target_rr

        if is_buy:
            recent_low = df_m15.iloc[-5:]["low"].min()
            sl_ref = min(recent_low, curr_e21) if TIGHT_SL_MODE else (min(last_low_val, curr_e238) if last_low_val > 0 else (curr_e238 - buffer))
            structural_sl = sl_ref - effective_buffer
            sl, risk, applied_sl_mode = calculate_stop_loss(
                entry=entry,
                structural_sl=structural_sl,
                atr=atr,
                is_buy=True,
                spread_buffer=spread_buffer,
                point_scale=point_scale,
                symbol=symbol,
            )
            if sl is None or risk is None:
                return None
            tp1 = entry + (risk * target_rr)
            tp2 = max(last_high_val, entry + (risk * (target_rr + 1.0))) if last_high_val > 0 else (entry + (risk * (target_rr + 1.0)))

            # POC Target Capping (Webinar & PDF: POC as volume reaction magnet)
            if self.enable_poc and poc > 0.0 and risk > 0:
                if poc > entry and poc < tp1:
                    poc_rr = (poc - entry) / risk
                    if poc_rr >= 1.5:
                        logger.info(
                            f"🎯 [POC CAPPING] {symbol} BUY: Skrócono TP1 z {tp1:.{digits}f} do POC {poc:.{digits}f} "
                            f"({poc_rr:.1f}R zamiast {target_rr:.1f}R - magnes wolumenowy ZuMiKo)."
                        )
                        tp1 = poc
                        actual_rr = round(poc_rr, 1)

            zone_top = curr_e10
            zone_bottom = curr_e21
            swing_pat = f"HL ({last_low_val:.{digits}f})"
        else:
            recent_high = df_m15.iloc[-5:]["high"].max()
            sl_ref = max(recent_high, curr_e21) if TIGHT_SL_MODE else (max(last_high_val, curr_e238) if last_high_val > 0 else (curr_e238 + buffer))
            structural_sl = sl_ref + effective_buffer
            sl, risk, applied_sl_mode = calculate_stop_loss(
                entry=entry,
                structural_sl=structural_sl,
                atr=atr,
                is_buy=False,
                spread_buffer=spread_buffer,
                point_scale=point_scale,
                symbol=symbol,
            )
            if sl is None or risk is None:
                return None
            tp1 = entry - (risk * target_rr)
            tp2 = min(last_low_val, entry - (risk * (target_rr + 1.0))) if last_low_val > 0 else (entry - (risk * (target_rr + 1.0)))

            # POC Target Capping (Webinar & PDF: POC as volume reaction magnet)
            if self.enable_poc and poc > 0.0 and risk > 0:
                if poc < entry and poc > tp1:
                    poc_rr = (entry - poc) / risk
                    if poc_rr >= 1.5:
                        logger.info(
                            f"🎯 [POC CAPPING] {symbol} SELL: Skrócono TP1 z {tp1:.{digits}f} do POC {poc:.{digits}f} "
                            f"({poc_rr:.1f}R zamiast {target_rr:.1f}R - magnes wolumenowy ZuMiKo)."
                        )
                        tp1 = poc
                        actual_rr = round(poc_rr, 1)

            zone_top = curr_e21
            zone_bottom = curr_e10
            swing_pat = f"LH ({last_high_val:.{digits}f})"

        grade = "A+" if total_score >= 85 else ("A" if total_score >= 70 else "B")

        is_main_signal = (
            (h1_dir == direction)
            and is_ribbon_expanding
            and ((c >= vwap) if (vwap > 0.0 and is_buy) else ((c <= vwap) if (vwap > 0.0 and not is_buy) else True))
        )
        setup_type = (
            "Główny Sygnał ZuMiKo KillMLM (VWAP + Ribbon + MTF)"
            if is_main_signal
            else "Sygnał Korekcyjny ZuMiKo (EMA Pullback)"
        )

        return TradeSetup(
            symbol=symbol,
            direction=direction,
            timeframe=LTF_TIMEFRAME,
            setup_type=setup_type,
            entry_price=round(entry, digits),
            sl_price=round(sl, digits),
            tp1_price=round(tp1, digits),
            tp2_price=round(tp2, digits),
            rr_ratio=round(actual_rr, 1),
            htf_trend=h4_dir,
            zone_top=round(zone_top, digits),
            zone_bottom=round(zone_bottom, digits),
            reason=f"Zumiko KillMLM {total_score}% Score [{grade}]. MTF: M15/H1/H4 zgodnie w {direction}.",
            timestamp=current_time,
            impulse_high=round(last_high_val, digits),
            impulse_low=round(last_low_val, digits),
            confluence_score=total_score,
            grade=grade,
            confluence_breakdown=breakdown,
            session_name=session_info["session_name"],
            is_killzone=session_info["is_killzone"],
            news_warning=news_warn,
            is_optimal_rr=is_optimal_rr,
            rr_source_info=rr_source,
            strategy_name="ZUMIKO",
            mtf_table=mtf,
            ema_fast=round(curr_e10, digits),
            ema_slow=round(curr_e21, digits),
            ema_238=round(curr_e238, digits),
            swing_pattern=swing_pat,
            spread_used=spread_buffer,
            sl_mode=applied_sl_mode,
            vwap=round(vwap, digits) if vwap > 0.0 else 0.0,
            poc=round(poc, digits) if poc > 0.0 else 0.0,
            rsi=round(rsi, 1),
            is_ribbon_expanding=is_ribbon_expanding,
            is_healthy_ha=ha_ok,
            is_main_zumiko_signal=is_main_signal,
        )
