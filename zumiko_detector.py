from datetime import datetime
import logging
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from news_filter import NewsFilter
from rr_optimizer import RROptimizer
from session_manager import SessionManager
from smc_detector import ConfluenceItem, TradeSetup
from trade_journal import TradeJournal
from config import (
    ADD_SPREAD_TO_SL,
    DEFAULT_RR_RATIO,
    MIN_TRADES_FOR_OPTIMAL_RR,
    SL_ATR_MULTIPLIER,
    SL_CALCULATION_MODE,
    SL_HYBRID_MIN_ATR_MULT,
    TIGHT_SL_MODE,
    USE_OPTIMAL_RR,
)

logger = logging.getLogger("ForexSMC.ZumikoDetector")


class ZumikoDetector:
    """
    Detector for the Zumiko KillMLM Institutional Strategy:
    1. Trend Baseline: EMA 238 (Market Filter)
    2. Momentum Trigger: EMA Fast (10) vs EMA Slow (21) Crossover / Pullback Bounce
    3. Market Structure Swings: Higher Low (HL) for BUY / Lower High (LH) for SELL (swingLen=10)
    4. Multi-Timeframe Trend Matrix: Validates trend across M15, H1, and H4
    """

    def __init__(
        self,
        fast_ema_len: int = 10,
        slow_ema_len: int = 21,
        trend_ema_len: int = 238,
        swing_len: int = 10,
        default_rr: float = DEFAULT_RR_RATIO,
        min_confluence_score: int = 70,
        use_optimal_rr: bool = USE_OPTIMAL_RR,
        min_trades_for_optimal: int = MIN_TRADES_FOR_OPTIMAL_RR,
        require_h1_alignment: bool = True,
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
        point_scale = 0.01 if digits == 2 else 0.0001
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

        # 4. News & Session
        session_info = SessionManager.get_current_session(current_time)
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
    ) -> Optional[TradeSetup]:
        is_buy = direction == "BUY"

        # 1. Baseline
        baseline_ok = (c > curr_e238) if is_buy else (c < curr_e238)
        # 2. Momentum
        momentum_ok = (curr_e10 > curr_e21) if is_buy else (curr_e10 < curr_e21)
        # 3. Trigger
        if is_buy:
            is_fresh_cross = (curr_e10 > curr_e21) and (e10.iloc[-3] <= e21.iloc[-3] or prev_e10 <= prev_e21)
            is_pullback = (l <= curr_e21 + buffer) and (c >= curr_e10 - buffer)
            trigger_name = "Złoty Krzyż EMA 10/21" if is_fresh_cross else "Odbicie od strefy EMA 10/21"
        else:
            is_fresh_cross = (curr_e10 < curr_e21) and (e10.iloc[-3] >= e21.iloc[-3] or prev_e10 >= prev_e21)
            is_pullback = (h >= curr_e21 - buffer) and (c <= curr_e10 + buffer)
            trigger_name = "Krzyż Śmierci EMA 10/21" if is_fresh_cross else "Odrzucenie od strefy EMA 10/21"
        trigger_ok = is_fresh_cross or is_pullback

        # 4. H1 check
        h1_ok = (not self.require_h1_alignment) or (h1_dir == direction)

        if not (baseline_ok and momentum_ok and trigger_ok and h1_ok):
            return None

        total_score = 0
        breakdown: List[ConfluenceItem] = []

        # Factor 1: Trend Baseline EMA 238
        cmp_sym = ">" if is_buy else "<"
        total_score += 25
        breakdown.append(ConfluenceItem(
            factor=f"Trend EMA 238 ({direction})",
            weight=25,
            achieved=True,
            detail=f"Cena {c:.{digits}f} {cmp_sym} EMA 238 ({curr_e238:.{digits}f}) (+25%)"
        ))

        # Factor 2: Przebicie / Odrzucenie EMA 10/21
        total_score += 25
        breakdown.append(ConfluenceItem(
            factor=f"Przebicie / Odrzucenie EMA 10/21 ({direction})",
            weight=25,
            achieved=True,
            detail=f"{trigger_name} (EMA10: {curr_e10:.{digits}f} {cmp_sym} EMA21: {curr_e21:.{digits}f}) (+25%)"
        ))

        # Factor 3: Market structure
        has_struct = (last_low_type == "HL") if is_buy else (last_high_type == "LH")
        w3 = 25 if has_struct else 15
        total_score += w3
        struct_label = "HL" if is_buy else "LH"
        struct_val = last_low_val if is_buy else last_high_val
        struct_desc = f"Struktura {struct_label} ({struct_val:.{digits}f})" if has_struct else f"Ostatni swing ({struct_val:.{digits}f})"
        breakdown.append(ConfluenceItem(
            factor=f"Potwierdzenie Struktury {struct_label} ({direction})",
            weight=25,
            achieved=has_struct,
            detail=f"{struct_desc} (+{w3}%)"
        ))

        # Factor 4: MTF Confluence
        mtf_full = (h1_dir == direction and h4_dir == direction)
        w4 = 25 if mtf_full else 15
        total_score += w4
        mtf_desc = f"M15: ({direction}) | H1: ({h1_dir}) | H4: ({h4_dir})"
        breakdown.append(ConfluenceItem(
            factor=f"Zgodność Multi-Timeframe ({direction})",
            weight=25,
            achieved=mtf_full,
            detail=f"{mtf_desc} (+{w4}%)"
        ))

        if total_score < self.min_confluence_score:
            return None

        spread_buffer = spread if ADD_SPREAD_TO_SL else 0.0
        effective_buffer = buffer + spread_buffer
        atr = (df_m15["high"] - df_m15["low"]).tail(14).mean()

        entry = c
        if is_buy:
            recent_low = df_m15.iloc[-5:]["low"].min()
            sl_ref = min(recent_low, curr_e21) if TIGHT_SL_MODE else (min(last_low_val, curr_e238) if last_low_val > 0 else (curr_e238 - buffer))
            structural_sl = sl_ref - effective_buffer
            structural_risk = entry - structural_sl
            atr_risk = atr * SL_ATR_MULTIPLIER

            if SL_CALCULATION_MODE == "ATR":
                sl = entry - (atr_risk + spread_buffer)
                applied_sl_mode = "ATR"
            elif SL_CALCULATION_MODE == "HYBRID":
                min_risk = atr * SL_HYBRID_MIN_ATR_MULT
                final_risk = max(structural_risk, min_risk)
                sl = entry - final_risk
                applied_sl_mode = "HYBRID"
            else:
                sl = structural_sl
                applied_sl_mode = "STRUCTURE"

            risk = entry - sl
            if risk <= (point_scale * 5):
                return None
            tp1 = entry + (risk * target_rr)
            tp2 = max(last_high_val, entry + (risk * (target_rr + 1.0)))
            zone_top = curr_e10
            zone_bottom = curr_e21
            swing_pat = f"HL ({last_low_val:.{digits}f})"
        else:
            recent_high = df_m15.iloc[-5:]["high"].max()
            sl_ref = max(recent_high, curr_e21) if TIGHT_SL_MODE else (max(last_high_val, curr_e238) if last_high_val > 0 else (curr_e238 + buffer))
            structural_sl = sl_ref + effective_buffer
            structural_risk = structural_sl - entry
            atr_risk = atr * SL_ATR_MULTIPLIER

            if SL_CALCULATION_MODE == "ATR":
                sl = entry + (atr_risk + spread_buffer)
                applied_sl_mode = "ATR"
            elif SL_CALCULATION_MODE == "HYBRID":
                min_risk = atr * SL_HYBRID_MIN_ATR_MULT
                final_risk = max(structural_risk, min_risk)
                sl = entry + final_risk
                applied_sl_mode = "HYBRID"
            else:
                sl = structural_sl
                applied_sl_mode = "STRUCTURE"

            risk = sl - entry
            if risk <= (point_scale * 5):
                return None
            tp1 = entry - (risk * target_rr)
            tp2 = min(last_low_val, entry - (risk * (target_rr + 1.0)))
            zone_top = curr_e21
            zone_bottom = curr_e10
            swing_pat = f"LH ({last_high_val:.{digits}f})"

        grade = "A+" if total_score >= 90 else ("A" if total_score >= 75 else "B")

        return TradeSetup(
            symbol=symbol,
            direction=direction,
            timeframe="M15",
            setup_type="Zumiko KillMLM MTF Trend & EMA Pullback",
            entry_price=round(entry, digits),
            sl_price=round(sl, digits),
            tp1_price=round(tp1, digits),
            tp2_price=round(tp2, digits),
            rr_ratio=round(target_rr, 1),
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
        )
