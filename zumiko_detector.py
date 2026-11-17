import logging
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from news_filter import NewsFilter
from rr_optimizer import RROptimizer
from session_manager import SessionManager
from smc_detector import ConfluenceItem, TradeSetup
from trade_journal import TradeJournal
from config import DEFAULT_RR_RATIO, MIN_TRADES_FOR_OPTIMAL_RR, USE_OPTIMAL_RR

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
    ):
        self.fast_ema_len = fast_ema_len
        self.slow_ema_len = slow_ema_len
        self.trend_ema_len = trend_ema_len
        self.swing_len = swing_len
        self.default_rr = default_rr
        self.min_confluence_score = min_confluence_score
        self.use_optimal_rr = use_optimal_rr
        self.min_trades_for_optimal = min_trades_for_optimal
        self.require_h1_alignment = require_h1_alignment

        self.news_filter = NewsFilter()
        self.journal = TradeJournal()

    def get_effective_rr(self) -> Tuple[float, bool, str]:
        """Determines target RR dynamically from TradeJournal / RROptimizer or .env."""
        if self.use_optimal_rr:
            trades = self.journal.get_all_trades()
            if len(trades) >= self.min_trades_for_optimal:
                opt_res = RROptimizer.calculate_best_rr(trades)
                if opt_res and opt_res.best_rr >= 1.0:
                    return (
                        opt_res.best_rr,
                        True,
                        f"Zoptymalizowany Best RR (1:{opt_res.best_rr:.1f} z {len(trades)} zagrań)",
                    )
        return (
            self.default_rr,
            False,
            f"Domyślny RR z .env (1:{self.default_rr:.1f})",
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

        # ==========================================================
        # SCENARIO A: BUY (DŁUGA)
        # ==========================================================
        # 1. Trend Baseline: Cena powyżej EMA 238
        is_above_238 = c > curr_e238
        # 2. Momentum Trigger: EMA 10 > EMA 21
        is_ema_bullish = curr_e10 > curr_e21
        # Crossover w ostatnich 3 świecach lub odbicie od strefy EMA 10/21
        is_fresh_cross = (curr_e10 > curr_e21) and (e10.iloc[-3] <= e21.iloc[-3] or prev_e10 <= prev_e21)
        is_pullback_bounce = (l <= curr_e21 + buffer) and (c >= curr_e10 - buffer)
        buy_trigger = is_fresh_cross or is_pullback_bounce

        # 3. MTF Check: H1 musi zgadzać się z BUY (jeśli włączona weryfikacja)
        h1_ok = (not self.require_h1_alignment) or (h1_dir == "BUY")

        if is_above_238 and is_ema_bullish and buy_trigger and h1_ok:
            total_score = 0
            breakdown: List[ConfluenceItem] = []

            # Factor 1: Trend Baseline EMA 238 (BUY)
            w1 = 25
            total_score += w1
            breakdown.append(ConfluenceItem(
                factor="Trend EMA 238 (BUY)",
                weight=25,
                achieved=True,
                detail=f"Cena {c:.{digits}f} > EMA 238 ({curr_e238:.{digits}f}) (+{w1}%)"
            ))

            # Factor 2: Przebicie / Odbicie EMA 10/21 (BUY)
            w2 = 25
            total_score += w2
            trigger_name = "Złoty Krzyż EMA 10/21" if is_fresh_cross else "Odbicie od strefy EMA 10/21"
            breakdown.append(ConfluenceItem(
                factor="Przebicie / Odbicie EMA 10/21 (BUY)",
                weight=25,
                achieved=True,
                detail=f"{trigger_name} (EMA10: {curr_e10:.{digits}f} > EMA21: {curr_e21:.{digits}f}) (+{w2}%)"
            ))

            # Factor 3: Potwierdzenie Struktury Rynku (BUY)
            has_hl = (last_low_type == "HL")
            w3 = 25 if has_hl else 15
            total_score += w3
            struct_desc = f"Wyższy Dołek HL ({last_low_val:.{digits}f})" if has_hl else f"Ostatni dołek ({last_low_val:.{digits}f})"
            breakdown.append(ConfluenceItem(
                factor="Potwierdzenie Struktury HL (BUY)",
                weight=25,
                achieved=has_hl,
                detail=f"{struct_desc} (+{w3}%)"
            ))

            # Factor 4: Zgodność Multi-Timeframe (BUY)
            mtf_full = (h1_dir == "BUY" and h4_dir == "BUY")
            w4 = 25 if mtf_full else 15
            total_score += w4
            mtf_desc = f"M15: (BUY) | H1: ({h1_dir}) | H4: ({h4_dir})"
            breakdown.append(ConfluenceItem(
                factor="Zgodność Multi-Timeframe (BUY)",
                weight=25,
                achieved=mtf_full,
                detail=f"{mtf_desc} (+{w4}%)"
            ))

            if total_score >= self.min_confluence_score:
                entry = c
                # Stop loss poniżej ostatniego dołka lub EMA 238
                sl_ref = min(last_low_val, curr_e238) if last_low_val > 0 else (curr_e238 - buffer)
                sl = sl_ref - buffer
                risk = entry - sl

                if risk > (point_scale * 5):
                    tp1 = entry + (risk * target_rr)
                    tp2 = max(last_high_val, entry + (risk * (target_rr + 1.0)))
                    grade = "A+" if total_score >= 90 else ("A" if total_score >= 75 else "B")

                    setups.append(TradeSetup(
                        symbol=symbol,
                        direction="BUY",
                        timeframe="M15",
                        setup_type="Zumiko KillMLM MTF Trend & EMA Pullback",
                        entry_price=round(entry, digits),
                        sl_price=round(sl, digits),
                        tp1_price=round(tp1, digits),
                        tp2_price=round(tp2, digits),
                        rr_ratio=round(target_rr, 1),
                        htf_trend=h4_dir,
                        zone_top=round(curr_e10, digits),
                        zone_bottom=round(curr_e21, digits),
                        reason=f"Zumiko KillMLM {total_score}% Score [{grade}]. MTF: M15/H1/H4 zgodnie w górę.",
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
                        swing_pattern=f"HL ({last_low_val:.{digits}f})",
                    ))

        # ==========================================================
        # SCENARIO B: SELL (KRÓTKA)
        # ==========================================================
        # 1. Trend Baseline: Cena poniżej EMA 238
        is_below_238 = c < curr_e238
        # 2. Momentum Trigger: EMA 10 < EMA 21
        is_ema_bearish = curr_e10 < curr_e21
        is_fresh_cross_sell = (curr_e10 < curr_e21) and (e10.iloc[-3] >= e21.iloc[-3] or prev_e10 >= prev_e21)
        is_pullback_rejection = (h >= curr_e21 - buffer) and (c <= curr_e10 + buffer)
        sell_trigger = is_fresh_cross_sell or is_pullback_rejection

        # 3. MTF Check: H1 musi zgadzać się z SELL
        h1_ok_sell = (not self.require_h1_alignment) or (h1_dir == "SELL")

        if is_below_238 and is_ema_bearish and sell_trigger and h1_ok_sell:
            total_score = 0
            breakdown: List[ConfluenceItem] = []

            # Factor 1: Trend Baseline EMA 238 (SELL)
            w1 = 25
            total_score += w1
            breakdown.append(ConfluenceItem(
                factor="Trend EMA 238 (SELL)",
                weight=25,
                achieved=True,
                detail=f"Cena {c:.{digits}f} < EMA 238 ({curr_e238:.{digits}f}) (+{w1}%)"
            ))

            # Factor 2: Przebicie / Odrzucenie EMA 10/21 (SELL)
            w2 = 25
            total_score += w2
            trigger_name = "Krzyż Śmierci EMA 10/21" if is_fresh_cross_sell else "Odrzucenie od strefy EMA 10/21"
            breakdown.append(ConfluenceItem(
                factor="Przebicie / Odrzucenie EMA 10/21 (SELL)",
                weight=25,
                achieved=True,
                detail=f"{trigger_name} (EMA10: {curr_e10:.{digits}f} < EMA21: {curr_e21:.{digits}f}) (+{w2}%)"
            ))

            # Factor 3: Potwierdzenie Struktury Rynku (SELL)
            has_lh = (last_high_type == "LH")
            w3 = 25 if has_lh else 15
            total_score += w3
            struct_desc = f"Niższy Szczyt LH ({last_high_val:.{digits}f})" if has_lh else f"Ostatni szczyt ({last_high_val:.{digits}f})"
            breakdown.append(ConfluenceItem(
                factor="Potwierdzenie Struktury LH (SELL)",
                weight=25,
                achieved=has_lh,
                detail=f"{struct_desc} (+{w3}%)"
            ))

            # Factor 4: Zgodność Multi-Timeframe (SELL)
            mtf_full = (h1_dir == "SELL" and h4_dir == "SELL")
            w4 = 25 if mtf_full else 15
            total_score += w4
            mtf_desc = f"M15: (SELL) | H1: ({h1_dir}) | H4: ({h4_dir})"
            breakdown.append(ConfluenceItem(
                factor="Zgodność Multi-Timeframe (SELL)",
                weight=25,
                achieved=mtf_full,
                detail=f"{mtf_desc} (+{w4}%)"
            ))

            if total_score >= self.min_confluence_score:
                entry = c
                # Stop loss powyżej ostatniego szczytu lub EMA 238
                sl_ref = max(last_high_val, curr_e238) if last_high_val > 0 else (curr_e238 + buffer)
                sl = sl_ref + buffer
                risk = sl - entry

                if risk > (point_scale * 5):
                    tp1 = entry - (risk * target_rr)
                    tp2 = min(last_low_val, entry - (risk * (target_rr + 1.0)))
                    grade = "A+" if total_score >= 90 else ("A" if total_score >= 75 else "B")

                    setups.append(TradeSetup(
                        symbol=symbol,
                        direction="SELL",
                        timeframe="M15",
                        setup_type="Zumiko KillMLM MTF Trend & EMA Pullback",
                        entry_price=round(entry, digits),
                        sl_price=round(sl, digits),
                        tp1_price=round(tp1, digits),
                        tp2_price=round(tp2, digits),
                        rr_ratio=round(target_rr, 1),
                        htf_trend=h4_dir,
                        zone_top=round(curr_e21, digits),
                        zone_bottom=round(curr_e10, digits),
                        reason=f"Zumiko KillMLM {total_score}% Score [{grade}]. MTF: M15/H1/H4 zgodnie w dół.",
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
                        swing_pattern=f"LH ({last_high_val:.{digits}f})",
                    ))

        return setups
