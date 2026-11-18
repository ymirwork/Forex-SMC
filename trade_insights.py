import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ForexSMC.TradeInsights")


@dataclass
class EdgeMetrics:
    """Statistical metrics for a category of trades."""
    category: str
    label: str
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    net_r: float = 0.0
    gross_wins_r: float = 0.0
    gross_losses_r: float = 0.0
    avg_mae_r: float = 0.0

    @property
    def winrate(self) -> float:
        return (self.wins / self.n_trades * 100) if self.n_trades > 0 else 0.0

    @property
    def ev_per_trade(self) -> float:
        return (self.net_r / self.n_trades) if self.n_trades > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_losses_r == 0:
            return float("inf") if self.gross_wins_r > 0 else 0.0
        return abs(self.gross_wins_r / self.gross_losses_r)

    @property
    def is_weak(self) -> bool:
        """A category is weak if EV < 0 AND Profit Factor < 1.0."""
        return self.ev_per_trade < 0.0 and self.profit_factor < 1.0


@dataclass
class SetupEvaluation:
    """Result of evaluating a new setup against historical edge data."""
    is_flagged: bool = False
    warnings: List[str] = field(default_factory=list)
    risk_multiplier: float = 1.0  # 1.0 = normal, 0.5 = halved, 0.0 = blocked
    summary: str = ""


class TradeInsightEngine:
    """
    Quantitative Trade Insight Engine.
    Analyzes historical trades to identify weak setups by computing Expected Value (EV)
    and Profit Factor across multiple dimensions: symbol, session, strategy, confluence factors.
    """

    def __init__(self, journal, min_sample_size: int = 10):
        self.journal = journal
        self.min_sample_size = min_sample_size
        self._edge_cache: Dict[str, List[EdgeMetrics]] = {}
        self._last_refresh_count: int = 0

    def _get_closed_trades(self) -> List[Dict]:
        """Retrieves closed trades from the journal."""
        if hasattr(self.journal, "get_closed_trades"):
            return self.journal.get_closed_trades()
        # Fallback for older journal without the new method
        all_trades = self.journal.get_all_trades()
        return [t for t in all_trades if t.get("status") in ("CLOSED", "WIN", "LOSS", "BE")]

    def _compute_metrics(self, trades: List[Dict], category: str, label: str) -> EdgeMetrics:
        """Computes edge metrics for a group of trades."""
        m = EdgeMetrics(category=category, label=label)
        m.n_trades = len(trades)
        mae_sum = 0.0
        for t in trades:
            rr = float(t.get("realized_r", 0.0))
            m.net_r += rr
            if rr > 0:
                m.wins += 1
                m.gross_wins_r += rr
            elif rr < 0:
                m.losses += 1
                m.gross_losses_r += rr
            else:
                # BE trades count towards total but are neither win nor loss
                pass
            mae_sum += abs(float(t.get("mae_r", 0.0)))
        m.avg_mae_r = (mae_sum / m.n_trades) if m.n_trades > 0 else 0.0
        return m

    def _group_by(self, trades: List[Dict], key_fn, category: str) -> List[EdgeMetrics]:
        """Groups trades by a key function and computes metrics per group."""
        groups = defaultdict(list)
        for t in trades:
            k = key_fn(t)
            if k:
                groups[k].append(t)
        return [self._compute_metrics(group, category, label) for label, group in groups.items()]

    def refresh_edge_data(self) -> Dict[str, List[EdgeMetrics]]:
        """Recomputes all edge matrices from closed trades."""
        trades = self._get_closed_trades()
        if len(trades) == self._last_refresh_count and self._edge_cache:
            return self._edge_cache
        self._last_refresh_count = len(trades)

        result = {}

        # 1. By Symbol
        result["symbol"] = self._group_by(trades, lambda t: t.get("symbol", "").upper(), "symbol")

        # 2. By Session
        result["session"] = self._group_by(
            trades,
            lambda t: t.get("session_name") or self._infer_session(t.get("timestamp", "")),
            "session",
        )

        # 3. By Strategy
        result["strategy"] = self._group_by(
            trades, lambda t: t.get("strategy") or "SMC", "strategy"
        )

        # 4. By Day of Week
        result["day_of_week"] = self._group_by(
            trades, lambda t: self._get_day_of_week(t.get("timestamp", "")), "day_of_week"
        )

        # 5. By Grade (confluence bracket)
        def grade_fn(t):
            score = int(t.get("confluence_score", 0))
            if score >= 85:
                return "A+ (>=85%)"
            elif score >= 70:
                return "A (70-84%)"
            else:
                return "B (<70%)"
        result["grade"] = self._group_by(trades, grade_fn, "grade")

        # 6. By Confluence Factor Impact (which factors correlate with wins/losses)
        result["confluence_factors"] = self._analyze_confluence_impact(trades)

        # 7. Overall
        if trades:
            result["overall"] = [self._compute_metrics(trades, "overall", "ALL TRADES")]

        self._edge_cache = result
        return result

    def _analyze_confluence_impact(self, trades: List[Dict]) -> List[EdgeMetrics]:
        """Analyzes which confluence factors correlate with winning vs losing trades."""
        factor_present: Dict[str, List[Dict]] = defaultdict(list)
        factor_absent: Dict[str, List[Dict]] = defaultdict(list)

        for t in trades:
            raw_json = t.get("confluences_json", "{}")
            try:
                conf_data = json.loads(raw_json) if raw_json else {}
            except (json.JSONDecodeError, TypeError):
                conf_data = {}

            if not isinstance(conf_data, dict):
                continue

            for factor_name, achieved in conf_data.items():
                if achieved:
                    factor_present[factor_name].append(t)
                else:
                    factor_absent[factor_name].append(t)

        results = []
        for factor_name in set(list(factor_present.keys()) + list(factor_absent.keys())):
            present_trades = factor_present.get(factor_name, [])
            absent_trades = factor_absent.get(factor_name, [])
            if present_trades:
                results.append(self._compute_metrics(
                    present_trades, "confluence_present", f"{factor_name} ✅"
                ))
            if absent_trades:
                results.append(self._compute_metrics(
                    absent_trades, "confluence_absent", f"{factor_name} ❌"
                ))
        return results

    def _infer_session(self, timestamp_str: str) -> str:
        """Infers trading session from UTC timestamp for legacy trades without session_name."""
        if not timestamp_str:
            return "Unknown"
        try:
            from datetime import datetime
            dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            hour = dt.hour  # UTC hour
            if 0 <= hour < 6:
                return "Asian"
            elif 6 <= hour < 8:
                return "London Open"
            elif 8 <= hour < 12:
                return "London"
            elif 12 <= hour < 17:
                return "NY AM"
            elif 17 <= hour < 21:
                return "NY PM"
            else:
                return "Late/Off-Hours"
        except Exception:
            return "Unknown"

    def _get_day_of_week(self, timestamp_str: str) -> str:
        """Returns day name from timestamp."""
        if not timestamp_str:
            return "Unknown"
        try:
            from datetime import datetime
            dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            return days[dt.weekday()]
        except Exception:
            return "Unknown"

    def evaluate_setup(
        self,
        symbol: str,
        session_name: str = "",
        strategy: str = "SMC",
        confluence_score: int = 0,
        filter_mode: str = "ADVISORY",
    ) -> SetupEvaluation:
        """
        Evaluates a new incoming setup against historical edge data.
        Returns SetupEvaluation with flags, warnings, and risk multiplier.
        """
        edge_data = self.refresh_edge_data()
        evaluation = SetupEvaluation()
        weak_hits = []

        # Check symbol edge
        for m in edge_data.get("symbol", []):
            if m.label.upper() == symbol.upper() and m.n_trades >= self.min_sample_size and m.is_weak:
                weak_hits.append(
                    f"⚠️ {symbol}: EV {m.ev_per_trade:+.2f}R | WinRate {m.winrate:.0f}% | PF {m.profit_factor:.2f} (n={m.n_trades})"
                )

        # Check session edge
        if session_name:
            for m in edge_data.get("session", []):
                if m.label == session_name and m.n_trades >= self.min_sample_size and m.is_weak:
                    weak_hits.append(
                        f"⚠️ Sesja {session_name}: EV {m.ev_per_trade:+.2f}R | WinRate {m.winrate:.0f}% | PF {m.profit_factor:.2f} (n={m.n_trades})"
                    )

        # Check strategy edge
        strat_key = strategy.upper().replace(" ", "").replace("_", "")
        for m in edge_data.get("strategy", []):
            m_key = m.label.upper().replace(" ", "").replace("_", "")
            is_match = (
                m.label == strategy
                or m_key == strat_key
                or (("ZUMIKO" in m_key or "KILLMLM" in m_key) and ("ZUMIKO" in strat_key or "KILLMLM" in strat_key))
            )
            if is_match and m.n_trades >= self.min_sample_size and m.is_weak:
                weak_hits.append(
                    f"⚠️ Strategia {strategy}: EV {m.ev_per_trade:+.2f}R | WinRate {m.winrate:.0f}% | PF {m.profit_factor:.2f} (n={m.n_trades})"
                )

        # Check symbol+session cross
        closed = self._get_closed_trades()
        sym_sess_trades = [
            t for t in closed
            if t.get("symbol", "").upper() == symbol.upper()
            and (t.get("session_name") or self._infer_session(t.get("timestamp", ""))) == session_name
        ]
        if len(sym_sess_trades) >= self.min_sample_size:
            cross_m = self._compute_metrics(sym_sess_trades, "cross", f"{symbol} × {session_name}")
            if cross_m.is_weak:
                weak_hits.append(
                    f"⚠️ {symbol} w {session_name}: EV {cross_m.ev_per_trade:+.2f}R | WinRate {cross_m.winrate:.0f}% | PF {cross_m.profit_factor:.2f} (n={cross_m.n_trades})"
                )

        if weak_hits:
            evaluation.is_flagged = True
            evaluation.warnings = weak_hits
            if filter_mode == "STRICT":
                evaluation.risk_multiplier = 0.0
                evaluation.summary = "🛑 ZABLOKOWANO: Historycznie stratny setup."
            else:
                evaluation.risk_multiplier = 0.5
                evaluation.summary = "⚠️ Ryzyko zredukowane o 50%: Historycznie słaby setup."

        return evaluation

    def generate_insights_report(self) -> str:
        """Generates a comprehensive Telegram-formatted insights report."""
        edge_data = self.refresh_edge_data()
        lines = ["📊 **TRADE INSIGHTS REPORT**\n"]

        # Overall stats
        overall = edge_data.get("overall", [])
        if overall:
            o = overall[0]
            lines.append(f"📈 **Podsumowanie ({o.n_trades} zamkniętych):**")
            lines.append(f"• Win Rate: `{o.winrate:.1f}%` | Net R: `{o.net_r:+.2f}R`")
            lines.append(f"• EV per trade: `{o.ev_per_trade:+.3f}R` | PF: `{o.profit_factor:.2f}`")
            lines.append("")

        # Symbol breakdown
        sym_metrics = sorted(edge_data.get("symbol", []), key=lambda m: m.ev_per_trade)
        if sym_metrics:
            lines.append("💱 **Rozbicie wg par walutowych:**")
            for m in sym_metrics:
                icon = "🔴" if m.is_weak and m.n_trades >= self.min_sample_size else ("🟢" if m.ev_per_trade > 0 else "⚪")
                lines.append(
                    f"• {icon} `{m.label}`: EV `{m.ev_per_trade:+.2f}R` | WR `{m.winrate:.0f}%` | "
                    f"PF `{m.profit_factor:.2f}` | Net `{m.net_r:+.2f}R` (n={m.n_trades})"
                )
            lines.append("")

        # Session breakdown
        sess_metrics = sorted(edge_data.get("session", []), key=lambda m: m.ev_per_trade)
        if sess_metrics:
            lines.append("🕒 **Rozbicie wg sesji:**")
            for m in sess_metrics:
                icon = "🔴" if m.is_weak and m.n_trades >= self.min_sample_size else ("🟢" if m.ev_per_trade > 0 else "⚪")
                lines.append(
                    f"• {icon} `{m.label}`: EV `{m.ev_per_trade:+.2f}R` | WR `{m.winrate:.0f}%` | "
                    f"PF `{m.profit_factor:.2f}` (n={m.n_trades})"
                )
            lines.append("")

        # Strategy breakdown
        strat_metrics = sorted(edge_data.get("strategy", []), key=lambda m: m.ev_per_trade)
        if strat_metrics:
            lines.append("🎯 **Rozbicie wg strategii:**")
            for m in strat_metrics:
                icon = "🔴" if m.is_weak and m.n_trades >= self.min_sample_size else ("🟢" if m.ev_per_trade > 0 else "⚪")
                lines.append(
                    f"• {icon} `{m.label}`: EV `{m.ev_per_trade:+.2f}R` | WR `{m.winrate:.0f}%` | "
                    f"PF `{m.profit_factor:.2f}` (n={m.n_trades})"
                )
            lines.append("")

        # Day of week
        dow_metrics = edge_data.get("day_of_week", [])
        if dow_metrics:
            lines.append("📅 **Rozbicie wg dnia tygodnia:**")
            day_order = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
            for m in sorted(dow_metrics, key=lambda x: day_order.get(x.label, 9)):
                icon = "🔴" if m.is_weak and m.n_trades >= self.min_sample_size else ("🟢" if m.ev_per_trade > 0 else "⚪")
                lines.append(
                    f"• {icon} `{m.label}`: EV `{m.ev_per_trade:+.2f}R` | WR `{m.winrate:.0f}%` (n={m.n_trades})"
                )
            lines.append("")

        # Grade breakdown
        grade_metrics = edge_data.get("grade", [])
        if grade_metrics:
            lines.append("⭐ **Rozbicie wg oceny konfluencji:**")
            for m in sorted(grade_metrics, key=lambda x: x.label):
                icon = "🔴" if m.is_weak and m.n_trades >= self.min_sample_size else ("🟢" if m.ev_per_trade > 0 else "⚪")
                lines.append(
                    f"• {icon} `{m.label}`: EV `{m.ev_per_trade:+.2f}R` | WR `{m.winrate:.0f}%` | "
                    f"PF `{m.profit_factor:.2f}` (n={m.n_trades})"
                )
            lines.append("")

        # Confluence factor impact
        conf_metrics = edge_data.get("confluence_factors", [])
        if conf_metrics:
            lines.append("🧩 **Wpływ czynników konfluencji:**")
            for m in sorted(conf_metrics, key=lambda x: x.ev_per_trade):
                if m.n_trades >= 3:  # Lower threshold for factor analysis
                    icon = "🔴" if m.ev_per_trade < 0 else "🟢"
                    lines.append(
                        f"• {icon} `{m.label}`: EV `{m.ev_per_trade:+.2f}R` | WR `{m.winrate:.0f}%` (n={m.n_trades})"
                    )
            lines.append("")

        # Weak setup warnings
        all_weak = []
        for category_name, metrics_list in edge_data.items():
            if category_name in ("overall", "confluence_factors"):
                continue
            for m in metrics_list:
                if m.is_weak and m.n_trades >= self.min_sample_size:
                    all_weak.append(m)

        if all_weak:
            lines.append("🚨 **SŁABE ZAGRANIA DO ELIMINACJI:**")
            for m in sorted(all_weak, key=lambda x: x.ev_per_trade):
                lines.append(
                    f"• 🔴 **{m.category.upper()}: {m.label}** — EV `{m.ev_per_trade:+.2f}R`, "
                    f"PF `{m.profit_factor:.2f}`, WR `{m.winrate:.0f}%` (n={m.n_trades})"
                )
            lines.append("")
            lines.append(
                f"💡 _Tryb filtra: Bot aktywnie {'blokuje' if True else 'ostrzega o'} "
                f"powyższych schematów przy nowych sygnałach._"
            )
        else:
            lines.append("✅ **Brak wykrytych statystycznie słabych schematów** (wszystkie kategorie mają EV >= 0 lub n < min. próba).")

        return "\n".join(lines)

    def generate_weekend_summary(self) -> str:
        """Generates a concise weekend summary with top insights and recommendations."""
        edge_data = self.refresh_edge_data()
        lines = ["📋 **WEEKENDOWE PODSUMOWANIE EDGE**\n"]

        overall = edge_data.get("overall", [])
        if not overall or overall[0].n_trades == 0:
            return "📋 **WEEKENDOWE PODSUMOWANIE:** Brak zamkniętych transakcji do analizy."

        o = overall[0]
        lines.append(f"📊 Zamknięte zagrania: `{o.n_trades}` | Net R: `{o.net_r:+.2f}R` | WR: `{o.winrate:.1f}%` | EV: `{o.ev_per_trade:+.3f}R`\n")

        # Best and worst symbol
        sym_metrics = edge_data.get("symbol", [])
        if sym_metrics:
            best = max(sym_metrics, key=lambda m: m.ev_per_trade)
            worst = min(sym_metrics, key=lambda m: m.ev_per_trade)
            lines.append(f"🏆 **Najlepsza para:** `{best.label}` (EV: `{best.ev_per_trade:+.2f}R`, WR: `{best.winrate:.0f}%`)")
            lines.append(f"💀 **Najsłabsza para:** `{worst.label}` (EV: `{worst.ev_per_trade:+.2f}R`, WR: `{worst.winrate:.0f}%`)")
            lines.append("")

        # Actionable recommendations
        all_weak = []
        for category_name, metrics_list in edge_data.items():
            if category_name in ("overall", "confluence_factors"):
                continue
            for m in metrics_list:
                if m.is_weak and m.n_trades >= self.min_sample_size:
                    all_weak.append(m)

        if all_weak:
            lines.append("🎯 **Rekomendacje na przyszły tydzień:**")
            for m in sorted(all_weak, key=lambda x: x.ev_per_trade)[:5]:
                lines.append(f"• Rozważ wyeliminowanie: **{m.label}** (EV: `{m.ev_per_trade:+.2f}R`)")
        else:
            lines.append("🎯 **Rekomendacja:** Kontynuuj obecną strategię — brak wykrytych wycieków kapitału.")

        return "\n".join(lines)
