from dataclasses import dataclass, field
from datetime import datetime
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
    DEFAULT_RR_RATIO,
    LTF_TIMEFRAME,
    MIN_TRADES_FOR_OPTIMAL_RR,
    SL_ATR_MULTIPLIER,
    SL_CALCULATION_MODE,
    SL_HYBRID_MIN_ATR_MULT,
    TIGHT_SL_MODE,
    USE_OPTIMAL_RR,
)


def calculate_stop_loss(
    entry: float,
    structural_sl: float,
    atr: float,
    is_buy: bool,
    spread_buffer: float = 0.0,
    point_scale: float = 0.0001,
) -> Tuple[Optional[float], Optional[float], str]:
    """
    Calculates Stop Loss price and risk amount according to configured SL mode:
    - "STRUCTURE": Pure structural swing / OTE / EMA invalidation level.
    - "ATR": Entry +/- (ATR * multiplier + spread_buffer).
    - "HYBRID": Structural SL, but guarantees a minimum risk buffer of (ATR * hybrid_mult + spread_buffer).
    Returns (sl, risk, applied_sl_mode). Returns (None, None, applied_sl_mode) if risk <= 5 * point_scale.
    """
    atr_val = atr if (atr is not None and atr > 0) else 0.0
    atr_risk = atr_val * SL_ATR_MULTIPLIER

    if is_buy:
        structural_risk = entry - structural_sl
        if SL_CALCULATION_MODE == "ATR":
            sl = entry - (atr_risk + spread_buffer)
            applied_sl_mode = "ATR"
        elif SL_CALCULATION_MODE == "HYBRID":
            min_risk = (atr_val * SL_HYBRID_MIN_ATR_MULT) + spread_buffer
            final_risk = max(structural_risk, min_risk)
            sl = entry - final_risk
            applied_sl_mode = "HYBRID"
        else:
            sl = structural_sl
            applied_sl_mode = "STRUCTURE"

        risk = entry - sl
    else:
        structural_risk = structural_sl - entry
        if SL_CALCULATION_MODE == "ATR":
            sl = entry + (atr_risk + spread_buffer)
            applied_sl_mode = "ATR"
        elif SL_CALCULATION_MODE == "HYBRID":
            min_risk = (atr_val * SL_HYBRID_MIN_ATR_MULT) + spread_buffer
            final_risk = max(structural_risk, min_risk)
            sl = entry + final_risk
            applied_sl_mode = "HYBRID"
        else:
            sl = structural_sl
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

    def get_effective_rr(self) -> Tuple[float, bool, str]:
        """Determines target RR dynamically using shared RROptimizer logic."""
        return RROptimizer.get_effective_rr(
            journal=self.journal,
            default_rr=self.default_rr,
            use_optimal_rr=self.use_optimal_rr,
            min_trades=self.min_trades_for_optimal,
        )

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

    def _check_order_block_overlap(self, df: pd.DataFrame, swings: pd.DataFrame, z_bottom: float, z_top: float, direction: str) -> Tuple[bool, str]:
        """Checks if an unmitigated Order Block or FVG aligns with the entry zone."""
        try:
            ob_df = smc.ob(df, swings)
            recent_obs = ob_df.tail(30)
            target_val = 1 if direction == "BUY" else -1

            for _, row in recent_obs.iterrows():
                if row.get("OB") == target_val and row.get("MitigatedIndex", 0) == 0:
                    ob_top, ob_bottom = row["Top"], row["Bottom"]
                    # Check overlap
                    if not (ob_bottom > z_top or ob_top < z_bottom):
                        return True, f"Unmitigated {direction} OB [{ob_bottom:.2f} - {ob_top:.2f}]"
        except Exception:
            pass
        return False, "Brak strefy OB w tym rejonie"

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
        current_price = current_candle["close"]
        current_time = current_candle["time"]

        digits = 2 if "JPY" in symbol.upper() or "XAU" in symbol.upper() or "GOLD" in symbol.upper() else 4
        point_scale = 0.01 if digits == 2 else 0.0001
        atr = (df_ltf["high"] - df_ltf["low"]).tail(14).mean()
        buffer = max(atr * 0.2, point_scale * 5)

        # Session & News Context
        session_info = SessionManager.get_current_session(current_time)
        has_news, news_warn, _ = self.news_filter.check_news(symbol)

        # Dynamic / Adaptive Best RR Calculation
        target_rr, is_optimal_rr, rr_source = self.get_effective_rr()

        # M15 Swings & CHoCH
        try:
            swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=self.swing_length)
            bos_choch_ltf = smc.bos_choch(df_ltf, swings_ltf)
        except Exception:
            return setups

        choch_df = bos_choch_ltf[bos_choch_ltf["CHOCH"].notna() & (bos_choch_ltf["CHOCH"] != 0)]
        if not choch_df.empty:
            recent_choch = choch_df.tail(3)
            for choch_bar_idx, choch_row in recent_choch.iterrows():
                choch_dir = choch_row["CHOCH"]
                broken_level = choch_row.get("Level", 0.0)

                leg_start_idx = max(0, choch_bar_idx - 20)
                leg_df = df_ltf.iloc[leg_start_idx:]

                dir_str = "BUY" if choch_dir == 1 else ("SELL" if choch_dir == -1 else None)
                if not dir_str:
                    continue

                setup = self._build_smc_setup(
                    direction=dir_str,
                    symbol=symbol,
                    leg_df=leg_df,
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
                )
                if setup:
                    setups.append(setup)
                    break

        return setups

    def _build_smc_setup(
        self,
        direction: str,
        symbol: str,
        leg_df: pd.DataFrame,
        df_ltf: pd.DataFrame,
        swings_ltf: pd.DataFrame,
        current_candle: pd.Series,
        htf_trend: str,
        broken_level: float,
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
        df_d1: Optional[pd.DataFrame] = None,
        df_w1: Optional[pd.DataFrame] = None,
        df_h1: Optional[pd.DataFrame] = None,
    ) -> Optional[TradeSetup]:
        is_buy = direction == "BUY"
        expected_htf = "BULLISH" if is_buy else "BEARISH"
        trend_ok = (not self.strict_htf_alignment) or (htf_trend in [expected_htf, "NEUTRAL"])
        if not trend_ok:
            return None

        current_price = current_candle["close"]
        impulse_low = leg_df["low"].min()
        impulse_high = leg_df["high"].max()
        impulse_range = impulse_high - impulse_low
        if impulse_range <= (point_scale * 10):
            return None

        spread_buffer = spread if ADD_SPREAD_TO_SL else 0.0
        effective_buffer = buffer + spread_buffer
        atr = (df_ltf["high"] - df_ltf["low"]).tail(14).mean()

        if is_buy:
            fibo_50 = impulse_high - (0.500 * impulse_range)
            fibo_618 = impulse_high - (0.618 * impulse_range)
            fibo_705 = impulse_high - (0.705 * impulse_range)
            fibo_786 = impulse_high - (0.786 * impulse_range)
            if not ((current_price <= fibo_618 + buffer) and (current_price >= impulse_low - buffer)):
                return None
            entry = current_price
            if TIGHT_SL_MODE:
                recent_pullback_low = df_ltf.iloc[-5:]["low"].min()
                structural_sl = min(fibo_786, recent_pullback_low) - effective_buffer
            else:
                structural_sl = impulse_low - effective_buffer

            sl, risk, applied_sl_mode = calculate_stop_loss(
                entry=entry,
                structural_sl=structural_sl,
                atr=atr,
                is_buy=True,
                spread_buffer=spread_buffer,
                point_scale=point_scale,
            )
            if sl is None or risk is None:
                return None

            tp1 = entry + (risk * target_rr)
            tp2 = max(impulse_high, entry + (risk * (target_rr + 1.0)))
            zone_top = fibo_618
            zone_bottom = fibo_786
        else:
            fibo_50 = impulse_low + (0.500 * impulse_range)
            fibo_618 = impulse_low + (0.618 * impulse_range)
            fibo_705 = impulse_low + (0.705 * impulse_range)
            fibo_786 = impulse_low + (0.786 * impulse_range)
            if not ((current_price >= fibo_618 - buffer) and (current_price <= impulse_high + buffer)):
                return None
            entry = current_price
            if TIGHT_SL_MODE:
                recent_pullback_high = df_ltf.iloc[-5:]["high"].max()
                structural_sl = max(fibo_786, recent_pullback_high) + effective_buffer
            else:
                structural_sl = impulse_high + effective_buffer

            sl, risk, applied_sl_mode = calculate_stop_loss(
                entry=entry,
                structural_sl=structural_sl,
                atr=atr,
                is_buy=False,
                spread_buffer=spread_buffer,
                point_scale=point_scale,
            )
            if sl is None or risk is None:
                return None

            tp1 = entry - (risk * target_rr)
            tp2 = min(impulse_low, entry - (risk * (target_rr + 1.0)))
            zone_top = fibo_786
            zone_bottom = fibo_618

        actual_rr = round(target_rr, 1)
        breakdown: List[ConfluenceItem] = []
        total_score = 0

        # 1. H4 Trend Alignment (20%)
        h4_match = htf_trend == expected_htf
        w1 = 20 if h4_match else (10 if htf_trend == "NEUTRAL" else 0)
        total_score += w1
        breakdown.append(ConfluenceItem(
            factor="Zgodność z trendem H4",
            weight=20,
            achieved=h4_match,
            detail=f"H4: {htf_trend} (+{w1}%)"
        ))

        # 2. M15 CHoCH Confirmation (20%)
        total_score += 20
        choch_label = "szczytu" if is_buy else "dołka"
        breakdown.append(ConfluenceItem(
            factor="Potwierdzenie M15 CHoCH",
            weight=20,
            achieved=True,
            detail=f"Wybicie {choch_label} {broken_level:.{digits}f} (+20%)"
        ))

        # 3. Fibonacci Retracement Depth (20%)
        is_deep_ote = (current_price <= fibo_705) if is_buy else (current_price >= fibo_705)
        w3 = 20 if is_deep_ote else 15
        total_score += w3
        fibo_desc = "Głębokie OTE (70.5% - 78.6%)" if is_deep_ote else "Golden Pocket (61.8%)"
        breakdown.append(ConfluenceItem(
            factor="Poziom Fibonacciego",
            weight=20,
            achieved=True,
            detail=f"{fibo_desc} (+{w3}%)"
        ))

        # 4. Order Block / FVG Confluence (15%)
        ob_fibo_low = min(fibo_786, fibo_618)
        ob_fibo_high = max(fibo_786, fibo_618)
        has_ob, ob_detail = self._check_order_block_overlap(df_ltf, swings_ltf, ob_fibo_low, ob_fibo_high, direction)
        w4 = 15 if has_ob else 0
        total_score += w4
        breakdown.append(ConfluenceItem(
            factor="Zbieżność z Order Block / FVG",
            weight=15,
            achieved=has_ob,
            detail=f"{ob_detail} (+{w4}%)"
        ))

        # 5. Advanced Liquidity Sweep (15% base + 5% bonus for Multi-Sweep / HTF)
        liq_eval = self.liquidity_detector.evaluate_sweeps(
            df_ltf=df_ltf,
            direction=direction,
            df_d1=df_d1,
            df_w1=df_w1,
            df_h1=df_h1,
        )
        has_sweep = liq_eval.has_sweep
        w5 = liq_eval.confluence_weight
        bonus = liq_eval.bonus_weight
        total_score += (w5 + bonus)
        total_score = min(100, total_score)
        breakdown.append(ConfluenceItem(
            factor="Wybicie płynności (Sweep)",
            weight=15,
            achieved=has_sweep,
            detail=f"{liq_eval.summary_msg} (+{w5 + bonus}%)"
        ))

        # 6. Candlestick Wick Rejection (10%)
        has_rejection, wick_msg = self._check_candle_rejection(current_candle, direction)
        w6 = 10 if has_rejection else 0
        total_score += w6
        total_score = min(100, total_score)
        breakdown.append(ConfluenceItem(
            factor="Odrzucenie świecowe (Knot)",
            weight=10,
            achieved=has_rejection,
            detail=f"{wick_msg} (+{w6}%)"
        ))

        if total_score < self.min_confluence_score:
            return None

        grade = "A+" if total_score >= 85 else ("A" if total_score >= 70 else "B")

        return TradeSetup(
            symbol=symbol,
            direction=direction,
            timeframe=LTF_TIMEFRAME,
            setup_type=f"{LTF_TIMEFRAME} CHoCH + Fibo >= 61.8% Pullback (OTE)",
            entry_price=round(entry, digits),
            sl_price=round(sl, digits),
            tp1_price=round(tp1, digits),
            tp2_price=round(tp2, digits),
            rr_ratio=actual_rr,
            htf_trend=htf_trend,
            zone_top=round(zone_top, digits),
            zone_bottom=round(zone_bottom, digits),
            reason=f"M15 CHoCH + Fibo OTE ({total_score}% Confluence [{grade}]). {liq_eval.summary_msg}.",
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
        )
