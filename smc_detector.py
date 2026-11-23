from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from smartmoneyconcepts import smc

from news_filter import NewsFilter
from rr_optimizer import RROptimizer
from session_manager import SessionManager
from liquidity_detector import LiquidityDetector, LiquidityEvaluation
from trade_journal import TradeJournal
from config import (
    ADD_SPREAD_TO_SL,
    BLOCK_EVENING_ENTRIES,
    BLOCK_ROLLOVER_SESSION,
    BONUS_POINTS_SWEEP_OB,
    DEFAULT_RR_RATIO,
    ENABLE_ROOM_TO_RUN_HTF_FILTER,
    ENFORCE_STRONG_STRUCTURAL_SL,
    LIQUIDITY_SWEEP_LOOKBACK_BARS,
    LTF_TIMEFRAME,
    MAX_SL_ATR_MULTIPLIER,
    MIN_SL_PIPS_CROSS_JPY,
    MIN_SL_PIPS_FOREX,
    MIN_SL_PIPS_GBP,
    MIN_SL_PIPS_GOLD,
    MIN_TRADES_FOR_OPTIMAL_RR,
    OPTIMAL_RR_MIN_FLOOR,
    REJECTION_WICK_MIN_RATIO,
    REQUIRE_REJECTION_CANDLE,
    REQUIRE_STRUCTURAL_SPACE_FOR_RR,
    REQUIRE_SWEEP_ENGINEERED_OB,
    ROOM_TO_RUN_HTF_MIN_RR,
    SL_ATR_MULTIPLIER,
    SL_CALCULATION_MODE,
    SL_HYBRID_MIN_ATR_MULT,
    TIGHT_SL_MODE,
    USE_OPTIMAL_RR,
    clean_symbol,
)

logger = logging.getLogger("ForexSMC.SMCDetector")


def get_symbol_pip_size(symbol: str, digits: int = 5) -> float:
    """Returns the base pip scale for a symbol."""
    sym = (symbol or "").upper()
    if "XAU" in sym or "GOLD" in sym:
        return 0.10 if digits == 2 else 0.01
    if "JPY" in sym or digits == 3:
        return 0.01
    return 0.0001


def get_min_sl_distance(symbol: str, point_scale: float = 0.0001) -> float:
    """
    Returns the absolute minimum Stop Loss distance floor in price units.
    Prevents micro-stops below market noise and broker spread thresholds.
    Returns 0.0 if symbol is empty (for generic unit test callers).
    """
    if not symbol:
        return 0.0
    sym = symbol.upper()
    if "XAU" in sym or "GOLD" in sym:
        # 1 standard Gold pip = $0.10 (300 pips = $30.00) regardless of broker 2-digit or 3-digit point scale
        gold_pip = 0.10
        return MIN_SL_PIPS_GOLD * gold_pip
    if "GBP" in sym:
        return MIN_SL_PIPS_GBP * point_scale
    if "JPY" in sym:
        return MIN_SL_PIPS_CROSS_JPY * point_scale
    return MIN_SL_PIPS_FOREX * point_scale


def check_rejection_candle(
    df: pd.DataFrame,
    is_buy: bool,
    min_wick_ratio: float = REJECTION_WICK_MIN_RATIO,
) -> Tuple[bool, str]:
    """
    Validates whether recent candle price action confirms rejection of the zone:
    - For BUY:
      1. Candle closed bullish (close >= open), OR
      2. Lower rejection wick >= min_wick_ratio * total_range (buying absorption at lows).
    - For SELL:
      1. Candle closed bearish (close <= open), OR
      2. Upper rejection wick >= min_wick_ratio * total_range (selling absorption at highs).
    Checks the latest bar (iloc[-1]) and the previous bar (iloc[-2]).
    """
    if df is None or len(df) < 2:
        return True, "Brak wystarczającej liczby świec do analizy odrzucenia"

    for i in [-1, -2]:
        row = df.iloc[i]
        o = float(row.get("open", 0.0))
        h = float(row.get("high", 0.0))
        l = float(row.get("low", 0.0))
        c = float(row.get("close", 0.0))
        rng = h - l
        if rng <= 0:
            continue

        if is_buy:
            if c >= o:
                return True, "Potwierdzenie: świeca zamknęła się prowzrostowo (close >= open)"
            lower_wick = min(o, c) - l
            if (lower_wick / rng) >= min_wick_ratio:
                return True, f"Potwierdzenie: dolny knot odrzucenia {(lower_wick / rng)*100:.1f}% strefy popytowej"
        else:
            if c <= o:
                return True, "Potwierdzenie: świeca zamknęła się prospadkowo (close <= open)"
            upper_wick = h - max(o, c)
            if (upper_wick / rng) >= min_wick_ratio:
                return True, f"Potwierdzenie: górny knot odrzucenia {(upper_wick / rng)*100:.1f}% strefy podażowej"

    direction_str = "wzrostu (spadający nóż bez odrzucenia)" if is_buy else "spadku (dynamiczny wzrost bez odrzucenia)"
    return False, f"Brak potwierdzenia świecowego dla {direction_str}"


def calculate_stop_loss(
    entry: float,
    structural_sl: float,
    atr: float,
    is_buy: bool,
    spread_buffer: float = 0.0,
    point_scale: float = 0.0001,
    symbol: str = "",
) -> Tuple[Optional[float], Optional[float], str]:
    """
    Calculates Stop Loss price and risk amount according to configured SL mode:
    - "STRUCTURE": Pure structural swing / OTE / EMA invalidation level, clamped to minimum SL floor.
    - "ATR": Entry +/- (ATR * multiplier + spread_buffer), clamped to minimum SL floor.
    - "HYBRID": Structural SL, but guarantees a minimum risk buffer of (ATR * hybrid_mult + spread_buffer) and min SL floor.
    Returns (sl, risk, applied_sl_mode). Returns (None, None, applied_sl_mode) if risk <= 5 * point_scale.
    """
    atr_val = atr if (atr is not None and atr > 0) else 0.0
    atr_risk = atr_val * SL_ATR_MULTIPLIER
    min_floor = get_min_sl_distance(symbol, point_scale)

    if is_buy:
        structural_risk = entry - structural_sl
        if SL_CALCULATION_MODE == "ATR":
            final_risk = max(atr_risk + spread_buffer, min_floor)
            sl = entry - final_risk
            applied_sl_mode = "ATR"
        elif SL_CALCULATION_MODE == "HYBRID":
            min_risk = max((atr_val * SL_HYBRID_MIN_ATR_MULT) + spread_buffer, min_floor)
            final_risk = max(structural_risk, min_risk)
            sl = entry - final_risk
            applied_sl_mode = "HYBRID"
        else:
            final_risk = max(structural_risk, min_floor)
            sl = entry - final_risk
            applied_sl_mode = "STRUCTURE"

        risk = entry - sl
    else:
        structural_risk = structural_sl - entry
        if SL_CALCULATION_MODE == "ATR":
            final_risk = max(atr_risk + spread_buffer, min_floor)
            sl = entry + final_risk
            applied_sl_mode = "ATR"
        elif SL_CALCULATION_MODE == "HYBRID":
            min_risk = max((atr_val * SL_HYBRID_MIN_ATR_MULT) + spread_buffer, min_floor)
            final_risk = max(structural_risk, min_risk)
            sl = entry + final_risk
            applied_sl_mode = "HYBRID"
        else:
            final_risk = max(structural_risk, min_floor)
            sl = entry + final_risk
            applied_sl_mode = "STRUCTURE"

        risk = sl - entry

    if risk <= (point_scale * 5):
        return None, None, applied_sl_mode

    return sl, risk, applied_sl_mode


@dataclass
class ConfluenceItem:
    factor: str
    weight: int
    achieved: bool
    detail: str


@dataclass
class TradeSetup:
    """Represents a validated SMC trade opportunity with institutional confluence scoring."""
    symbol: str
    direction: str          # "BUY" or "SELL"
    timeframe: str          # "M15"
    setup_type: str         # e.g., "M15 CHoCH + Fibo 61.8% Pullback (OTE)"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    rr_ratio: float
    htf_trend: str          # "BULLISH", "BEARISH", or "NEUTRAL"
    zone_top: float
    zone_bottom: float
    reason: str
    timestamp: pd.Timestamp
    fibo_50: float = 0.0
    fibo_618: float = 0.0
    fibo_786: float = 0.0
    impulse_high: float = 0.0
    impulse_low: float = 0.0
    choch_level: float = 0.0
    confluence_score: int = 0
    grade: str = "A"       # "A+" (>=85%), "A" (>=70%), "B"
    confluence_breakdown: List[ConfluenceItem] = field(default_factory=list)
    session_name: str = ""
    is_killzone: bool = False
    news_warning: Optional[str] = None
    is_optimal_rr: bool = False
    rr_source_info: str = ""
    strategy_name: str = "SMC"  # "SMC", "ZUMIKO", or "GOLDEN_COMBO"
    mtf_table: Dict[str, str] = field(default_factory=dict)
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_238: float = 0.0
    swing_pattern: str = ""
    is_pyramid: bool = False
    parent_ticket: Optional[int] = None
    pyramid_level: int = 1
    pyramid_profit_r: float = 0.0
    cluster_tickets: List[int] = field(default_factory=list)
    master_tp: float = 0.0
    cluster_locked_profit_usd: float = 0.0
    cluster_locked_profit_r: float = 0.0
    spread_used: float = 0.0
    sl_mode: str = "STRUCTURE"
    swept_levels: List[Dict] = field(default_factory=list)
    is_multi_sweep: bool = False
    has_htf_sweep: bool = False
    liquidity_summary: str = ""
    regime: str = "COMPRESSION"
    dynamic_be_trigger: float = 0.85
    is_adaptive_tp: bool = False
    strong_level: float = 0.0
    weak_level: float = 0.0
    is_sweep_engineered: bool = False
    htf_barrier_price: float = 0.0
    htf_barrier_dist_r: float = 0.0
    vwap: float = 0.0
    poc: float = 0.0
    rsi: float = 0.0
    is_ribbon_expanding: bool = False
    is_healthy_ha: bool = False
    is_main_zumiko_signal: bool = False



class SMCDetector:
    """
    Advanced SMC Detector with Institutional Confluence Weights Engine:
    1. H4 HTF Trend (20%)
    2. M15 CHoCH Confirmation (20%)
    3. Fibonacci OTE Depth (20%)
    4. Order Block / FVG Confluence (15%)
    5. Session Liquidity Sweep (Asian Range Sweep) (15%)
    6. Candle Confirmation / Wick Rejection (10%)
    Threshold: Score >= min_confluence_score (default 70%)
    """

    def __init__(
        self,
        swing_length: int = 5,
        default_rr: float = DEFAULT_RR_RATIO,
        min_confluence_score: int = 70,
        strict_htf_alignment: bool = False,
        use_optimal_rr: bool = USE_OPTIMAL_RR,
        min_trades_for_optimal: int = MIN_TRADES_FOR_OPTIMAL_RR,
        journal: Optional[TradeJournal] = None,
        news_filter: Optional[NewsFilter] = None,
        liquidity_detector: Optional[LiquidityDetector] = None,
    ):
        self.swing_length = swing_length
        self.default_rr = default_rr
        self.min_confluence_score = min_confluence_score
        self.strict_htf_alignment = strict_htf_alignment
        self.use_optimal_rr = use_optimal_rr
        self.min_trades_for_optimal = min_trades_for_optimal
        self.news_filter = news_filter or NewsFilter()
        self.journal = journal or TradeJournal()
        self.liquidity_detector = liquidity_detector or LiquidityDetector()
        from adaptive_manager import AdaptivePositionManager
        self.adaptive_manager = AdaptivePositionManager(journal=self.journal)
        self._latest_swings: Dict[str, Dict] = {}
        self._rr_cache: Dict[Tuple[Optional[str], float], Tuple[float, Tuple[float, bool, str]]] = {}

    def get_effective_rr(self, regime: Optional[str] = None, be_trigger_r: float = 0.85) -> Tuple[float, bool, str]:
        """Determines target RR dynamically using shared RROptimizer logic (cached 60s)."""
        cache_key = (regime, round(be_trigger_r, 2))
        now = time.time()
        if cache_key in self._rr_cache:
            cached_time, cached_val = self._rr_cache[cache_key]
            if (now - cached_time) < 60.0:
                return cached_val

        result = RROptimizer.get_effective_rr(
            journal=self.journal,
            default_rr=self.default_rr,
            use_optimal_rr=self.use_optimal_rr,
            min_trades=self.min_trades_for_optimal,
            regime=regime,
            be_trigger_r=be_trigger_r,
        )
        self._rr_cache[cache_key] = (now, result)
        return result

    def get_latest_m15_swing(self, symbol: str, is_buy: bool) -> Tuple[float, float, float]:
        """
        Returns cached (swing_price, atr, point_scale) for symbol from latest M15 scan.
        If is_buy: returns (swing_low, atr, point_scale)
        If not is_buy: returns (swing_high, atr, point_scale)
        Supports both raw broker symbols (e.g. 'EURUSD.pro') and clean symbols ('EURUSD').
        """
        cached = self._latest_swings.get(symbol.upper(), {})
        if not cached:
            cached = self._latest_swings.get(clean_symbol(symbol), {})
        if not cached:
            return 0.0, 0.0, 0.0001
        swing_price = cached.get("swing_low", 0.0) if is_buy else cached.get("swing_high", 0.0)
        return float(swing_price), float(cached.get("atr", 0.0)), float(cached.get("point_scale", 0.0001))

    def determine_htf_trend(self, df_htf: pd.DataFrame) -> str:
        """Determines H4 trend using BOS/CHoCH and swing highs/lows."""
        if df_htf is None or len(df_htf) < 40:
            return "NEUTRAL"

        try:
            swings = smc.swing_highs_lows(df_htf, swing_length=self.swing_length)
            bos_choch = smc.bos_choch(df_htf, swings)

            recent_structure = bos_choch.dropna(subset=["BOS", "CHOCH"], how="all")
            if not recent_structure.empty:
                last_event = recent_structure.iloc[-1]
                signal = last_event.get("CHOCH") if pd.notna(last_event.get("CHOCH")) else last_event.get("BOS")
                if signal == 1:
                    return "BULLISH"
                elif signal == -1:
                    return "BEARISH"

            valid_swings = swings[swings["HighLow"].notna() & (swings["HighLow"] != 0)]
            if len(valid_swings) >= 4:
                highs = valid_swings[valid_swings["HighLow"] == 1]["Level"].values
                lows = valid_swings[valid_swings["HighLow"] == -1]["Level"].values
                if len(highs) >= 2 and len(lows) >= 2:
                    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
                        return "BULLISH"
                    elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
                        return "BEARISH"
        except Exception:
            pass

        ema50 = df_htf["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        ema200 = df_htf["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        if ema50 > ema200:
            return "BULLISH"
        elif ema50 < ema200:
            return "BEARISH"
        return "NEUTRAL"

    def _check_candle_rejection(self, current_candle: pd.Series, direction: str) -> Tuple[bool, str]:
        """
        Checks if the current or previous candle left a rejection wick (>= 30% of total candle range).
        """
        o, h, l, c = current_candle["open"], current_candle["high"], current_candle["low"], current_candle["close"]
        candle_range = h - l
        if candle_range <= 0:
            return False, "Świeca bez zakresu"

        if direction == "BUY":
            # Long lower wick indicates buying pressure
            lower_wick = min(o, c) - l
            wick_ratio = lower_wick / candle_range
            if wick_ratio >= 0.30:
                return True, f"Knot odrzucenia dołu: {wick_ratio * 100:.0f}% świecy"
            return False, f"Dolny knot {wick_ratio * 100:.0f}% (< 30%)"

        elif direction == "SELL":
            # Long upper wick indicates selling pressure
            upper_wick = h - max(o, c)
            wick_ratio = upper_wick / candle_range
            if wick_ratio >= 0.30:
                return True, f"Knot odrzucenia góry: {wick_ratio * 100:.0f}% świecy"
            return False, f"Górny knot {wick_ratio * 100:.0f}% (< 30%)"

        return False, "Brak"

    @staticmethod
    def _check_sweep_engineered_ob(
        df: pd.DataFrame,
        ob_bar_idx: int,
        direction: str,
        lookback_bars: int = 5,
    ) -> Tuple[bool, str]:
        """
        Checks if the candle forming an Order Block (or its immediate predecessor)
        performed an institutional liquidity sweep (Manipulation Grab) before breaking structure.
        (Smart Risk Mentorship Hack #2).
        """
        if ob_bar_idx < 1 or df is None or len(df) <= ob_bar_idx:
            return False, ""

        is_buy = direction == "BUY"
        win_start = max(0, ob_bar_idx - lookback_bars)
        prior_bars = df.iloc[win_start:ob_bar_idx]
        if prior_bars.empty:
            return False, ""

        if is_buy:
            prior_min_low = float(prior_bars["low"].min())
            curr_low = float(df.iloc[ob_bar_idx]["low"])
            if curr_low < prior_min_low:
                return True, f"Dołek OB ({curr_low:.5f}) wybił Sell-Side Liquidity ({prior_min_low:.5f})"

            if ob_bar_idx >= 2:
                prior_2_win = df.iloc[max(0, ob_bar_idx - 1 - lookback_bars):ob_bar_idx - 1]
                if not prior_2_win.empty and float(df.iloc[ob_bar_idx - 1]["low"]) < float(prior_2_win["low"].min()):
                    return True, f"Świeca przed OB wybiła Sell-Side Liquidity ({float(prior_2_win['low'].min()):.5f})"
        else:
            prior_max_high = float(prior_bars["high"].max())
            curr_high = float(df.iloc[ob_bar_idx]["high"])
            if curr_high > prior_max_high:
                return True, f"Szczyt OB ({curr_high:.5f}) wybił Buy-Side Liquidity ({prior_max_high:.5f})"

            if ob_bar_idx >= 2:
                prior_2_win = df.iloc[max(0, ob_bar_idx - 1 - lookback_bars):ob_bar_idx - 1]
                if not prior_2_win.empty and float(df.iloc[ob_bar_idx - 1]["high"]) > float(prior_2_win["high"].max()):
                    return True, f"Świeca przed OB wybiła Buy-Side Liquidity ({float(prior_2_win['high'].max()):.5f})"

        return False, ""

    @staticmethod
    def find_nearest_htf_barrier(
        df_htf: Optional[pd.DataFrame],
        direction: str,
        entry_price: float,
        lookback: int = 150,
    ) -> Tuple[Optional[float], str]:
        """
        Finds the nearest unmitigated opposing HTF Supply/Demand barrier ahead of the price.
        (Smart Risk Mentorship Hack #1 & #5).
        Returns: (barrier_price, barrier_desc) or (None, "")
        """
        if df_htf is None or len(df_htf) < 30 or entry_price <= 0:
            return None, ""

        is_buy = direction == "BUY"
        opposing_ob_val = -1.0 if is_buy else 1.0  # If BUY, oppose with Supply (-1.0); if SELL, Demand (1.0)
        df_slice = df_htf.iloc[-lookback:] if len(df_htf) > lookback else df_htf
        current_bar = len(df_slice) - 1

        candidate_barriers: List[Tuple[float, str]] = []

        # 1. Check HTF unmitigated Order Blocks
        try:
            swings_htf = smc.swing_highs_lows(df_slice, swing_length=3)
            ob_htf = smc.ob(df_slice, swings_htf)
            for _, row in ob_htf.iterrows():
                if row.get("OB") == opposing_ob_val:
                    mit = row.get("MitigatedIndex", 0.0)
                    is_unmitigated = (mit == 0.0 or pd.isna(mit) or mit >= current_bar)
                    if is_unmitigated:
                        top, btm = float(row["Top"]), float(row["Bottom"])
                        if is_buy and btm > entry_price:
                            candidate_barriers.append((btm, f"HTF Supply OB [{btm:.4f} - {top:.4f}]"))
                        elif not is_buy and top < entry_price:
                            candidate_barriers.append((top, f"HTF Demand OB [{btm:.4f} - {top:.4f}]"))
        except Exception as e:
            logger.debug(f"Error checking HTF OB barrier: {e}")

        # 2. Check HTF unmitigated Fair Value Gaps
        try:
            fvg_htf = smc.fvg(df_slice)
            for _, row in fvg_htf.iterrows():
                if row.get("FVG") == opposing_ob_val:
                    mit = row.get("MitigatedIndex", 0.0)
                    is_unmitigated = (mit == 0.0 or pd.isna(mit) or mit >= current_bar)
                    if is_unmitigated:
                        top, btm = float(row["Top"]), float(row["Bottom"])
                        if is_buy and btm > entry_price:
                            candidate_barriers.append((btm, f"HTF Supply FVG [{btm:.4f} - {top:.4f}]"))
                        elif not is_buy and top < entry_price:
                            candidate_barriers.append((top, f"HTF Demand FVG [{btm:.4f} - {top:.4f}]"))
        except Exception as e:
            logger.debug(f"Error checking HTF FVG barrier: {e}")

        if not candidate_barriers:
            return None, ""

        if is_buy:
            nearest = min(candidate_barriers, key=lambda x: x[0])
            return nearest[0], nearest[1]
        else:
            nearest = max(candidate_barriers, key=lambda x: x[0])
            return nearest[0], nearest[1]

    def _check_ob_and_fvg_confluence(
        self,
        df: pd.DataFrame,
        swings: pd.DataFrame,
        leg_start_idx: int,
        z_bottom: float,
        z_top: float,
        direction: str,
    ) -> Tuple[bool, str, bool, str]:
        """
        Checks if an unmitigated Order Block (OB) or Fair Value Gap (FVG)
        originating in the impulse leg aligns with the entry zone [z_bottom, z_top].
        Returns: (has_zone, zone_description, is_sweep_engineered, sweep_details)
        """
        target_val = 1.0 if direction == "BUY" else -1.0
        current_bar = len(df) - 1
        has_ob = False
        ob_details = []
        has_fvg = False
        fvg_details = []
        is_sweep_ob = False
        sweep_ob_detail = ""

        # 1. Check Order Blocks across the impulse leg
        try:
            ob_df = smc.ob(df, swings)
            obs_in_leg = ob_df.iloc[max(0, leg_start_idx - 5):]
            for bar_idx, row in obs_in_leg.iterrows():
                if row.get("OB") == target_val:
                    mit = row.get("MitigatedIndex", 0.0)
                    is_unmitigated = (mit == 0.0 or pd.isna(mit) or mit >= current_bar)
                    if is_unmitigated:
                        top, btm = float(row["Top"]), float(row["Bottom"])
                        if not (btm > z_top or top < z_bottom):
                            has_ob = True
                            is_sw, sw_msg = self._check_sweep_engineered_ob(df, int(bar_idx), direction)
                            if is_sw:
                                is_sweep_ob = True
                                sweep_ob_detail = sw_msg
                                ob_details.append(f"Sweep-OB [{btm:.4f} - {top:.4f}] ({sw_msg})")
                            else:
                                ob_details.append(f"OB [{btm:.4f} - {top:.4f}]")
                            break
        except Exception as e:
            logger.debug(f"Error checking OB overlap: {e}")

        # 2. Check Fair Value Gaps (FVG) across the impulse leg
        try:
            fvg_df = smc.fvg(df)
            fvgs_in_leg = fvg_df.iloc[max(0, leg_start_idx - 5):]
            for _, row in fvgs_in_leg.iterrows():
                if row.get("FVG") == target_val:
                    mit = row.get("MitigatedIndex", 0.0)
                    is_unmitigated = (mit == 0.0 or pd.isna(mit) or mit >= current_bar)
                    if is_unmitigated:
                        top, btm = float(row["Top"]), float(row["Bottom"])
                        if not (btm > z_top or top < z_bottom):
                            has_fvg = True
                            fvg_details.append(f"FVG [{btm:.4f} - {top:.4f}]")
                            break
        except Exception as e:
            logger.debug(f"Error checking FVG overlap: {e}")

        zone_desc = ""
        if has_ob and has_fvg:
            zone_desc = f"Zbieżność {direction} {ob_details[0]} + {fvg_details[0]}"
        elif has_ob:
            zone_desc = f"Strefa {direction} {ob_details[0]}"
        elif has_fvg:
            zone_desc = f"Strefa {direction} {fvg_details[0]}"
        else:
            zone_desc = "Brak strefy OB/FVG w tym rejonie"

        return (has_ob or has_fvg), zone_desc, is_sweep_ob, sweep_ob_detail

    def scan_for_setups(
        self,
        symbol: str,
        df_ltf: pd.DataFrame,
        df_htf: Optional[pd.DataFrame] = None,
        spread: float = 0.0,
        df_d1: Optional[pd.DataFrame] = None,
        df_w1: Optional[pd.DataFrame] = None,
        df_h1: Optional[pd.DataFrame] = None,
    ) -> List[TradeSetup]:
        setups: List[TradeSetup] = []
        if df_ltf is None or len(df_ltf) < 60:
            return setups

        htf_trend = self.determine_htf_trend(df_htf) if df_htf is not None else "NEUTRAL"
        current_candle = df_ltf.iloc[-1]
        current_time = current_candle["time"]

        digits = 2 if "JPY" in symbol.upper() or "XAU" in symbol.upper() or "GOLD" in symbol.upper() else 4
        point_scale = get_symbol_pip_size(symbol, digits)
        atr = (df_ltf["high"] - df_ltf["low"]).tail(14).mean()
        buffer = max(atr * 0.2, point_scale * 5)

        # Session & News Context
        session_info = SessionManager.get_current_session(current_time)
        if BLOCK_ROLLOVER_SESSION and ("Rollover" in session_info.get("session_name", "") or session_info.get("is_rollover", False)):
            logger.info(f"🛡️ [FILTER] Pomijam {symbol}: Aktywna sesja {session_info.get('session_name')} (BLOCK_ROLLOVER_SESSION=True).")
            return setups
        if BLOCK_EVENING_ENTRIES and session_info.get("is_evening_blocked", False):
            logger.info(f"🛡️ [FILTER] Pomijam {symbol}: Blokada wejść wieczornych ({session_info.get('session_name')}, {session_info.get('time_utc')}).")
            return setups

        has_news, news_warn, _ = self.news_filter.check_news(symbol)

        # M15 Swings & CHoCH / BOS (Trend Reversals & Trend Continuations)
        try:
            swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=self.swing_length)
            bos_choch_ltf = smc.bos_choch(df_ltf, swings_ltf)
        except Exception as e:
            logger.debug(f"Error computing swings / bos_choch for {symbol}: {e}")
            return setups

        # Cache confirmed swing extremes for M15 trailing SL
        try:
            valid_swings = swings_ltf[swings_ltf["HighLow"].notna() & (swings_ltf["HighLow"] != 0)]
            if len(valid_swings) > 0:
                highs = valid_swings[valid_swings["HighLow"] == 1]["Level"].values
                lows = valid_swings[valid_swings["HighLow"] == -1]["Level"].values
                swing_data = {
                    "swing_high": float(highs[-1]) if len(highs) > 0 else 0.0,
                    "swing_low": float(lows[-1]) if len(lows) > 0 else 0.0,
                    "atr": float(atr),
                    "point_scale": point_scale,
                }
                self._latest_swings[symbol.upper()] = swing_data
                clean_k = clean_symbol(symbol)
                if clean_k and clean_k != symbol.upper():
                    self._latest_swings[clean_k] = swing_data
        except Exception as e:
            logger.debug(f"Error caching M15 swings for {symbol}: {e}")

        # Calculate ATR percentile from LTF history
        rolling_atr = (df_ltf["high"] - df_ltf["low"]).tail(min(len(df_ltf), 100))
        atr_percentile = (sum(1 for val in rolling_atr if atr >= val) / len(rolling_atr)) * 100.0 if len(rolling_atr) > 0 else 50.0

        # Extract both CHoCH and BOS events
        structure_events = bos_choch_ltf[bos_choch_ltf["CHOCH"].notna() | bos_choch_ltf["BOS"].notna()].copy()
        if structure_events.empty:
            return setups

        structure_events["event_dir"] = structure_events["CHOCH"].fillna(structure_events["BOS"])
        structure_events["event_name"] = np.where(structure_events["CHOCH"].notna(), "CHoCH", "BOS")
        # Sort by BrokenIndex descending (newest breakout first)
        structure_events = structure_events.sort_values(by="BrokenIndex", ascending=False)

        current_bar_idx = len(df_ltf) - 1
        max_structure_age_bars = 30  # Max 30 bars (~7.5 hours on M15) for fresh structure

        evaluated_directions = set()

        for swing_bar_idx, ev_row in structure_events.iterrows():
            broken_idx = int(ev_row.get("BrokenIndex", 0))
            event_dir = int(ev_row["event_dir"])
            event_name = str(ev_row["event_name"])
            broken_level = float(ev_row.get("Level", 0.0))
            swing_bar_idx = int(swing_bar_idx)

            dir_str = "BUY" if event_dir == 1 else "SELL"

            if dir_str in evaluated_directions:
                continue
            evaluated_directions.add(dir_str)

            # Check 1: Freshness (Breakout must have occurred within max_structure_age_bars)
            bars_since_break = current_bar_idx - broken_idx
            structure_age = bars_since_break
            if bars_since_break > max_structure_age_bars or bars_since_break < 0:
                logger.debug(f"{symbol} {dir_str} {event_name} pominięty: zbyt stary ({bars_since_break} bars > {max_structure_age_bars}).")
                continue

            # Check 2: Invalidation (Ensure no opposing breakout occurred after this breakout)
            has_opposing_break = any(
                (int(r["event_dir"]) != event_dir and int(r.get("BrokenIndex", 0)) > broken_idx)
                for _, r in structure_events.iterrows()
            )
            if has_opposing_break:
                logger.debug(f"{symbol} {dir_str} {event_name} unieważniony przez nowsze przeciwne wyłamanie.")
                continue

            # Check 3: Determine accurate SMC impulse leg (dealing range)
            if dir_str == "BUY":
                win_start = max(0, min(swing_bar_idx - 5, broken_idx - 15))
                search_win = df_ltf.iloc[win_start:broken_idx + 1]
                if search_win.empty:
                    continue
                min_low_rel_idx = int(search_win["low"].argmin())
                impulse_low = float(search_win["low"].iloc[min_low_rel_idx])

                post_break_win = df_ltf.iloc[broken_idx:]
                if post_break_win.empty:
                    continue
                max_high_rel_idx = int(post_break_win["high"].argmax())
                impulse_high = float(post_break_win["high"].iloc[max_high_rel_idx])
                leg_start_idx = win_start + min_low_rel_idx
            else:
                win_start = max(0, min(swing_bar_idx - 5, broken_idx - 15))
                search_win = df_ltf.iloc[win_start:broken_idx + 1]
                if search_win.empty:
                    continue
                max_high_rel_idx = int(search_win["high"].argmax())
                impulse_high = float(search_win["high"].iloc[max_high_rel_idx])

                post_break_win = df_ltf.iloc[broken_idx:]
                if post_break_win.empty:
                    continue
                min_low_rel_idx = int(post_break_win["low"].argmin())
                impulse_low = float(post_break_win["low"].iloc[min_low_rel_idx])
                leg_start_idx = win_start + max_high_rel_idx

            # Classify market regime and determine dynamic BE & optimal RR
            regime = self.adaptive_manager.classify_regime(
                htf_trend=htf_trend,
                ltf_trend=dir_str,
                atr_percentile=atr_percentile,
                is_news_window=has_news,
            )
            dynamic_be = self.adaptive_manager.get_dynamic_be_trigger(regime)
            target_rr, is_optimal_rr, rr_source = self.get_effective_rr(
                regime=regime.value,
                be_trigger_r=dynamic_be,
            )

            setup = self._build_smc_setup(
                direction=dir_str,
                symbol=symbol,
                impulse_low=impulse_low,
                impulse_high=impulse_high,
                leg_start_idx=leg_start_idx,
                event_name=event_name,
                df_ltf=df_ltf,
                swings_ltf=swings_ltf,
                current_candle=current_candle,
                htf_trend=htf_trend,
                broken_level=broken_level,
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
                df_d1=df_d1,
                df_w1=df_w1,
                df_h1=df_h1,
                df_htf=df_htf,
                structure_age=structure_age,
                regime=regime.value,
                dynamic_be=dynamic_be,
            )
            if setup:
                setups.append(setup)
                break

        return setups

    def _build_smc_setup(
        self,
        direction: str,
        symbol: str,
        impulse_low: Optional[float] = None,
        impulse_high: Optional[float] = None,
        leg_start_idx: int = 0,
        event_name: str = "CHoCH",
        df_ltf: Optional[pd.DataFrame] = None,
        swings_ltf: Optional[pd.DataFrame] = None,
        current_candle: Optional[pd.Series] = None,
        htf_trend: str = "NEUTRAL",
        broken_level: float = 0.0,
        target_rr: float = 2.0,
        is_optimal_rr: bool = False,
        rr_source: str = "Default",
        session_info: Optional[dict] = None,
        news_warn: Optional[str] = None,
        current_time: Optional[datetime] = None,
        digits: int = 4,
        point_scale: float = 0.0001,
        buffer: float = 0.0005,
        spread: float = 0.0,
        df_d1: Optional[pd.DataFrame] = None,
        df_w1: Optional[pd.DataFrame] = None,
        df_h1: Optional[pd.DataFrame] = None,
        df_htf: Optional[pd.DataFrame] = None,
        leg_df: Optional[pd.DataFrame] = None,
        structure_age: int = 5,
        regime: str = "COMPRESSION",
        dynamic_be: float = 0.85,
    ) -> Optional[TradeSetup]:
        if leg_df is not None and (impulse_low is None or impulse_high is None):
            impulse_low = float(leg_df["low"].min())
            impulse_high = float(leg_df["high"].max())

        if impulse_low is None or impulse_high is None or df_ltf is None or current_candle is None:
            return None

        if session_info is None:
            session_info = {"session_name": "Standard", "is_killzone": False}
        if current_time is None:
            current_time = current_candle["time"] if "time" in current_candle else datetime.utcnow()
        is_buy = direction == "BUY"
        expected_htf = "BULLISH" if is_buy else "BEARISH"
        trend_ok = (not self.strict_htf_alignment) or (htf_trend in [expected_htf, "NEUTRAL"])
        if not trend_ok:
            return None

        current_price = current_candle["close"]
        impulse_range = impulse_high - impulse_low
        if impulse_range <= (point_scale * 10):
            return None

        spread_buffer = spread if ADD_SPREAD_TO_SL else 0.0
        effective_buffer = buffer + spread_buffer
        atr = (df_ltf["high"] - df_ltf["low"]).tail(14).mean()

        # Pre-evaluate liquidity sweeps so Stop Loss can be protected beyond swept levels
        liq_eval = self.liquidity_detector.evaluate_sweeps(
            df_ltf=df_ltf,
            direction=direction,
            df_d1=df_d1,
            df_w1=df_w1,
            df_h1=df_h1,
        )

        if is_buy:
            fibo_50 = impulse_high - (0.500 * impulse_range)
            fibo_618 = impulse_high - (0.618 * impulse_range)
            fibo_705 = impulse_high - (0.705 * impulse_range)
            fibo_786 = impulse_high - (0.786 * impulse_range)
            # Price must be in discount (at or below 61.8% pullback) and not breaking below impulse low
            if not ((current_price <= fibo_618 + buffer) and (current_price >= impulse_low - buffer)):
                return None

            # Rejection Candle Filter: wait for confirmation rather than entering a falling knife
            if REQUIRE_REJECTION_CANDLE:
                rejection_ok, reject_reason = check_rejection_candle(df_ltf, is_buy=True)
                if not rejection_ok:
                    logger.info(f"🛡️ [FILTER] Odrzucono setup {symbol} BUY: {reject_reason}")
                    return None

            entry = current_price
            strong_level = impulse_low
            weak_level = impulse_high

            if ENFORCE_STRONG_STRUCTURAL_SL:
                # Smart Risk Rule: SL strictly protected behind Strong Low
                structural_sl = strong_level - effective_buffer
            elif TIGHT_SL_MODE:
                recent_pullback_low = df_ltf.iloc[-5:]["low"].min()
                structural_sl = min(fibo_786, recent_pullback_low) - effective_buffer
            else:
                structural_sl = strong_level - effective_buffer

            # Guarantee SL protects beyond swept liquidity levels
            if liq_eval.has_sweep and liq_eval.swept_levels:
                sweep_min_price = min(s.price for s in liq_eval.swept_levels)
                lookback_bars = min(len(df_ltf), LIQUIDITY_SWEEP_LOOKBACK_BARS)
                sweep_window_low = df_ltf.iloc[-lookback_bars:]["low"].min()
                structural_sl = min(structural_sl, min(sweep_min_price, sweep_window_low) - effective_buffer)

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

            # Filter: Max SL vs ATR
            if MAX_SL_ATR_MULTIPLIER > 0 and atr > 0 and risk > (atr * MAX_SL_ATR_MULTIPLIER):
                logger.info(
                    f"🛡️ [FILTER] Odrzucono setup {symbol} BUY: SL zbyt szeroki "
                    f"({risk / point_scale:.1f}p > {MAX_SL_ATR_MULTIPLIER:.1f}*ATR)."
                )
                return None

            # SMC TP Targeting: Primary target is Buy-Side Liquidity (BSL) at impulse_high
            structural_space = impulse_high - entry
            structural_rr = (structural_space / risk) if risk > 0 else 0.0

            # Filter: Require at least OPTIMAL_RR_MIN_FLOOR (min 2.0R) structural room to external liquidity
            min_allowed_rr = max(OPTIMAL_RR_MIN_FLOOR, 1.5)
            if REQUIRE_STRUCTURAL_SPACE_FOR_RR and structural_rr < min_allowed_rr:
                logger.info(
                    f"🛡️ [FILTER] Odrzucono setup {symbol} BUY: Zbyt mała przestrzeń do szczytu fali BSL "
                    f"({structural_rr:.2f}R < {min_allowed_rr:.1f}R)."
                )
                return None

            if structural_rr >= target_rr:
                tp1 = entry + (risk * target_rr)
                tp2 = max(impulse_high, entry + (risk * (target_rr + 0.8)))
                actual_rr = round(target_rr, 1)
            else:
                # Cap TP1 at impulse_high (BSL) so price doesn't reverse before taking profit
                tp1 = impulse_high
                tp2 = impulse_high + (impulse_range * 0.272)
                actual_rr = round(structural_rr, 1)

            zone_top = fibo_618
            zone_bottom = fibo_786
        else:
            fibo_50 = impulse_low + (0.500 * impulse_range)
            fibo_618 = impulse_low + (0.618 * impulse_range)
            fibo_705 = impulse_low + (0.705 * impulse_range)
            fibo_786 = impulse_low + (0.786 * impulse_range)
            # Price must be in premium (at or above 61.8% pullback) and not breaking above impulse high
            if not ((current_price >= fibo_618 - buffer) and (current_price <= impulse_high + buffer)):
                return None

            # Rejection Candle Filter: wait for confirmation rather than entering a rising knife
            if REQUIRE_REJECTION_CANDLE:
                rejection_ok, reject_reason = check_rejection_candle(df_ltf, is_buy=False)
                if not rejection_ok:
                    logger.info(f"🛡️ [FILTER] Odrzucono setup {symbol} SELL: {reject_reason}")
                    return None

            entry = current_price
            strong_level = impulse_high
            weak_level = impulse_low

            if ENFORCE_STRONG_STRUCTURAL_SL:
                # Smart Risk Rule: SL strictly protected behind Strong High
                structural_sl = strong_level + effective_buffer
            elif TIGHT_SL_MODE:
                recent_pullback_high = df_ltf.iloc[-5:]["high"].max()
                structural_sl = max(fibo_786, recent_pullback_high) + effective_buffer
            else:
                structural_sl = strong_level + effective_buffer

            # Guarantee SL protects beyond swept liquidity levels
            if liq_eval.has_sweep and liq_eval.swept_levels:
                sweep_max_price = max(s.price for s in liq_eval.swept_levels)
                lookback_bars = min(len(df_ltf), LIQUIDITY_SWEEP_LOOKBACK_BARS)
                sweep_window_high = df_ltf.iloc[-lookback_bars:]["high"].max()
                structural_sl = max(structural_sl, max(sweep_max_price, sweep_window_high) + effective_buffer)

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

            # Filter: Max SL vs ATR
            if MAX_SL_ATR_MULTIPLIER > 0 and atr > 0 and risk > (atr * MAX_SL_ATR_MULTIPLIER):
                logger.info(
                    f"🛡️ [FILTER] Odrzucono setup {symbol} SELL: SL zbyt szeroki "
                    f"({risk / point_scale:.1f}p > {MAX_SL_ATR_MULTIPLIER:.1f}*ATR)."
                )
                return None

            # SMC TP Targeting: Primary target is Sell-Side Liquidity (SSL) at impulse_low
            structural_space = entry - impulse_low
            structural_rr = (structural_space / risk) if risk > 0 else 0.0

            # Filter: Require at least OPTIMAL_RR_MIN_FLOOR (min 2.0R) structural room to external liquidity
            min_allowed_rr = max(OPTIMAL_RR_MIN_FLOOR, 1.5)
            if REQUIRE_STRUCTURAL_SPACE_FOR_RR and structural_rr < min_allowed_rr:
                logger.info(
                    f"🛡️ [FILTER] Odrzucono setup {symbol} SELL: Zbyt mała przestrzeń do dołka fali SSL "
                    f"({structural_rr:.2f}R < {min_allowed_rr:.1f}R)."
                )
                return None

            if structural_rr >= target_rr:
                tp1 = entry - (risk * target_rr)
                tp2 = min(impulse_low, entry - (risk * (target_rr + 0.8)))
                actual_rr = round(target_rr, 1)
            else:
                # Cap TP1 at impulse_low (SSL) so price doesn't reverse before taking profit
                tp1 = impulse_low
                tp2 = impulse_low - (impulse_range * 0.272)
                actual_rr = round(structural_rr, 1)

            zone_top = fibo_786
            zone_bottom = fibo_618

        # Smart Risk Hack #1 & #5: Room to Run & HTF Barrier Filter
        htf_barrier_price = 0.0
        htf_barrier_dist_r = 0.0

        if ENABLE_ROOM_TO_RUN_HTF_FILTER and risk > 0:
            barrier_p, barrier_desc = self.find_nearest_htf_barrier(df_h1, direction, entry)
            if barrier_p is None and df_htf is not None:
                barrier_p, barrier_desc = self.find_nearest_htf_barrier(df_htf, direction, entry)

            if barrier_p is not None:
                htf_barrier_price = barrier_p
                room_dist = (barrier_p - entry) if is_buy else (entry - barrier_p)
                room_r = room_dist / risk
                htf_barrier_dist_r = round(room_r, 2)

                if room_r < ROOM_TO_RUN_HTF_MIN_RR:
                    logger.info(
                        f"🛡️ [FILTER] Odrzucono setup {symbol} {direction}: "
                        f"Zbyt mała przestrzeń do przeciwległej strefy HTF ({room_r:.2f}R < {ROOM_TO_RUN_HTF_MIN_RR:.1f}R, {barrier_desc})."
                    )
                    return None

                # Cap TP1 at or before the opposing HTF barrier if barrier is closer than current TP1
                # (Smart Risk Hack #1 & #5: Never project Take Profit into an unmitigated HTF S/D barrier)
                if is_buy and barrier_p < tp1:
                    logger.info(
                        f"🎯 [TP CAPPING] {symbol} BUY: Skrócono TP1 z {tp1:.{digits}f} do bariery HTF {barrier_p:.{digits}f} "
                        f"({room_r:.1f}R zamiast {actual_rr:.1f}R, {barrier_desc})."
                    )
                    tp1 = barrier_p
                    actual_rr = round(room_r, 1)
                elif not is_buy and barrier_p > tp1:
                    logger.info(
                        f"🎯 [TP CAPPING] {symbol} SELL: Skrócono TP1 z {tp1:.{digits}f} do bariery HTF {barrier_p:.{digits}f} "
                        f"({room_r:.1f}R zamiast {actual_rr:.1f}R, {barrier_desc})."
                    )
                    tp1 = barrier_p
                    actual_rr = round(room_r, 1)

        breakdown: List[ConfluenceItem] = []
        total_score = 0

        # 1. H4 Trend Alignment (25% max, or -20% penalty for counter-trend)
        h4_match = htf_trend == expected_htf
        if h4_match:
            w1 = 25
            h4_desc = f"H4 zgodny ({htf_trend}) (+25%)"
        elif htf_trend == "NEUTRAL":
            w1 = 10
            h4_desc = f"H4 neutralny (+10%)"
        else:
            w1 = -20
            h4_desc = f"H4 przeciwny: {htf_trend} (-20% KARA)"
        total_score += w1
        breakdown.append(ConfluenceItem(
            factor="Zgodność z trendem H4",
            weight=25,
            achieved=h4_match,
            detail=h4_desc
        ))

        # 2. Institutional POI: Order Block / FVG Confluence (25%) + Sweep-Engineered OB
        ob_fibo_low = min(fibo_786, fibo_618)
        ob_fibo_high = max(fibo_786, fibo_618)
        has_ob_fvg, ob_detail, is_sweep_ob, sweep_detail = self._check_ob_and_fvg_confluence(
            df_ltf, swings_ltf, leg_start_idx, ob_fibo_low, ob_fibo_high, direction
        )

        if REQUIRE_SWEEP_ENGINEERED_OB and not is_sweep_ob:
            logger.info(f"🛡️ [FILTER] Odrzucono setup {symbol} {direction}: Wymagany Sweep-Engineered OB (Smart Risk Hack #2).")
            return None

        w2 = 25 if has_ob_fvg else 0
        total_score += w2
        breakdown.append(ConfluenceItem(
            factor="Strefa Instytucjonalna (OB / FVG)",
            weight=25,
            achieved=has_ob_fvg,
            detail=f"{ob_detail} (+{w2}%)"
        ))

        # Sweep-Engineered OB Bonus (Smart Risk Hack #2)
        if is_sweep_ob:
            total_score += BONUS_POINTS_SWEEP_OB
            breakdown.append(ConfluenceItem(
                factor="Sweep-Engineered OB (Hack #2)",
                weight=BONUS_POINTS_SWEEP_OB,
                achieved=True,
                detail=f"Instytucjonalny Manipulation Grab: {sweep_detail} (+{BONUS_POINTS_SWEEP_OB}%)"
            ))

        # 3. Market Structure Confirmation (CHoCH / BOS) (15%) with Smart Risk Strong/Weak Mapping
        choch_label = "szczytu" if is_buy else "dołka"
        event_desc = "Odwrócenie trendu (CHoCH)" if event_name == "CHoCH" else "Kontynuacja trendu (BOS)"
        is_fresh = structure_age <= 10
        w3 = 15 if is_fresh else 10
        total_score += w3
        fresh_desc = "Świeże wybicie" if is_fresh else f"Wybicie ({structure_age} b. temu)"
        strong_label = "Strong Low" if is_buy else "Strong High"
        weak_label = "Weak High" if is_buy else "Weak Low"
        breakdown.append(ConfluenceItem(
            factor=f"Potwierdzenie M15 {event_name}",
            weight=15,
            achieved=True,
            detail=f"{event_desc} ({fresh_desc}): {broken_level:.{digits}f} [{strong_label}: {strong_level:.{digits}f} -> {weak_label}: {weak_level:.{digits}f}] (+{w3}%)"
        ))

        # Room to Run HTF confluence (Hack #1 & #5)
        if ENABLE_ROOM_TO_RUN_HTF_FILTER and (df_h1 is not None or df_htf is not None):
            if htf_barrier_price > 0:
                breakdown.append(ConfluenceItem(
                    factor="Room to Run HTF",
                    weight=5,
                    achieved=True,
                    detail=f"Czysta droga: {htf_barrier_dist_r:.1f}R do strefy HTF ({htf_barrier_price:.{digits}f}) (+5%)"
                ))
                total_score += 5
            else:
                breakdown.append(ConfluenceItem(
                    factor="Room to Run HTF",
                    weight=5,
                    achieved=True,
                    detail="Czysta droga: brak przeciwnych stref Supply/Demand HTF na wykresie (+5%)"
                ))
                total_score += 5

        # 4. Advanced Liquidity Sweep (15% base + 5% bonus for Multi-Sweep / HTF)
        has_sweep = liq_eval.has_sweep
        w4 = liq_eval.confluence_weight
        bonus = liq_eval.bonus_weight
        total_score += (w4 + bonus)
        breakdown.append(ConfluenceItem(
            factor="Wybicie płynności (Sweep)",
            weight=15,
            achieved=has_sweep,
            detail=f"{liq_eval.summary_msg} (+{w4 + bonus}%)"
        ))

        # 5. Institutional Session Timing (Killzone) (10%)
        is_killzone = session_info.get("is_killzone", False)
        session_name = session_info.get("session_name", "Standard")
        w5 = 10 if is_killzone else 0
        total_score += w5
        breakdown.append(ConfluenceItem(
            factor="Timing Sesyjny (Killzone)",
            weight=10,
            achieved=is_killzone,
            detail=f"Sesja: {session_name} (+{w5}%)"
        ))

        # 6. Fibonacci Retracement Depth (Discount / Premium OTE) (10%)
        is_deep_ote = (current_price <= fibo_705) if is_buy else (current_price >= fibo_705)
        w6 = 10 if is_deep_ote else 5
        total_score += w6
        fibo_desc = "Głębokie OTE (70.5% - 78.6%)" if is_deep_ote else "Golden Pocket (61.8% - 70.5%)"
        breakdown.append(ConfluenceItem(
            factor="Strefa Fibonacciego (OTE)",
            weight=10,
            achieved=True,
            detail=f"{fibo_desc} (+{w6}%)"
        ))

        # 7. Candlestick Wick Rejection (5%)
        has_rejection, wick_msg = self._check_candle_rejection(current_candle, direction)
        w7 = 5 if has_rejection else 0
        total_score += w7
        breakdown.append(ConfluenceItem(
            factor="Odrzucenie świecowe (Knot)",
            weight=5,
            achieved=has_rejection,
            detail=f"{wick_msg} (+{w7}%)"
        ))

        total_score = max(0, min(100, total_score))

        if total_score < self.min_confluence_score:
            return None

        grade = "A+" if total_score >= 85 else ("A" if total_score >= 70 else "B")

        return TradeSetup(
            symbol=symbol,
            direction=direction,
            timeframe=LTF_TIMEFRAME,
            setup_type=f"{LTF_TIMEFRAME} {event_name} + Fibo >= 61.8% Pullback (OTE)",
            entry_price=round(entry, digits),
            sl_price=round(sl, digits),
            tp1_price=round(tp1, digits),
            tp2_price=round(tp2, digits),
            rr_ratio=actual_rr,
            htf_trend=htf_trend,
            zone_top=round(zone_top, digits),
            zone_bottom=round(zone_bottom, digits),
            reason=f"M15 {event_name} + Fibo OTE ({total_score}% Confluence [{grade}]). {liq_eval.summary_msg}.",
            timestamp=current_time,
            fibo_50=round(fibo_50, digits),
            fibo_618=round(fibo_618, digits),
            fibo_786=round(fibo_786, digits),
            impulse_high=round(impulse_high, digits),
            impulse_low=round(impulse_low, digits),
            choch_level=round(broken_level, digits),
            confluence_score=total_score,
            grade=grade,
            confluence_breakdown=breakdown,
            session_name=session_info["session_name"],
            is_killzone=session_info["is_killzone"],
            news_warning=news_warn,
            is_optimal_rr=is_optimal_rr,
            rr_source_info=rr_source,
            spread_used=spread_buffer,
            sl_mode=applied_sl_mode,
            swept_levels=[
                {
                    "name": s.name,
                    "price": round(s.price, digits),
                    "level_type": s.level_type,
                    "bars_ago": s.bars_ago,
                    "description": s.description,
                }
                for s in liq_eval.swept_levels
            ],
            is_multi_sweep=liq_eval.is_multi_sweep,
            has_htf_sweep=liq_eval.has_htf_sweep,
            liquidity_summary=liq_eval.summary_msg,
            regime=regime,
            dynamic_be_trigger=dynamic_be,
            is_adaptive_tp=is_optimal_rr,
            strong_level=round(strong_level, digits),
            weak_level=round(weak_level, digits),
            is_sweep_engineered=is_sweep_ob,
            htf_barrier_price=round(htf_barrier_price, digits) if htf_barrier_price > 0 else 0.0,
            htf_barrier_dist_r=htf_barrier_dist_r,
        )
