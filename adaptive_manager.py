"""
adaptive_manager.py
Adaptive Position & Risk Management Engine for Forex-SMC.
Provides:
- MarketRegime classification (STRONG_TREND, COMPRESSION, HIGH_VOLATILITY)
- Dynamic Break-Even trigger calibration per regime
- Structural M15 Trailing SL with ATR buffer and broker stops_level verification
- Safe pyramiding qualification gate
- Telemetry & execution for SHADOW vs LIVE modes
"""
from collections import deque
from enum import Enum
import logging
from typing import Any, Dict, List, Optional, Tuple

from config import (
    ADAPTIVE_BE_CHOP_R,
    ADAPTIVE_BE_TREND_R,
    ADAPTIVE_BE_VOLATILE_R,
    ADAPTIVE_M15_TRAILING,
    ADAPTIVE_MIN_REGIME_SAMPLES,
    ADAPTIVE_MODE,
    ADAPTIVE_TRAILING_ATR_MULT,
    ADAPTIVE_TRAILING_MIN_STEP_R,
    ENABLE_ADAPTIVE_MANAGER,
)

logger = logging.getLogger("ForexSMC.AdaptiveManager")


class MarketRegime(str, Enum):
    STRONG_TREND = "STRONG_TREND"
    COMPRESSION = "COMPRESSION"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


class AdaptivePositionManager:
    """
    Two-Tier Adaptive Position Management:
    - Tier 1: Macro Market Regime Classification & Regime-Specific Targets
    - Tier 2: Micro Execution (Dynamic BE, Structural M15 Trailing SL, Scale-In Gate)
    Supports 'SHADOW' mode (telemetry logging without changing live MT5 orders) and 'LIVE' mode.
    """

    def __init__(self, journal=None, mt5_client=None):
        self.journal = journal
        self.mt5_client = mt5_client
        self.mode = ADAPTIVE_MODE.upper() if isinstance(ADAPTIVE_MODE, str) else "SHADOW"
        self.enabled = ENABLE_ADAPTIVE_MANAGER
        self.min_regime_samples = ADAPTIVE_MIN_REGIME_SAMPLES
        self._shadow_logs: deque = deque(maxlen=100)

    def classify_regime(
        self,
        htf_trend: str,
        ltf_trend: Optional[str],
        atr_percentile: float = 50.0,
        is_news_window: bool = False,
    ) -> MarketRegime:
        """
        Classifies current market into one of three macro regimes:
        1. HIGH_VOLATILITY: if news window is active or ATR percentile > 85.0
        2. STRONG_TREND: if HTF (H4) and LTF (M15) directions align and ATR is within normal expansion range (40-85 percentile)
        3. COMPRESSION: otherwise (choppy, low volume or opposing timeframes)
        """
        atr_val = float(atr_percentile if atr_percentile is not None else 50.0)
        if is_news_window or atr_val > 85.0:
            return MarketRegime.HIGH_VOLATILITY

        htf = (htf_trend or "").upper().strip()
        ltf = (ltf_trend or "").upper().strip()

        # Normalize direction representations (BUY -> BULLISH, SELL -> BEARISH)
        dir_map = {"BUY": "BULLISH", "LONG": "BULLISH", "SELL": "BEARISH", "SHORT": "BEARISH"}
        htf = dir_map.get(htf, htf)
        ltf = dir_map.get(ltf, ltf)

        if htf in ("BULLISH", "BEARISH") and ltf in ("BULLISH", "BEARISH"):
            if htf == ltf and 40.0 <= atr_val <= 85.0:
                return MarketRegime.STRONG_TREND

        return MarketRegime.COMPRESSION

    def get_dynamic_be_trigger(self, regime: MarketRegime) -> float:
        """
        Returns dynamic Break-Even trigger R:
        - STRONG_TREND: larger buffer (e.g. 1.15R) to allow healthy pullback/retest
        - COMPRESSION: tighter protection (e.g. 0.80R) to quickly lock safety in chop
        - HIGH_VOLATILITY: standard or conservative (e.g. 0.85R)
        """
        if regime == MarketRegime.STRONG_TREND:
            return ADAPTIVE_BE_TREND_R
        elif regime == MarketRegime.COMPRESSION:
            return ADAPTIVE_BE_CHOP_R
        return ADAPTIVE_BE_VOLATILE_R

    def can_pyramid(self, regime: MarketRegime) -> bool:
        """
        Risk-neutral scale-in is strictly permitted only in STRONG_TREND regime.
        """
        return regime == MarketRegime.STRONG_TREND

    def evaluate_m15_trailing_sl(
        self,
        symbol: str,
        is_buy: bool,
        current_sl: float,
        current_price: float,
        entry_price: float,
        initial_sl_dist: float,
        m15_swing_price: float,
        atr_m15: float,
        stops_level_points: float = 0.0,
        point_scale: float = 0.0001,
        spread: float = 0.0,
    ) -> Tuple[bool, float, str]:
        """
        Evaluates structural trailing behind M15 protected swing with strict broker protection:
        - Position must be in profit (>= +1.0R) and already protected at BE.
        - Proposed SL must improve current SL by at least ADAPTIVE_TRAILING_MIN_STEP_R (default 0.40R).
        - Must maintain distance >= max(ADAPTIVE_TRAILING_ATR_MULT * ATR_M15, 2.5 * stops_level) from current_price.
        - Includes spread buffer to prevent premature wick-outs.
        Returns: (should_trail, new_sl, reason)
        """
        if not ADAPTIVE_M15_TRAILING:
            return False, 0.0, "M15 trailing disabled in config"

        if initial_sl_dist <= 0 or current_price <= 0 or m15_swing_price <= 0:
            return False, 0.0, "Invalid price references"

        min_broker_dist = max(stops_level_points * point_scale * 2.5, point_scale * 10)
        min_safe_dist = max(atr_m15 * ADAPTIVE_TRAILING_ATR_MULT, min_broker_dist)
        spread_buffer = spread if spread > 0 else (point_scale * 5)

        if is_buy:
            # Trailing for BUY: candidate SL is placed below swing low minus spread buffer
            candidate_sl = m15_swing_price - spread_buffer

            if candidate_sl <= entry_price:
                return False, 0.0, "Candidate SL does not lock profit above entry"

            improvement = candidate_sl - current_sl
            min_step = initial_sl_dist * ADAPTIVE_TRAILING_MIN_STEP_R
            if improvement < min_step:
                return False, 0.0, f"SL improvement too small (+{improvement / initial_sl_dist:.2f}R < {ADAPTIVE_TRAILING_MIN_STEP_R:.2f}R)"

            dist_to_market = current_price - candidate_sl
            if dist_to_market < min_safe_dist:
                return False, 0.0, f"Candidate SL too close to market ({dist_to_market:.5f} < min safe {min_safe_dist:.5f})"

            locked_r = (candidate_sl - entry_price) / initial_sl_dist
            return True, candidate_sl, f"M15 Structural Trail locks +{locked_r:.2f}R"
        else:
            # Trailing for SELL: candidate SL is placed above swing high plus spread buffer
            candidate_sl = m15_swing_price + spread_buffer

            if candidate_sl >= entry_price:
                return False, 0.0, "Candidate SL does not lock profit above entry"

            improvement = current_sl - candidate_sl
            min_step = initial_sl_dist * ADAPTIVE_TRAILING_MIN_STEP_R
            if improvement < min_step:
                return False, 0.0, f"SL improvement too small (+{improvement / initial_sl_dist:.2f}R < {ADAPTIVE_TRAILING_MIN_STEP_R:.2f}R)"

            dist_to_market = candidate_sl - current_price
            if dist_to_market < min_safe_dist:
                return False, 0.0, f"Candidate SL too close to market ({dist_to_market:.5f} < min safe {min_safe_dist:.5f})"

            locked_r = (entry_price - candidate_sl) / initial_sl_dist
            return True, candidate_sl, f"M15 Structural Trail locks +{locked_r:.2f}R"

    def get_optimal_tp(
        self,
        symbol: str,
        regime: MarketRegime,
        default_tp_r: float = 2.0,
    ) -> Tuple[float, bool, str]:
        """
        Determines the optimal Take Profit target (in R) dynamically based on regime statistics.
        Falls back gracefully if journal has insufficient sample size.
        """
        if not self.enabled or not self.journal:
            return default_tp_r, False, f"Default TP (1:{default_tp_r:.1f})"

        from rr_optimizer import RROptimizer

        be_trigger = self.get_dynamic_be_trigger(regime)
        return RROptimizer.get_effective_rr(
            journal=self.journal,
            default_rr=default_tp_r,
            use_optimal_rr=True,
            min_trades=self.min_regime_samples,
            regime=regime.value,
            be_trigger_r=be_trigger,
        )

    def log_shadow_action(self, ticket: int, action: str, details: str):
        """Records a shadow-mode telemetry event for offline analysis."""
        entry = {
            "ticket": ticket,
            "action": action,
            "details": details,
        }
        self._shadow_logs.append(entry)
        logger.info(f"🕶️ [SHADOW ADAPTIVE] #{ticket}: {action} | {details}")
