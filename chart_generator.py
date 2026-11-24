import os
from typing import Optional
import matplotlib
matplotlib.use("Agg")  # Headless rendering
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import pandas as pd
from config import ZUMIKO_FAST_EMA, ZUMIKO_SLOW_EMA, ZUMIKO_TREND_EMA
from smc_detector import TradeSetup

plt.style.use("dark_background")


class ChartGenerator:
    """Generates professional dark-themed candlestick charts with SMC zone overlays and Fibonacci levels."""

    def __init__(self, output_dir: str = "charts"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def generate_setup_chart(
        self,
        df: pd.DataFrame,
        setup: TradeSetup,
        bars_count: int = 60,
    ) -> Optional[str]:
        """
        Plots candlestick chart with Fibonacci Retracement levels (50%, 61.8%, 78.6%),
        CHoCH level, Entry, SL, and TP targets.
        """
        if df is None or len(df) < 20:
            return None

        plot_df = df.tail(bars_count).copy().reset_index(drop=True)
        if plot_df.empty:
            return None

        fig, ax = plt.subplots(figsize=(13, 7.0), dpi=150)
        try:
            fig.patch.set_facecolor("#131722")
            ax.set_facecolor("#131722")

            # Plot candles
            width = 0.6
            up_color = "#089981"    # Green
            down_color = "#f23645"  # Red

            for i, row in plot_df.iterrows():
                o, h, l, c = row["open"], row["high"], row["low"], row["close"]
                color = up_color if c >= o else down_color
                ax.plot([i, i], [l, h], color=color, linewidth=1.2)
                lower_body = min(o, c)
                height = abs(c - o)
                if height == 0:
                    height = 0.0001
                rect = patches.Rectangle(
                    (i - width / 2, lower_body),
                    width,
                    height,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.5,
                )
                ax.add_patch(rect)

            # Draw Fibonacci OTE Retracement (Golden Pocket: 61.8% - 78.6%)
            if setup.fibo_618 > 0 and setup.fibo_786 > 0:
                f_top = max(setup.fibo_618, setup.fibo_786)
                f_bottom = min(setup.fibo_618, setup.fibo_786)
                f_height = f_top - f_bottom

                # Shaded Golden Pocket Zone
                gp_rect = patches.Rectangle(
                    (0, f_bottom),
                    len(plot_df) + 8,
                    f_height,
                    facecolor="#f59e0b",
                    alpha=0.20,
                    edgecolor="#f59e0b",
                    linestyle="--",
                    linewidth=1.2,
                    label="OTE Zone (61.8% - 78.6%)",
                )
                ax.add_patch(gp_rect)

                # Fibonacci horizontal lines
                if setup.fibo_50 > 0:
                    ax.axhline(setup.fibo_50, color="#9ca3af", linestyle=":", linewidth=1.0, alpha=0.7)
                    ax.text(len(plot_df), setup.fibo_50, f" 50.0% ({setup.fibo_50})", color="#9ca3af", va="center", fontsize=8)

                ax.axhline(setup.fibo_618, color="#f59e0b", linestyle="-.", linewidth=1.4)
                ax.text(len(plot_df), setup.fibo_618, f" 61.8% ({setup.fibo_618})", color="#f59e0b", va="center", fontweight="bold", fontsize=8)

                ax.axhline(setup.fibo_786, color="#d97706", linestyle="-.", linewidth=1.4)
                ax.text(len(plot_df), setup.fibo_786, f" 78.6% ({setup.fibo_786})", color="#d97706", va="center", fontweight="bold", fontsize=8)

            else:
                # Fallback zone if generic Order Block
                z_bottom = min(setup.zone_bottom, setup.zone_top)
                z_top = max(setup.zone_bottom, setup.zone_top)
                zone_color = "#089981" if setup.direction == "BUY" else "#f23645"
                zone_rect = patches.Rectangle(
                    (0, z_bottom),
                    len(plot_df) + 8,
                    z_top - z_bottom,
                    facecolor=zone_color,
                    alpha=0.18,
                    edgecolor=zone_color,
                    linestyle="--",
                    linewidth=1.2,
                )
                ax.add_patch(zone_rect)
                lbl = "[SWAP ZONE]" if "SWAP" in getattr(setup, "setup_type", "").upper() else "Order Block"
                ax.text(2, z_top, f" {lbl} [{z_bottom:.4f} - {z_top:.4f}]", color=zone_color, va="bottom", fontsize=8, fontweight="bold")

            # Draw CHoCH / BOS line if present
            if setup.choch_level > 0:
                ax.axhline(setup.choch_level, color="#ec4899", linestyle="--", linewidth=1.2, alpha=0.8)
                ev_label = "M15 BOS" if "BOS" in getattr(setup, "setup_type", "").upper() else "M15 CHoCH"
                ax.text(2, setup.choch_level, f" {ev_label} Level ({setup.choch_level})", color="#ec4899", va="bottom", fontsize=8, fontweight="bold")

            # Draw Swept Liquidity lines if present (PDL, PDH, Asian H/L, EQH/EQL, PWL/PWH)
            swept_levels = getattr(setup, "swept_levels", [])
            if swept_levels:
                for s_lvl in swept_levels[:2]:  # plot up to 2 key swept levels to avoid clutter
                    lvl_price = s_lvl.get("price", 0.0)
                    lvl_name = s_lvl.get("name", "SWEEP")
                    if lvl_price > 0:
                        ax.axhline(lvl_price, color="#eab308", linestyle="-.", linewidth=1.3, alpha=0.85)
                        ax.text(
                            2, lvl_price,
                            f" ⚡ SWEPT {lvl_name} ({lvl_price})",
                            color="#eab308",
                            va="bottom" if setup.direction == "BUY" else "top",
                            fontsize=8,
                            fontweight="bold"
                        )

            # Draw Zumiko EMAs (EMA Fast, EMA Slow, EMA Trend)
            if getattr(setup, "ema_238", 0.0) > 0 or getattr(setup, "strategy_name", "") in ("ZUMIKO", "GOLDEN_COMBO"):
                e_fast_full = df["close"].ewm(span=ZUMIKO_FAST_EMA, adjust=False).mean()
                e_slow_full = df["close"].ewm(span=ZUMIKO_SLOW_EMA, adjust=False).mean()
                e_trend_full = df["close"].ewm(span=ZUMIKO_TREND_EMA, adjust=False).mean()

                e_fast_sub = e_fast_full.tail(bars_count).reset_index(drop=True)
                e_slow_sub = e_slow_full.tail(bars_count).reset_index(drop=True)
                e_trend_sub = e_trend_full.tail(bars_count).reset_index(drop=True)

                ax.plot(e_fast_sub.index, e_fast_sub.values, color="#00e5ff", linewidth=1.2, label=f"EMA Fast ({ZUMIKO_FAST_EMA}): {setup.ema_fast}", alpha=0.9)
                ax.plot(e_slow_sub.index, e_slow_sub.values, color="#f59e0b", linewidth=1.4, label=f"EMA Slow ({ZUMIKO_SLOW_EMA}): {setup.ema_slow}", alpha=0.9)
                ax.plot(e_trend_sub.index, e_trend_sub.values, color="#a855f7", linewidth=2.0, linestyle="-", label=f"EMA Trend ({ZUMIKO_TREND_EMA}): {setup.ema_238}", alpha=0.95)

            # Draw Institutional Daily VWAP if present
            vwap_val = getattr(setup, "vwap", 0.0)
            if vwap_val > 0:
                ax.axhline(vwap_val, color="#38bdf8", linestyle=":", linewidth=1.4, alpha=0.9, label=f"Daily VWAP: {vwap_val}")

            # Lines for Entry, SL, TP1, TP2
            ax.axhline(setup.entry_price, color="#2962ff", linestyle=":", linewidth=2.0, label=f"Entry: {setup.entry_price}")
            ax.axhline(setup.sl_price, color="#f23645", linestyle="--", linewidth=1.8, label=f"SL: {setup.sl_price}")
            ax.axhline(setup.tp1_price, color="#089981", linestyle="--", linewidth=1.8, label=f"TP1: {setup.tp1_price}")

            # Text labels on right
            x_text = len(plot_df) + 3
            ax.text(x_text, setup.entry_price, f" ENTRY {setup.entry_price}", color="#2962ff", va="center", fontweight="bold", fontsize=9)
            ax.text(x_text, setup.sl_price, f" SL {setup.sl_price}", color="#f23645", va="center", fontweight="bold", fontsize=9)
            ax.text(x_text, setup.tp1_price, f" TP1 {setup.tp1_price}", color="#089981", va="center", fontweight="bold", fontsize=9)
            if vwap_val > 0:
                ax.text(x_text, vwap_val, f" VWAP {vwap_val}", color="#38bdf8", va="center", fontsize=8)

            # X-axis time ticks
            tick_step = max(1, len(plot_df) // 6)
            tick_indices = list(range(0, len(plot_df), tick_step))
            tick_labels = [plot_df.iloc[idx]["time"].strftime("%H:%M") for idx in tick_indices]
            ax.set_xticks(tick_indices)
            ax.set_xticklabels(tick_labels, color="#b2b5be", fontsize=9)

            # Y-axis styling
            ax.yaxis.tick_right()
            ax.yaxis.set_label_position("right")
            ax.tick_params(colors="#b2b5be", labelsize=9)
            ax.grid(color="#2a2e39", linestyle=":", linewidth=0.5, alpha=0.7)

            chart_low = plot_df["low"].min()
            chart_high = plot_df["high"].max()
            chart_range = max(chart_high - chart_low, 0.0001)

            # Limits
            all_y = [
                chart_low, chart_high,
                setup.sl_price, setup.tp1_price, setup.entry_price
            ]
            if setup.fibo_786 > 0:
                all_y.extend([setup.fibo_618, setup.fibo_786])
            if vwap_val > 0 and (chart_low - chart_range * 0.5) <= vwap_val <= (chart_high + chart_range * 0.5):
                all_y.append(vwap_val)
            for s_lvl in getattr(setup, "swept_levels", []):
                p = s_lvl.get("price", 0.0)
                if p > 0:
                    # Clamp to visible action window so distant HTF levels don't squash candles
                    if (chart_low - chart_range * 0.5) <= p <= (chart_high + chart_range * 0.5):
                        all_y.append(p)
            y_min = min(all_y)
            y_max = max(all_y)
            y_margin = (y_max - y_min) * 0.08
            ax.set_ylim(y_min - y_margin, y_max + y_margin)
            ax.set_xlim(-1, len(plot_df) + 10)

            # Header Title
            dir_tag = f"[{setup.direction}]"
            strat_name = getattr(setup, "strategy_name", "SMC")
            if strat_name == "GOLDEN_COMBO":
                strat_header = "GOLDEN COMBO (SMC + ZUMIKO)"
            elif strat_name == "ZUMIKO":
                strat_header = "ZUMIKO KILLMLM"
            else:
                strat_header = "SMC"
            score_tag = f"Score: {setup.confluence_score}% [{setup.grade}]" if setup.confluence_score > 0 else ""
            title_text = (
                f"{setup.symbol} [{setup.timeframe}] | {dir_tag} {strat_header} - {setup.setup_type} | {score_tag}\n"
                f"Bias: {setup.htf_trend} | Risk/Reward: 1:{setup.rr_ratio} | Time: {setup.timestamp.strftime('%Y-%m-%d %H:%M')}"
            )
            ax.set_title(title_text, color="#ffffff", fontsize=11, fontweight="bold", loc="left", pad=12)

            # Legend
            ax.legend(loc="upper left", facecolor="#1e222d", edgecolor="#363a45", fontsize=8)

            # Save plot
            safe_time = setup.timestamp.strftime("%Y%m%d_%H%M%S")
            filename = f"{setup.symbol}_{setup.direction}_{safe_time}.png"
            filepath = os.path.join(self.output_dir, filename)

            plt.tight_layout()
            plt.savefig(filepath, facecolor=fig.get_facecolor(), edgecolor="none", dpi=150)

            return filepath
        finally:
            plt.close(fig)

