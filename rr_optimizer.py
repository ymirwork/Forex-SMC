from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import pandas as pd


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
        min_trades: int = 10,
    ) -> Tuple[float, bool, str]:
        """
        Determines the target Risk:Reward dynamically:
        If enough trades exist (>= min_trades), uses mathematically
        optimized BEST RR from the journal. Otherwise falls back to default_rr.
        """
        if use_optimal_rr and journal is not None:
            trades = [t for t in journal.get_all_trades() if t.get("status") in ("CLOSED", "WIN", "LOSS", "BE")]
            if len(trades) >= min_trades:
                opt_res = RROptimizer.calculate_best_rr(trades)
                if opt_res and opt_res.best_rr >= 1.0:
                    return (
                        opt_res.best_rr,
                        True,
                        f"Zoptymalizowany Best RR (1:{opt_res.best_rr:.1f} z {len(trades)} zagrań)",
                    )
        return (
            default_rr,
            False,
            f"Domyślny RR z .env (1:{default_rr:.1f})",
        )

    @staticmethod
    def calculate_best_rr(
        trades: List[Dict],
        min_rr: float = 1.0,
        max_rr: float = 8.0,
        step: float = 0.1,
    ) -> Optional[RROptimizerResult]:
        """
        Runs counter-factual simulation across candidate RR targets from min_rr to max_rr.
        Each trade dict must have: 'max_r' (float), and optionally 'realized_r' (float).
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
            sim_losses = 0

            for t in trades:
                max_r = float(t.get("max_r", 0.0))
                # If market ran at least up to target_rr, target would be hit!
                if max_r >= target_rr:
                    sim_wins += 1
                else:
                    # Target was not reached, price eventually stopped out at initial SL (-1R)
                    sim_losses += 1

            gross_gain = sim_wins * target_rr
            gross_loss = sim_losses * 1.0
            net_r = round(gross_gain - gross_loss, 2)
            winrate = round((sim_wins / total_trades) * 100, 1) if total_trades > 0 else 0.0
            pf = round(gross_gain / gross_loss, 2) if gross_loss > 0 else 99.0

            results.append({
                "target_rr": target_rr,
                "net_r": net_r,
                "winrate": winrate,
                "profit_factor": pf,
                "wins": sim_wins,
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
