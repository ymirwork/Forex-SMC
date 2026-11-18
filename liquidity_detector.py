from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import logging
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from config import (
    BROKER_UTC_OFFSET_HOURS,
    ENABLE_ASIAN_SWEEP,
    ENABLE_EQH_EQL_SWEEP,
    ENABLE_LIQUIDITY_DETECTOR,
    ENABLE_PDH_PDL_SWEEP,
    ENABLE_PWH_PWL_SWEEP,
    EQH_EQL_ATR_MULT,
    EQH_EQL_MIN_BARS_DISTANCE,
    LIQUIDITY_SWEEP_LOOKBACK_BARS,
)
from session_manager import SessionManager

logger = logging.getLogger("ForexSMC.LiquidityDetector")


@dataclass
class SweptLevel:
    name: str                   # e.g. "PDL", "Asian Low", "EQL", "PWL"
    price: float
    level_type: str             # "DAILY", "WEEKLY", "SESSION", "EQUAL_HIGHS_LOWS"
    bars_ago: int
    description: str


@dataclass
class LiquidityEvaluation:
    has_sweep: bool = False
    swept_levels: List[SweptLevel] = field(default_factory=list)
    is_multi_sweep: bool = False
    has_htf_sweep: bool = False
    confluence_weight: int = 0
    bonus_weight: int = 0
    summary_msg: str = "Brak sweepu płynności"
    all_levels: Dict[str, float] = field(default_factory=dict)


class LiquidityDetector:
    """
    Advanced Institutional Liquidity Engine:
    1. Previous Day High & Low (PDH / PDL)
    2. Previous Week High & Low (PWH / PWL)
    3. Session Liquidity (Asian Range High / Low)
    4. Equal Highs & Equal Lows (EQH / EQL) with dynamic ATR tolerance
    5. Sweep Validation: Wick rejection or rapid return within N bars
    """

    def __init__(
        self,
        enabled: bool = ENABLE_LIQUIDITY_DETECTOR,
        enable_pdh_pdl: bool = ENABLE_PDH_PDL_SWEEP,
        enable_pwh_pwl: bool = ENABLE_PWH_PWL_SWEEP,
        enable_eqh_eql: bool = ENABLE_EQH_EQL_SWEEP,
        enable_asian: bool = ENABLE_ASIAN_SWEEP,
        lookback_bars: int = LIQUIDITY_SWEEP_LOOKBACK_BARS,
        eqh_atr_mult: float = EQH_EQL_ATR_MULT,
        eqh_min_bars_distance: int = EQH_EQL_MIN_BARS_DISTANCE,
    ):
        self.enabled = enabled
        self.enable_pdh_pdl = enable_pdh_pdl
        self.enable_pwh_pwl = enable_pwh_pwl
        self.enable_eqh_eql = enable_eqh_eql
        self.enable_asian = enable_asian
        self.lookback_bars = lookback_bars
        self.eqh_atr_mult = eqh_atr_mult
        self.eqh_min_bars_distance = eqh_min_bars_distance

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
        """Calculates 14-period Average True Range."""
        if df is None or len(df) < period + 1:
            return 0.0
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = np.maximum(high - low, np.maximum(abs(high - prev_close), abs(low - prev_close)))
        atr = tr.tail(period).mean()
        return float(atr) if pd.notna(atr) else 0.0

    @classmethod
    def calculate_pdh_pdl(
        cls,
        df_d1: Optional[pd.DataFrame] = None,
        df_fallback: Optional[pd.DataFrame] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Determines Previous Day High (PDH) and Previous Day Low (PDL).
        Uses closed D1 candle if available, otherwise falls back to H1/M15 daily grouping.
        """
        if df_d1 is not None and len(df_d1) >= 2:
            # iloc[-2] is the completed previous daily bar
            pdh = float(df_d1["high"].iloc[-2])
            pdl = float(df_d1["low"].iloc[-2])
            return pdh, pdl
        elif df_d1 is not None and len(df_d1) == 1:
            return float(df_d1["high"].iloc[0]), float(df_d1["low"].iloc[0])

        if df_fallback is not None and not df_fallback.empty and "time" in df_fallback.columns:
            df_copy = df_fallback.copy()
            df_copy["date"] = pd.to_datetime(df_copy["time"]).dt.date
            unique_dates = sorted(df_copy["date"].unique())
            if len(unique_dates) >= 2:
                prev_date = unique_dates[-2]
                prev_day_bars = df_copy[df_copy["date"] == prev_date]
                if not prev_day_bars.empty:
                    return float(prev_day_bars["high"].max()), float(prev_day_bars["low"].min())

        return None, None

    @classmethod
    def calculate_pwh_pwl(
        cls,
        df_w1: Optional[pd.DataFrame] = None,
        df_fallback: Optional[pd.DataFrame] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Determines Previous Week High (PWH) and Previous Week Low (PWL).
        Uses closed W1 candle if available, or fallback grouping.
        """
        if df_w1 is not None and len(df_w1) >= 2:
            pwh = float(df_w1["high"].iloc[-2])
            pwl = float(df_w1["low"].iloc[-2])
            return pwh, pwl
        elif df_w1 is not None and len(df_w1) == 1:
            return float(df_w1["high"].iloc[0]), float(df_w1["low"].iloc[0])

        if df_fallback is not None and not df_fallback.empty and "time" in df_fallback.columns:
            df_copy = df_fallback.copy()
            times = pd.to_datetime(df_copy["time"])
            # Week identifier: year-week (integer for reliable chronological sorting)
            df_copy["year_week"] = times.dt.isocalendar().year * 100 + times.dt.isocalendar().week
            unique_weeks = sorted(df_copy["year_week"].unique())
            if len(unique_weeks) >= 2:
                prev_week = unique_weeks[-2]
                prev_week_bars = df_copy[df_copy["year_week"] == prev_week]
                if not prev_week_bars.empty:
                    return float(prev_week_bars["high"].max()), float(prev_week_bars["low"].min())

        return None, None

    def detect_equal_highs_lows(
        self,
        df: pd.DataFrame,
        atr: float,
        swing_len: int = 5,
    ) -> Tuple[List[float], List[float]]:
        """
        Identifies Equal Highs (EQH) and Equal Lows (EQL) within recent bars.
        Two swing points qualify as equal if abs(p1 - p2) <= eqh_atr_mult * atr
        and are separated by at least eqh_min_bars_distance.
        """
        if df is None or len(df) < (swing_len * 2 + 10) or atr <= 0:
            return [], []

        tolerance = self.eqh_atr_mult * atr
        highs = df["high"].values
        lows = df["low"].values
        n = len(df)

        # Detect swing highs and swing lows
        swing_high_indices: List[int] = []
        swing_low_indices: List[int] = []

        for i in range(swing_len, n - swing_len):
            val_h = highs[i]
            if all(val_h >= highs[i - k] for k in range(1, swing_len + 1)) and \
               all(val_h >= highs[i + k] for k in range(1, swing_len + 1)):
                swing_high_indices.append(i)

            val_l = lows[i]
            if all(val_l <= lows[i - k] for k in range(1, swing_len + 1)) and \
               all(val_l <= lows[i + k] for k in range(1, swing_len + 1)):
                swing_low_indices.append(i)

        # Pair swing highs
        eqh_levels: List[float] = []
        for i in range(len(swing_high_indices)):
            for j in range(i + 1, len(swing_high_indices)):
                idx1, idx2 = swing_high_indices[i], swing_high_indices[j]
                if (idx2 - idx1) >= self.eqh_min_bars_distance:
                    p1, p2 = highs[idx1], highs[idx2]
                    if abs(p1 - p2) <= tolerance:
                        avg_level = (p1 + p2) / 2.0
                        # Avoid duplicates
                        if not any(abs(avg_level - existing) <= tolerance for existing in eqh_levels):
                            eqh_levels.append(round(avg_level, 5))

        # Pair swing lows
        eql_levels: List[float] = []
        for i in range(len(swing_low_indices)):
            for j in range(i + 1, len(swing_low_indices)):
                idx1, idx2 = swing_low_indices[i], swing_low_indices[j]
                if (idx2 - idx1) >= self.eqh_min_bars_distance:
                    p1, p2 = lows[idx1], lows[idx2]
                    if abs(p1 - p2) <= tolerance:
                        avg_level = (p1 + p2) / 2.0
                        if not any(abs(avg_level - existing) <= tolerance for existing in eql_levels):
                            eql_levels.append(round(avg_level, 5))

        return eqh_levels, eql_levels

    def evaluate_sweeps(
        self,
        df_ltf: pd.DataFrame,
        direction: str,
        df_d1: Optional[pd.DataFrame] = None,
        df_w1: Optional[pd.DataFrame] = None,
        df_h1: Optional[pd.DataFrame] = None,
    ) -> LiquidityEvaluation:
        """
        Comprehensive evaluation of institutional and retail liquidity sweeps:
        - Asian High / Low
        - PDH / PDL (Daily)
        - PWH / PWL (Weekly)
        - Equal Highs / Equal Lows (EQH / EQL)
        """
        if not self.enabled or df_ltf is None or df_ltf.empty:
            return LiquidityEvaluation()

        atr = self.calculate_atr(df_ltf, 14)
        current_price = float(df_ltf["close"].iloc[-1])
        lookback = min(self.lookback_bars, len(df_ltf))
        recent_bars = df_ltf.tail(lookback)

        all_levels: Dict[str, float] = {}
        swept_levels: List[SweptLevel] = []

        is_buy = direction.upper() == "BUY"

        # 1. Asian Session High / Low
        if self.enable_asian:
            asia_high, asia_low = SessionManager.calculate_asian_range(df_ltf)
            if asia_high is not None and asia_low is not None:
                all_levels["Asian_High"] = asia_high
                all_levels["Asian_Low"] = asia_low

                if is_buy:
                    # Looking for sweep of Asian Low
                    swept, bars_ago = self._check_level_sweep_low(recent_bars, asia_low, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="Asian Low",
                            price=asia_low,
                            level_type="SESSION",
                            bars_ago=bars_ago,
                            description=f"Asian Low Swept ({asia_low:.4f})",
                        ))
                else:
                    # Looking for sweep of Asian High
                    swept, bars_ago = self._check_level_sweep_high(recent_bars, asia_high, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="Asian High",
                            price=asia_high,
                            level_type="SESSION",
                            bars_ago=bars_ago,
                            description=f"Asian High Swept ({asia_high:.4f})",
                        ))

        # Determine fallback DataFrame for session / daily / weekly grouping
        fallback_df = df_h1 if (df_h1 is not None and not df_h1.empty) else df_ltf

        # 2. Previous Day High / Low (PDH / PDL)
        if self.enable_pdh_pdl:
            pdh, pdl = self.calculate_pdh_pdl(df_d1=df_d1, df_fallback=fallback_df)
            if pdh is not None and pdl is not None:
                all_levels["PDH"] = pdh
                all_levels["PDL"] = pdl

                if is_buy:
                    swept, bars_ago = self._check_level_sweep_low(recent_bars, pdl, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="PDL",
                            price=pdl,
                            level_type="DAILY",
                            bars_ago=bars_ago,
                            description=f"PDL Swept ({pdl:.4f})",
                        ))
                else:
                    swept, bars_ago = self._check_level_sweep_high(recent_bars, pdh, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="PDH",
                            price=pdh,
                            level_type="DAILY",
                            bars_ago=bars_ago,
                            description=f"PDH Swept ({pdh:.4f})",
                        ))

        # 3. Previous Week High / Low (PWH / PWL)
        if self.enable_pwh_pwl:
            pwh, pwl = self.calculate_pwh_pwl(df_w1=df_w1, df_fallback=fallback_df)
            if pwh is not None and pwl is not None:
                all_levels["PWH"] = pwh
                all_levels["PWL"] = pwl

                if is_buy:
                    swept, bars_ago = self._check_level_sweep_low(recent_bars, pwl, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="PWL",
                            price=pwl,
                            level_type="WEEKLY",
                            bars_ago=bars_ago,
                            description=f"PWL Swept ({pwl:.4f})",
                        ))
                else:
                    swept, bars_ago = self._check_level_sweep_high(recent_bars, pwh, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="PWH",
                            price=pwh,
                            level_type="WEEKLY",
                            bars_ago=bars_ago,
                            description=f"PWH Swept ({pwh:.4f})",
                        ))

        # 4. Equal Highs & Equal Lows (EQH / EQL)
        if self.enable_eqh_eql:
            eqh_list, eql_list = self.detect_equal_highs_lows(df_ltf, atr=atr)
            if is_buy:
                for idx, eql in enumerate(eql_list):
                    all_levels[f"EQL_{idx+1}"] = eql
                    swept, bars_ago = self._check_level_sweep_low(recent_bars, eql, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="EQL",
                            price=eql,
                            level_type="EQUAL_HIGHS_LOWS",
                            bars_ago=bars_ago,
                            description=f"EQL Swept ({eql:.4f})",
                        ))
            else:
                for idx, eqh in enumerate(eqh_list):
                    all_levels[f"EQH_{idx+1}"] = eqh
                    swept, bars_ago = self._check_level_sweep_high(recent_bars, eqh, current_price)
                    if swept:
                        swept_levels.append(SweptLevel(
                            name="EQH",
                            price=eqh,
                            level_type="EQUAL_HIGHS_LOWS",
                            bars_ago=bars_ago,
                            description=f"EQH Swept ({eqh:.4f})",
                        ))

        has_sweep = len(swept_levels) > 0
        unique_level_types = {s.level_type for s in swept_levels}
        unique_level_names = {s.name for s in swept_levels}
        is_multi_sweep = len(swept_levels) >= 2 and (len(unique_level_types) >= 2 or len(unique_level_names) >= 2)
        has_htf_sweep = any(s.level_type in ("DAILY", "WEEKLY", "EQUAL_HIGHS_LOWS") for s in swept_levels)

        # Scoring: Base 15% + 5% bonus for Multi-Sweep or HTF Sweep
        confluence_weight = 15 if has_sweep else 0
        bonus_weight = 5 if (is_multi_sweep or has_htf_sweep) and has_sweep else 0

        # Construct summary message
        if has_sweep:
            names = [s.name for s in swept_levels]
            unique_names = list(dict.fromkeys(names))
            summary_msg = f"{' + '.join(unique_names)} Swept (Wymiecenie Płynności)"
            if is_multi_sweep:
                summary_msg += " [MULTI-SWEEP]"
        else:
            summary_msg = "Brak potwierdzonego sweepu płynności"

        return LiquidityEvaluation(
            has_sweep=has_sweep,
            swept_levels=swept_levels,
            is_multi_sweep=is_multi_sweep,
            has_htf_sweep=has_htf_sweep,
            confluence_weight=confluence_weight,
            bonus_weight=bonus_weight,
            summary_msg=summary_msg,
            all_levels=all_levels,
        )

    @staticmethod
    def _check_level_sweep_low(
        recent_bars: pd.DataFrame,
        level: float,
        current_price: float,
    ) -> Tuple[bool, int]:
        """
        Checks if low dipped below level, but closed back above or current_price >= level.
        Returns (swept: bool, bars_ago: int).
        """
        min_low = recent_bars["low"].min()
        if min_low < level and current_price >= level:
            # Find the candle where minimum occurred
            low_series = recent_bars["low"].values
            idx_from_end = len(low_series) - 1 - int(np.argmin(low_series))
            return True, idx_from_end
        return False, 0

    @staticmethod
    def _check_level_sweep_high(
        recent_bars: pd.DataFrame,
        level: float,
        current_price: float,
    ) -> Tuple[bool, int]:
        """
        Checks if high spiked above level, but closed back below or current_price <= level.
        Returns (swept: bool, bars_ago: int).
        """
        max_high = recent_bars["high"].max()
        if max_high > level and current_price <= level:
            high_series = recent_bars["high"].values
            idx_from_end = len(high_series) - 1 - int(np.argmax(high_series))
            return True, idx_from_end
        return False, 0
