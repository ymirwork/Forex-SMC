from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import pandas as pd

from config import MIN_TRADES_FOR_OPTIMAL_RR, OPTIMAL_RR_MIN_FLOOR


@dataclass
class RROptimizerResult:
    """Stores the optimal RR simulation findings."""
    total_trades: int
    current_realized_r: float
    current_winrate: float
    best_rr: float
    best_net_r: float
    best_winrate: float
    best_profit_factor: float
    simulation_table: pd.DataFrame
    best_ev: float = 0.0


class RROptimizer:
    """
    Quantitative Risk:Reward Optimizer (MFE - Maximum Favorable Excursion).
    Simulates counter-factual Take Profit targets to find the mathematically
    optimal Risk:Reward ratio that maximizes total account growth.
    """

    @staticmethod
    def get_effective_rr(
        journal,
        default_rr: float,
        use_optimal_rr: bool = True,
        min_trades: Optional[int] = None,
        min_floor_rr: Optional[float] = None,
        regime: Optional[str] = None,
        be_trigger_r: float = 0.85,
    ) -> Tuple[float, bool, str]:
        """
        Determines the target Risk:Reward dynamically:
        1. If regime is provided and enough regime trades exist, uses regime-optimized BEST RR.
        2. Otherwise falls back to global BEST RR if enough total trades exist.
        3. Otherwise falls back to default_rr.
        Guarantees that optimal RR is never below min_floor_rr (default: 2.0).
        """
        required_trades = min_trades if min_trades is not None else MIN_TRADES_FOR_OPTIMAL_RR
        floor_rr = min_floor_rr if min_floor_rr is not None else OPTIMAL_RR_MIN_FLOOR

        if use_optimal_rr and journal is not None:
            trades = [t for t in journal.get_all_trades() if t.get("status") in ("CLOSED", "WIN", "LOSS", "BE")]

            # 1. Try regime-specific optimization first if regime provided
            if regime:
                reg_res = RROptimizer.calculate_regime_rr(
                    trades,
                    regime=regime,
                    min_rr=floor_rr,
                    be_trigger_r=be_trigger_r,
                    min_trades=min_trades if min_trades is not None else 10,
                )
                if reg_res and reg_res.best_rr >= floor_rr:
                    return (
                        reg_res.best_rr,
                        True,
                        f"Zoptymalizowany Best RR dla reżimu [{regime.upper()}] (1:{reg_res.best_rr:.1f} z {reg_res.total_trades} zagrań, EV: {reg_res.best_ev:+.2f}R)",
                    )

            # 2. Global optimization fallback
            if len(trades) >= required_trades:
                opt_res = RROptimizer.calculate_best_rr(trades, min_rr=floor_rr, be_trigger_r=be_trigger_r)
                if opt_res and opt_res.best_rr >= floor_rr:
                    return (
                        opt_res.best_rr,
                        True,
                        f"Zoptymalizowany Best RR (1:{opt_res.best_rr:.1f} z {len(trades)} zagrań, EV: {opt_res.best_ev:+.2f}R)",
                    )
        return (
            default_rr,
            False,
            f"Domyślny RR z .env (1:{default_rr:.1f})",
        )

    @staticmethod
    def calculate_regime_rr(
        trades: List[Dict],
        regime: str,
        min_rr: float = OPTIMAL_RR_MIN_FLOOR,
        max_rr: float = 6.0,
        step: float = 0.1,
        be_trigger_r: float = 0.85,
        min_trades: int = 10,
    ) -> Optional[RROptimizerResult]:
        """
        Calculates optimal RR specifically for a market regime (STRONG_TREND, COMPRESSION, HIGH_VOLATILITY).
        Requires at least min_trades in that regime.
        """
        if not trades or not regime:
            return None
        regime_upper = regime.upper().strip()
        regime_trades = [
            t for t in trades
            if str(t.get("regime", "")).upper().strip() == regime_upper
        ]
        if len(regime_trades) < min_trades:
            return None
        return RROptimizer.calculate_best_rr(
            regime_trades,
            min_rr=min_rr,
            max_rr=max_rr,
            step=step,
            be_trigger_r=be_trigger_r,
        )

    @staticmethod
    def calculate_best_rr(
        trades: List[Dict],
        min_rr: float = OPTIMAL_RR_MIN_FLOOR,
        max_rr: float = 8.0,
        step: float = 0.1,
        be_trigger_r: float = 0.85,
    ) -> Optional[RROptimizerResult]:
        """
        Runs counter-factual simulation across candidate RR targets from min_rr to max_rr.
        Uses multi-outcome empirical simulation:
        - If peak MFE >= target_rr: WIN (+target_rr)
        - If peak MFE < target_rr, but >= be_trigger_r: BREAK-EVEN (0.0R, defended, no dollar loss)
        - If peak MFE < be_trigger_r: LOSS (-1.0R, stopped out at initial SL)
        """
        if not trades:
            return None

        # Filter out OPEN trades so unclosed positions don't skew the win rate and simulation
        valid_trades = [t for t in trades if t.get("status") in ("CLOSED", "WIN", "LOSS", "BE")]
        if not valid_trades:
            valid_trades = [t for t in trades if t.get("status") != "OPEN"]

        if len(valid_trades) < 2:
            return None

        trades = valid_trades
        total_trades = len(trades)
        actual_realized_r = sum(t.get("realized_r", 0.0) for t in trades)
        actual_wins = sum(1 for t in trades if t.get("realized_r", 0.0) > 0)
        actual_winrate = actual_wins / total_trades if total_trades > 0 else 0.0

        results = []
        curr_rr = min_rr

        # Iterate candidate RR targets
        while curr_rr <= max_rr + 0.001:
            target_rr = round(curr_rr, 1)

            sim_wins = 0
            sim_bes = 0
            sim_losses = 0

            for t in trades:
                # Use counterfactual Max R if available, falling back to recorded Max R
                eff_mfe = max(float(t.get("counterfactual_max_r") or 0.0), float(t.get("max_r") or 0.0))
                if eff_mfe >= target_rr:
                    sim_wins += 1
                elif be_trigger_r > 0 and eff_mfe >= be_trigger_r:
                    # Target not hit, but price passed Break-Even trigger -> protected at BE (0.0R outcome)
                    sim_bes += 1
                else:
                    # Stopped out at initial SL (-1.0R outcome)
                    sim_losses += 1

            gross_gain = sim_wins * target_rr
            gross_loss = sim_losses * 1.0
            net_r = round(gross_gain - gross_loss, 2)
            ev = round(net_r / total_trades, 3) if total_trades > 0 else 0.0
            winrate = round((sim_wins / total_trades) * 100, 1) if total_trades > 0 else 0.0
            pf = round(gross_gain / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_gain > 0 else 0.0)

            results.append({
                "target_rr": target_rr,
                "net_r": net_r,
                "ev": ev,
                "winrate": winrate,
                "profit_factor": pf,
                "wins": sim_wins,
                "bes": sim_bes,
                "losses": sim_losses,
            })

            curr_rr += step

        df_sim = pd.DataFrame(results)
        if df_sim.empty:
            return None

        # Find candidate with maximum Net R (tie-breaker: higher Profit Factor)
        best_row = df_sim.sort_values(by=["net_r", "profit_factor"], ascending=[False, False]).iloc[0]

        return RROptimizerResult(
            total_trades=total_trades,
            current_realized_r=round(actual_realized_r, 2),
            current_winrate=round(actual_winrate * 100, 1),
            best_rr=best_row["target_rr"],
            best_net_r=best_row["net_r"],
            best_winrate=best_row["winrate"],
            best_profit_factor=best_row["profit_factor"],
            simulation_table=df_sim,
            best_ev=best_row.get("ev", round(best_row["net_r"] / total_trades, 3)),
        )

    @staticmethod
    def format_optimizer_report(opt: RROptimizerResult) -> str:
        """Formats an easy-to-read, actionable Markdown report for Telegram."""
        if not opt:
            return "⚠️ Zbyt mało zamkniętych transakcji w bazie do przeprowadzenia optymalizacji (wymagane min. 2)."

        diff_r = opt.best_net_r - opt.current_realized_r
        diff_str = f"+{diff_r:.1f}R więcej" if diff_r >= 0 else f"{diff_r:.1f}R"

        # Key benchmark levels to show in summary table
        benchmark_rrs = [1.0, 1.5, 2.0, opt.best_rr, 3.0, 4.0, 5.0]
        # Remove duplicates and sort
        benchmark_rrs = sorted(list(set(benchmark_rrs)))

        table_lines = "🎯 `Target RR | Skuteczność | Net R  | PF   `\n"
        table_lines += "─" * 42 + "\n"

        for r_val in benchmark_rrs:
            match = opt.simulation_table[opt.simulation_table["target_rr"] == round(r_val, 1)]
            if not match.empty:
                row = match.iloc[0]
                is_best = (row["target_rr"] == opt.best_rr)
                star = "⭐" if is_best else "  "
                best_label = " 🏆" if is_best else ""
                table_lines += (
                    f"{star} `1:{row['target_rr']:<4.1f} | {row['winrate']:>4.1f}%     | {row['net_r']:>+5.1f}R | {row['profit_factor']:<4.2f}`{best_label}\n"
                )

        msg = (
            f"🎯 **OPTYMALIZATOR STRATEGII (BEST RR)**\n\n"
            f"📊 **Analiza Twoich zagrań (próba: {opt.total_trades} pozycji):**\n"
            f"• Aktualnie zrealizowany zysk: `{opt.current_realized_r:+.2f}R` (Skuteczność: `{opt.current_winrate}%`)\n\n"
            f"🏆 **TWÓJ MATEMATYCZNY BEST RR: 1 : {opt.best_rr:.1f}**\n"
            f"• Szacowany zysk przy sztywnym 1:{opt.best_rr:.1f}: `+{opt.best_net_r:.1f}R` ({diff_str}!)\n"
            f"• Skuteczność przy tym RR: `{opt.best_winrate}%`\n"
            f"• Profit Factor przy tym RR: `{opt.best_profit_factor:.2f}`\n\n"
            f"📈 **Symulacja różnych poziomów Take Profit:**\n"
            f"{table_lines}\n"
            f"💡 **Wniosek:** Ustawienie stałego TP na **1:{opt.best_rr:.1f}** maksymalizuje zysk Twojego edge'a. "
            f"Zamykanie pozycji zbyt wcześnie pozostawia znaczną część potencjału na rynku!"
        )
        return msg
