from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from smartmoneyconcepts import smc

from news_filter import NewsFilter
from rr_optimizer import RROptimizer
from session_manager import SessionManager
from trade_journal import TradeJournal
from config import DEFAULT_RR_RATIO, MIN_TRADES_FOR_OPTIMAL_RR, USE_OPTIMAL_RR


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
    ):
        self.swing_length = swing_length
        self.default_rr = default_rr
        self.min_confluence_score = min_confluence_score
        self.strict_htf_alignment = strict_htf_alignment
        self.use_optimal_rr = use_optimal_rr
        self.min_trades_for_optimal = min_trades_for_optimal
        self.news_filter = NewsFilter()
        self.journal = TradeJournal()

    def get_effective_rr(self) -> Tuple[float, bool, str]:
        """
        Determines the target Risk:Reward dynamically:
        If enough trades exist (>= min_trades_for_optimal), uses mathematically
        optimized BEST RR from the journal. Otherwise falls back to default_rr from .env.
        """
        if self.use_optimal_rr:
            trades = self.journal.get_all_trades()
            if len(trades) >= self.min_trades_for_optimal:
                opt_res = RROptimizer.calculate_best_rr(trades)
                if opt_res and opt_res.best_rr >= 1.0:
                    return (
                        opt_res.best_rr,
                        True,
                        f"Zoptymalizowany Best RR (1:{opt_res.best_rr:.1f} z {len(trades)} zagrań)"
                    )
        return (
            self.default_rr,
            False,
            f"Domyślny RR z .env (1:{self.default_rr:.1f})"
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

                if choch_dir == 1:
                    # ==========================================
                    # BULLISH SETUP (BUY)
                    # ==========================================
                    trend_ok = (not self.strict_htf_alignment) or (htf_trend in ["BULLISH", "NEUTRAL"])
                    if not trend_ok:
                        continue

                    impulse_low = leg_df["low"].min()
                    impulse_high = leg_df["high"].max()
                    impulse_range = impulse_high - impulse_low
                    if impulse_range <= (point_scale * 10):
                        continue

                    fibo_50 = impulse_high - (0.500 * impulse_range)
                    fibo_618 = impulse_high - (0.618 * impulse_range)
                    fibo_705 = impulse_high - (0.705 * impulse_range)
                    fibo_786 = impulse_high - (0.786 * impulse_range)

                    # Condition: Pullback >= 61.8%
                    if (current_price <= fibo_618 + buffer) and (current_price >= impulse_low - buffer):
                        entry = current_price
                        sl = impulse_low - buffer
                        risk = entry - sl
                        if risk <= (point_scale * 5):
                            continue

                        # TP1 calculated according to adaptive target_rr (e.g. 1.8R or default 2.0R)
                        tp1 = entry + (risk * target_rr)
                        # TP2 targets the structural swing high (or further expansion)
                        tp2 = max(impulse_high, entry + (risk * (target_rr + 1.0)))
                        actual_rr = round(target_rr, 1)

                        # CONFLUENCE WEIGHTS CALCULATION (0 - 100%)
                        breakdown: List[ConfluenceItem] = []
                        total_score = 0

                        # 1. H4 Trend Alignment (20%)
                        h4_match = htf_trend == "BULLISH"
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
                        breakdown.append(ConfluenceItem(
                            factor="Potwierdzenie M15 CHoCH",
                            weight=20,
                            achieved=True,
                            detail=f"Wybicie szczytu {broken_level:.{digits}f} (+20%)"
                        ))

                        # 3. Fibonacci Retracement Depth (20%)
                        is_deep_ote = current_price <= fibo_705
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
                        has_ob, ob_detail = self._check_order_block_overlap(df_ltf, swings_ltf, fibo_786, fibo_618, "BUY")
                        w4 = 15 if has_ob else 0
                        total_score += w4
                        breakdown.append(ConfluenceItem(
                            factor="Zbieżność z Order Block / FVG",
                            weight=15,
                            achieved=has_ob,
                            detail=f"{ob_detail} (+{w4}%)"
                        ))

                        # 5. Session Liquidity Sweep (15%)
                        has_sweep, sweep_msg = SessionManager.check_asian_sweep(df_ltf, "BUY")
                        w5 = 15 if has_sweep else 0
                        total_score += w5
                        breakdown.append(ConfluenceItem(
                            factor="Wybicie płynności (Sweep)",
                            weight=15,
                            achieved=has_sweep,
                            detail=f"{sweep_msg} (+{w5}%)"
                        ))

                        # 6. Candlestick Wick Rejection (10%)
                        has_rejection, wick_msg = self._check_candle_rejection(current_candle, "BUY")
                        w6 = 10 if has_rejection else 0
                        total_score += w6
                        breakdown.append(ConfluenceItem(
                            factor="Odrzucenie świecowe (Knot)",
                            weight=10,
                            achieved=has_rejection,
                            detail=f"{wick_msg} (+{w6}%)"
                        ))

                        # Filter by minimum score threshold
                        if total_score < self.min_confluence_score:
                            continue

                        grade = "A+" if total_score >= 85 else ("A" if total_score >= 70 else "B")

                        setup = TradeSetup(
                            symbol=symbol,
                            direction="BUY",
                            timeframe="M15",
                            setup_type="M15 CHoCH + Fibo >= 61.8% Pullback (OTE)",
                            entry_price=round(entry, digits),
                            sl_price=round(sl, digits),
                            tp1_price=round(tp1, digits),
                            tp2_price=round(tp2, digits),
                            rr_ratio=actual_rr,
                            htf_trend=htf_trend,
                            zone_top=round(fibo_618, digits),
                            zone_bottom=round(fibo_786, digits),
                            reason=f"M15 CHoCH + Fibo OTE ({total_score}% Confluence [{grade}]). {sweep_msg}.",
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
                        )
                        setups.append(setup)
                        break

                elif choch_dir == -1:
                    # ==========================================
                    # BEARISH SETUP (SELL)
                    # ==========================================
                    trend_ok = (not self.strict_htf_alignment) or (htf_trend in ["BEARISH", "NEUTRAL"])
                    if not trend_ok:
                        continue

                    impulse_high = leg_df["high"].max()
                    impulse_low = leg_df["low"].min()
                    impulse_range = impulse_high - impulse_low
                    if impulse_range <= (point_scale * 10):
                        continue

                    fibo_50 = impulse_low + (0.500 * impulse_range)
                    fibo_618 = impulse_low + (0.618 * impulse_range)
                    fibo_705 = impulse_low + (0.705 * impulse_range)
                    fibo_786 = impulse_low + (0.786 * impulse_range)

                    # Condition: Pullback >= 61.8%
                    if (current_price >= fibo_618 - buffer) and (current_price <= impulse_high + buffer):
                        entry = current_price
                        sl = impulse_high + buffer
                        risk = sl - entry
                        if risk <= (point_scale * 5):
                            continue

                        # TP1 calculated according to adaptive target_rr (e.g. 1.8R or default 2.0R)
                        tp1 = entry - (risk * target_rr)
                        # TP2 targets the structural swing low (or further expansion)
                        tp2 = min(impulse_low, entry - (risk * (target_rr + 1.0)))
                        actual_rr = round(target_rr, 1)

                        # CONFLUENCE WEIGHTS CALCULATION
                        breakdown: List[ConfluenceItem] = []
                        total_score = 0

                        # 1. H4 Trend (20%)
                        h4_match = htf_trend == "BEARISH"
                        w1 = 20 if h4_match else (10 if htf_trend == "NEUTRAL" else 0)
                        total_score += w1
                        breakdown.append(ConfluenceItem(
                            factor="Zgodność z trendem H4",
                            weight=20,
                            achieved=h4_match,
                            detail=f"H4: {htf_trend} (+{w1}%)"
                        ))

                        # 2. M15 CHoCH (20%)
                        total_score += 20
                        breakdown.append(ConfluenceItem(
                            factor="Potwierdzenie M15 CHoCH",
                            weight=20,
                            achieved=True,
                            detail=f"Wybicie dołka {broken_level:.{digits}f} (+20%)"
                        ))

                        # 3. Fibo OTE Depth (20%)
                        is_deep_ote = current_price >= fibo_705
                        w3 = 20 if is_deep_ote else 15
                        total_score += w3
                        fibo_desc = "Głębokie OTE (70.5% - 78.6%)" if is_deep_ote else "Golden Pocket (61.8%)"
                        breakdown.append(ConfluenceItem(
                            factor="Poziom Fibonacciego",
                            weight=20,
                            achieved=True,
                            detail=f"{fibo_desc} (+{w3}%)"
                        ))

                        # 4. Order Block / FVG (15%)
                        has_ob, ob_detail = self._check_order_block_overlap(df_ltf, swings_ltf, fibo_618, fibo_786, "SELL")
                        w4 = 15 if has_ob else 0
                        total_score += w4
                        breakdown.append(ConfluenceItem(
                            factor="Zbieżność z Order Block / FVG",
                            weight=15,
                            achieved=has_ob,
                            detail=f"{ob_detail} (+{w4}%)"
                        ))

                        # 5. Session Liquidity Sweep (15%)
                        has_sweep, sweep_msg = SessionManager.check_asian_sweep(df_ltf, "SELL")
                        w5 = 15 if has_sweep else 0
                        total_score += w5
                        breakdown.append(ConfluenceItem(
                            factor="Wybicie płynności (Sweep)",
                            weight=15,
                            achieved=has_sweep,
                            detail=f"{sweep_msg} (+{w5}%)"
                        ))

                        # 6. Candle Rejection (10%)
                        has_rejection, wick_msg = self._check_candle_rejection(current_candle, "SELL")
                        w6 = 10 if has_rejection else 0
                        total_score += w6
                        breakdown.append(ConfluenceItem(
                            factor="Odrzucenie świecowe (Knot)",
                            weight=10,
                            achieved=has_rejection,
                            detail=f"{wick_msg} (+{w6}%)"
                        ))

                        if total_score < self.min_confluence_score:
                            continue

                        grade = "A+" if total_score >= 85 else ("A" if total_score >= 70 else "B")

                        setup = TradeSetup(
                            symbol=symbol,
                            direction="SELL",
                            timeframe="M15",
                            setup_type="M15 CHoCH + Fibo >= 61.8% Pullback (OTE)",
                            entry_price=round(entry, digits),
                            sl_price=round(sl, digits),
                            tp1_price=round(tp1, digits),
                            tp2_price=round(tp2, digits),
                            rr_ratio=actual_rr,
                            htf_trend=htf_trend,
                            zone_top=round(fibo_786, digits),
                            zone_bottom=round(fibo_618, digits),
                            reason=f"M15 CHoCH + Fibo OTE ({total_score}% Confluence [{grade}]). {sweep_msg}.",
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
                        )
                        setups.append(setup)
                        break

        return setups
