"""
AI Strategy & Trade Auditor Module.
Performs quantitative and qualitative post-trade review using historical SQLite trade data.
Generates structured statistical prompts and queries Google Gemini Pro (with fallback)
to provide institutional diagnostic insights and actionable parameter tuning.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import (
    ADAPTIVE_BE_CHOP_R,
    ADAPTIVE_BE_TREND_R,
    ADAPTIVE_BE_VOLATILE_R,
    ADAPTIVE_MODE,
    ALERT_COOLDOWN_MINUTES,
    AUDIT_DEFAULT_RANGE,
    AUTO_BE_OFFSET_PIPS,
    AUTO_BE_R_TRIGGER,
    ENABLE_ADAPTIVE_MANAGER,
    ENABLE_PYRAMIDING,
    ENABLE_SMC_STRATEGY,
    ENABLE_SUNDAY_AUDIT_REPORT,
    ENABLE_ZUMIKO_STRATEGY,
    EVENING_BLOCK_END_HOUR_UTC,
    EVENING_BLOCK_START_HOUR_UTC,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    MIN_CONFLUENCE_SCORE,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    POST_LOSS_COOLDOWN_MINUTES,
    PYRAMID_ALLOW_MIN_LOT_FLOAT,
    PYRAMID_AUTO_EXECUTE,
    PYRAMID_EXECUTION_TIMEFRAME,
    PYRAMID_MIN_PROFIT_R,
    PYRAMID_MIN_RR_TO_TP,
    clean_symbol,
)
from trade_insights import EdgeMetrics, TradeInsightEngine
from trade_journal import TradeJournal

logger = logging.getLogger("ForexSMC.AIAuditor")

# Allowed configuration keys for safe automated recommendation application
ALLOWED_CONFIG_KEYS = {
    "AUTO_BE_R_TRIGGER": float,
    "AUTO_BE_OFFSET_PIPS": float,
    "MIN_CONFLUENCE_SCORE": int,
    "PYRAMID_MIN_PROFIT_R": float,
    "PYRAMID_MIN_RR_TO_TP": float,
    "PYRAMID_AUTO_EXECUTE": bool,
    "PYRAMID_ALLOW_MIN_LOT_FLOAT": bool,
    "EVENING_BLOCK_START_HOUR_UTC": int,
    "EVENING_BLOCK_END_HOUR_UTC": int,
    "ALERT_COOLDOWN_MINUTES": int,
    "POST_LOSS_COOLDOWN_MINUTES": int,
    "ADAPTIVE_BE_TREND_R": float,
    "ADAPTIVE_BE_CHOP_R": float,
    "ADAPTIVE_BE_VOLATILE_R": float,
    "ADAPTIVE_TRAILING_MIN_STEP_R": float,
    "ENABLE_ZUMIKO_STRATEGY": bool,
    "ENABLE_SMC_STRATEGY": bool,
}


def parse_trade_timestamp(ts_val: Any) -> Optional[datetime]:
    """Safely parses timestamp string into datetime."""
    if not ts_val:
        return None
    if isinstance(ts_val, datetime):
        return ts_val
    ts_str = str(ts_val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts_str.split(".")[0], fmt)
        except ValueError:
            pass
    return None


def filter_trades_by_range(trades: List[Dict], range_spec: str = "30d") -> List[Dict]:
    """
    Filters trades list according to a range specification:
    - 'all': returns all trades
    - '30d', '7d', '14d', '90d': filters trades where timestamp >= now - N days
    - '50', '100': returns the last N trades sorted by timestamp
    """
    if not trades:
        return []

    range_spec = (range_spec or "30d").strip().lower()

    if range_spec == "all":
        return trades

    # Case: integer count (e.g. '50', '100')
    if range_spec.isdigit():
        n = int(range_spec)
        return trades[-n:] if n > 0 else trades

    # Case: days specification (e.g. '30d', '7d')
    days_match = re.match(r"^(\d+)d$", range_spec)
    if days_match:
        n_days = int(days_match.group(1))
        cutoff = datetime.now() - timedelta(days=n_days)
        filtered = []
        for t in trades:
            dt = parse_trade_timestamp(t.get("timestamp"))
            if dt and dt >= cutoff:
                filtered.append(t)
            elif not dt:
                # If timestamp cannot be parsed, include conservatively
                filtered.append(t)
        return filtered

    return trades


class AIAuditAggregator:
    """
    Extracts and aggregates quantitative metrics across trades for AI consumption.
    """

    def __init__(self, journal: TradeJournal, range_spec: str = "30d"):
        self.journal = journal
        self.range_spec = range_spec
        self.engine = TradeInsightEngine(journal)

    def get_closed_trades(self) -> List[Dict]:
        """Returns closed trades in the selected range."""
        all_closed = self.engine._get_closed_trades()
        return filter_trades_by_range(all_closed, self.range_spec)

    def compute_max_drawdown_r(self, trades: List[Dict]) -> float:
        """Computes peak-to-trough maximum drawdown in R."""
        cum_r = 0.0
        peak_r = 0.0
        max_dd = 0.0
        for t in trades:
            r = float(t.get("realized_r", 0.0) or 0.0)
            cum_r += r
            if cum_r > peak_r:
                peak_r = cum_r
            dd = peak_r - cum_r
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 2)

    def collect_metrics(self) -> Dict[str, Any]:
        """Collects full multi-dimensional edge metrics and execution diagnostics."""
        trades = self.get_closed_trades()
        if not trades:
            return {
                "range_spec": self.range_spec,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "bes": 0,
                "winrate": 0.0,
                "net_r": 0.0,
                "ev_per_trade": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_r": 0.0,
                "symbols": [],
                "sessions": [],
                "strategies": [],
                "days_of_week": [],
                "grades": [],
                "regimes": [],
                "confluences": [],
                "execution_diagnostics": {},
                "current_config": self._get_current_config(),
            }

        # 1. Overall stats
        overall_m = self.engine._compute_metrics(trades, "overall", "ALL TRADES")
        max_dd_r = self.compute_max_drawdown_r(trades)

        # 2. Slice by dimensions
        symbols = self.engine._group_by(trades, lambda t: clean_symbol(t.get("symbol", "")), "symbol")
        sessions = self.engine._group_by(
            trades, lambda t: t.get("session_name") or self.engine._infer_session(t.get("timestamp", "")), "session"
        )
        strategies = self.engine._group_by(trades, lambda t: t.get("strategy") or "SMC", "strategy")
        days = self.engine._group_by(trades, lambda t: self.engine._get_day_of_week(t.get("timestamp", "")), "day_of_week")

        def grade_fn(t):
            g = t.get("grade")
            if g:
                return g
            score = int(t.get("confluence_score", 0) or 0)
            return "A+ (>=85%)" if score >= 85 else ("A (70-84%)" if score >= 70 else "B (<70%)")

        grades = self.engine._group_by(trades, grade_fn, "grade")
        regimes = self.engine._group_by(trades, lambda t: t.get("regime") or "UNKNOWN", "regime")
        confluences = self.engine._analyze_confluence_impact(trades)

        # 3. Execution diagnostics (MAE / MFE / Premature BE)
        losses = [t for t in trades if float(t.get("realized_r", 0.0) or 0.0) < 0]
        wins = [t for t in trades if float(t.get("realized_r", 0.0) or 0.0) > 0]
        bes = [t for t in trades if float(t.get("realized_r", 0.0) or 0.0) == 0]

        avg_mae_losses = (sum(abs(float(t.get("mae_r", 0.0) or 0.0)) for t in losses) / len(losses)) if losses else 0.0
        avg_mfe_losses = (sum(float(t.get("max_r", 0.0) or 0.0) for t in losses) / len(losses)) if losses else 0.0

        # Premature BE: Trade reached >= 1.2R (MFE) but closed at BE or small loss
        premature_be_trades = [
            t for t in trades
            if float(t.get("max_r", 0.0) or 0.0) >= 1.2 and float(t.get("realized_r", 0.0) or 0.0) <= 0.1
        ]

        # Top 3 worst losses & top 3 winners
        sorted_by_r = sorted(trades, key=lambda t: float(t.get("realized_r", 0.0) or 0.0))
        worst_losses = [
            {
                "symbol": clean_symbol(t.get("symbol", "")),
                "direction": t.get("direction"),
                "realized_r": t.get("realized_r"),
                "max_r": t.get("max_r"),
                "mae_r": t.get("mae_r"),
                "strategy": t.get("strategy"),
                "session": t.get("session_name") or self.engine._infer_session(t.get("timestamp", "")),
                "score": t.get("confluence_score"),
                "timestamp": t.get("timestamp"),
            }
            for t in sorted_by_r[:3] if float(t.get("realized_r", 0.0) or 0.0) < 0
        ]
        top_winners = [
            {
                "symbol": clean_symbol(t.get("symbol", "")),
                "direction": t.get("direction"),
                "realized_r": t.get("realized_r"),
                "strategy": t.get("strategy"),
                "session": t.get("session_name") or self.engine._infer_session(t.get("timestamp", "")),
                "timestamp": t.get("timestamp"),
            }
            for t in sorted_by_r[-3:][::-1] if float(t.get("realized_r", 0.0) or 0.0) > 0
        ]

        return {
            "range_spec": self.range_spec,
            "total_trades": len(trades),
            "wins": overall_m.wins,
            "losses": overall_m.losses,
            "bes": len(bes),
            "winrate": round(overall_m.winrate, 1),
            "net_r": round(overall_m.net_r, 2),
            "ev_per_trade": round(overall_m.ev_per_trade, 3),
            "profit_factor": round(overall_m.profit_factor, 2) if overall_m.profit_factor != float("inf") else 999.0,
            "max_drawdown_r": max_dd_r,
            "symbols": [self._metric_to_dict(m) for m in symbols],
            "sessions": [self._metric_to_dict(m) for m in sessions],
            "strategies": [self._metric_to_dict(m) for m in strategies],
            "days_of_week": [self._metric_to_dict(m) for m in days],
            "grades": [self._metric_to_dict(m) for m in grades],
            "regimes": [self._metric_to_dict(m) for m in regimes],
            "confluences": [self._metric_to_dict(m) for m in confluences if m.n_trades >= 3],
            "execution_diagnostics": {
                "avg_mae_losses": round(avg_mae_losses, 2),
                "avg_mfe_losses": round(avg_mfe_losses, 2),
                "premature_be_count": len(premature_be_trades),
                "worst_losses": worst_losses,
                "top_winners": top_winners,
            },
            "current_config": self._get_current_config(),
        }

    @staticmethod
    def _metric_to_dict(m: EdgeMetrics) -> Dict[str, Any]:
        return {
            "label": m.label,
            "n_trades": m.n_trades,
            "wins": m.wins,
            "losses": m.losses,
            "winrate": round(m.winrate, 1),
            "net_r": round(m.net_r, 2),
            "ev_per_trade": round(m.ev_per_trade, 3),
            "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else 999.0,
            "is_weak": m.is_weak,
        }

    @staticmethod
    def _get_current_config() -> Dict[str, Any]:
        return {
            "AUTO_BE_R_TRIGGER": AUTO_BE_R_TRIGGER,
            "AUTO_BE_OFFSET_PIPS": AUTO_BE_OFFSET_PIPS,
            "MIN_CONFLUENCE_SCORE": MIN_CONFLUENCE_SCORE,
            "ENABLE_SMC_STRATEGY": ENABLE_SMC_STRATEGY,
            "ENABLE_ZUMIKO_STRATEGY": ENABLE_ZUMIKO_STRATEGY,
            "ENABLE_PYRAMIDING": ENABLE_PYRAMIDING,
            "PYRAMID_EXECUTION_TIMEFRAME": PYRAMID_EXECUTION_TIMEFRAME,
            "PYRAMID_MIN_PROFIT_R": PYRAMID_MIN_PROFIT_R,
            "PYRAMID_MIN_RR_TO_TP": PYRAMID_MIN_RR_TO_TP,
            "PYRAMID_AUTO_EXECUTE": PYRAMID_AUTO_EXECUTE,
            "PYRAMID_ALLOW_MIN_LOT_FLOAT": PYRAMID_ALLOW_MIN_LOT_FLOAT,
            "EVENING_BLOCK_START_HOUR_UTC": EVENING_BLOCK_START_HOUR_UTC,
            "EVENING_BLOCK_END_HOUR_UTC": EVENING_BLOCK_END_HOUR_UTC,
            "ALERT_COOLDOWN_MINUTES": ALERT_COOLDOWN_MINUTES,
            "POST_LOSS_COOLDOWN_MINUTES": POST_LOSS_COOLDOWN_MINUTES,
            "ENABLE_ADAPTIVE_MANAGER": ENABLE_ADAPTIVE_MANAGER,
            "ADAPTIVE_MODE": ADAPTIVE_MODE,
            "ADAPTIVE_BE_TREND_R": ADAPTIVE_BE_TREND_R,
            "ADAPTIVE_BE_CHOP_R": ADAPTIVE_BE_CHOP_R,
            "ADAPTIVE_BE_VOLATILE_R": ADAPTIVE_BE_VOLATILE_R,
        }


def format_audit_prompt(metrics: Dict[str, Any]) -> str:
    """
    Builds a structured prompt containing real statistics and strategy rules for the LLM.
    """
    total = metrics.get("total_trades", 0)
    lines = [
        "Jesteś Głównym Analitykiem Ilościowym (Senior Quant & SMC Risk Manager) dla instytucjonalnego bota Forex/Gold.",
        "Twoim zadaniem jest przeprowadzić rygorystyczny audyt wyników egzekucji strategii na podstawie danych z bazy `trades.db`.",
        "",
        f"### 📊 1. METRYKI OGÓLNE (Zakres: {metrics.get('range_spec', '30d')})",
        f"- Zamknięte transakcje: **{total}** (WIN: {metrics.get('wins')}, LOSS: {metrics.get('losses')}, BE: {metrics.get('bes')})",
        f"- Win Rate: **{metrics.get('winrate')}%**",
        f"- Łączny zysk w R (Net R): **{metrics.get('net_r'):+.2f}R**",
        f"- Wartość oczekiwana na transakcję (EV): **{metrics.get('ev_per_trade'):+.3f}R**",
        f"- Profit Factor (PF): **{metrics.get('profit_factor')}**",
        f"- Max Drawdown w R: **{metrics.get('max_drawdown_r')}R**",
        "",
        "### 💱 2. ROZBICIE WG PAR WALUTOWYCH (SYMBOL EDGE)",
    ]

    for s in sorted(metrics.get("symbols", []), key=lambda x: x["ev_per_trade"]):
        icon = "🔴" if s["is_weak"] and s["n_trades"] >= 3 else ("🟢" if s["ev_per_trade"] > 0 else "⚪")
        lines.append(f"- {icon} **{s['label']}**: EV `{s['ev_per_trade']:+.2f}R` | WR `{s['winrate']}%` | PF `{s['profit_factor']}` | Net `{s['net_r']:+.2f}R` (n={s['n_trades']})")

    lines.append("\n### 🕒 3. ROZBICIE WG SESJI (SESSION EDGE)")
    for s in sorted(metrics.get("sessions", []), key=lambda x: x["ev_per_trade"]):
        icon = "🔴" if s["is_weak"] and s["n_trades"] >= 3 else ("🟢" if s["ev_per_trade"] > 0 else "⚪")
        lines.append(f"- {icon} **{s['label']}**: EV `{s['ev_per_trade']:+.2f}R` | WR `{s['winrate']}%` | PF `{s['profit_factor']}` (n={s['n_trades']})")

    lines.append("\n### 🎯 4. ROZBICIE WG STRATEGII")
    for st in metrics.get("strategies", []):
        icon = "🔴" if st["is_weak"] else "🟢"
        lines.append(f"- {icon} **{st['label']}**: EV `{st['ev_per_trade']:+.2f}R` | WR `{st['winrate']}%` | PF `{st['profit_factor']}` | Net `{st['net_r']:+.2f}R` (n={st['n_trades']})")

    lines.append("\n### ⭐ 5. ROZBICIE WG OCENY GRADE I REŻIMU RYNKU")
    for g in metrics.get("grades", []):
        lines.append(f"- Grade **{g['label']}**: EV `{g['ev_per_trade']:+.2f}R` | WR `{g['winrate']}%` | PF `{g['profit_factor']}` (n={g['n_trades']})")
    for r in metrics.get("regimes", []):
        lines.append(f"- Reżim **{r['label']}**: EV `{r['ev_per_trade']:+.2f}R` | WR `{r['winrate']}%` (n={r['n_trades']})")

    lines.append("\n### 🧩 6. WPŁYW CZYNNIKÓW KONFLUENCJI (CONFLUENCE ABLATION)")
    for c in metrics.get("confluences", [])[:10]:
        icon = "🔴" if c["ev_per_trade"] < 0 else "🟢"
        lines.append(f"- {icon} `{c['label']}`: EV `{c['ev_per_trade']:+.2f}R` | WR `{c['winrate']}%` (n={c['n_trades']})")

    diag = metrics.get("execution_diagnostics", {})
    lines.append("\n### 🔍 7. DIAGNOSTYKA EGZEKUCJI (MAE / MFE / BREAK-EVEN)")
    lines.append(f"- Średnie MAE na stratach (jak głęboko cena szła przeciwko nam): `{diag.get('avg_mae_losses', 0.0)}R`")
    lines.append(f"- Średnie MFE na stratach (ile zysku było na stole przed stratą): `{diag.get('avg_mfe_losses', 0.0)}R`")
    lines.append(f"- Transakcje z przedwczesnym ucięciem na BE (MFE >= 1.2R, ale zysk <= 0.1R): `{diag.get('premature_be_count', 0)}`")

    lines.append("\n### ⚙️ 8. AKTUALNA KONFIGURACJA BOTA (config.py)")
    lines.append("```json")
    lines.append(json.dumps(metrics.get("current_config", {}), indent=2))
    lines.append("```")

    lines.append("\n" + "=" * 60)
    lines.append("### TWOJE ZADANIE ANALITYCZNE:")
    lines.append("Przeanalizuj powyższe dane i przedstaw profesjonalny raport w 4 sekcjach:")
    lines.append("1. **WYCIEKI KAPITAŁU (LEAKS):** Wskaż 3 największe wycieki zysku (np. stratne sesje, pary walutowe, dni tygodnia).")
    lines.append("2. **KRYTYKA I SENS STRATEGII:** Oceń fundamentalną logikę setupów SMC oraz Zumiko KillMLM. Odpowiedz na pytanie: dlaczego ocena A+ lub A radzi sobie tak a nie inaczej? Czy strategie nie wchodzą za późno po wyczerpaniu impulsu?")
    lines.append("3. **OPTYMALIZACJA ZARZĄDZANIA POZYCJĄ (MFE/MAE/BE):** Na podstawie MFE/MAE oceń, czy Stop Loss jest za szeroki/wąski oraz czy reguła Break-Even (obecnie `AUTO_BE_R_TRIGGER`) nie dusi pozycji zbyt wcześnie.")
    lines.append("4. **REKOMENDACJE I ZMIANY:** Podaj konkretne liczby i zmiany parametrów.")
    lines.append("")
    lines.append("Na samym końcu raportu ZAWSZE umieść blok kodu JSON o dokładnie następującej strukturze:")
    lines.append("```json")
    lines.append("{")
    lines.append('  "summary": "Krótkie podsumowanie 1-2 zdania",')
    lines.append('  "capital_leaks": ["Opis wycieku 1", "Opis wycieku 2"],')
    lines.append('  "strategy_critique": "Kluczowy wniosek o logice strategii",')
    lines.append('  "recommended_config": {')
    lines.append('    "AUTO_BE_R_TRIGGER": 1.25,')
    lines.append('    "MIN_CONFLUENCE_SCORE": 75')
    lines.append("  },")
    lines.append('  "action_items": ["Zadanie 1", "Zadanie 2"]')
    lines.append("}")
    lines.append("```")

    return "\n".join(lines)


def query_gemini_api(prompt: str, api_key: str, model_name: str = "gemini-flash-lite-latest") -> Tuple[bool, str]:
    """
    Queries Google Gemini API via official REST endpoint using httpx (sync) with automatic model fallback.
    """
    if not api_key:
        return False, "Brak klucza GEMINI_API_KEY w konfiguracji."

    models_to_try = [model_name]
    for fallback_m in ("gemini-flash-lite-latest", "gemini-3.5-flash", "gemini-3.8-flash", "gemini-pro-latest"):
        if fallback_m not in models_to_try:
            models_to_try.append(fallback_m)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
        },
    }

    last_err = ""
    with httpx.Client(timeout=45.0) as client:
        for m in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
            try:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        full_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
                        if full_text:
                            return True, full_text
                    return False, f"Brak treści w odpowiedzi Gemini ({m}): {data}"
                elif resp.status_code in (404, 429, 503):
                    last_err = f"Model {m} zwrócił {resp.status_code}: {resp.text[:200]}"
                    continue
                else:
                    return False, f"Błąd Gemini API ({m}, {resp.status_code}): {resp.text[:300]}"
            except Exception as e:
                last_err = f"Wyjątek dla modelu {m}: {e}"
                continue

    return False, f"Wszystkie próby modeli Gemini zawiodły. Ostatni błąd: {last_err}"


async def query_gemini_api_async(prompt: str, api_key: str, model_name: str = "gemini-flash-lite-latest") -> Tuple[bool, str]:
    """
    Asynchronously queries Google Gemini API via official REST endpoint using httpx with automatic model fallback.
    """
    if not api_key:
        return False, "Brak klucza GEMINI_API_KEY w konfiguracji."

    models_to_try = [model_name]
    for fallback_m in ("gemini-flash-lite-latest", "gemini-3.5-flash", "gemini-3.8-flash", "gemini-pro-latest"):
        if fallback_m not in models_to_try:
            models_to_try.append(fallback_m)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
        },
    }

    last_err = ""
    async with httpx.AsyncClient(timeout=45.0) as client:
        for m in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        full_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
                        if full_text:
                            return True, full_text
                    return False, f"Brak treści w odpowiedzi Gemini ({m}): {data}"
                elif resp.status_code in (404, 429, 503):
                    last_err = f"Model {m} zwrócił {resp.status_code}: {resp.text[:200]}"
                    continue
                else:
                    return False, f"Błąd Gemini API ({m}, {resp.status_code}): {resp.text[:300]}"
            except Exception as e:
                last_err = f"Wyjątek dla modelu {m}: {e}"
                continue

    return False, f"Wszystkie próby modeli Gemini zawiodły. Ostatni błąd: {last_err}"


def deterministic_fallback_report(metrics: Dict[str, Any]) -> str:
    """
    Generates a deterministic rule-based quantitative audit when no LLM API key is present.
    """
    total = metrics.get("total_trades", 0)
    lines = [
        "# 📊 STATYSTYCZNY AUDYT STRATEGII (DETERMINISTIC FALLBACK)",
        f"_Brak aktywnego klucza GEMINI_API_KEY. Raport wygenerowany deterministycznie na podstawie {total} zagrań._\n",
        f"• **Win Rate:** `{metrics.get('winrate')}%` | **Net R:** `{metrics.get('net_r'):+.2f}R`",
        f"• **EV per trade:** `{metrics.get('ev_per_trade'):+.3f}R` | **Profit Factor:** `{metrics.get('profit_factor')}`",
        f"• **Max Drawdown:** `{metrics.get('max_drawdown_r')}R`\n",
    ]

    # Capital leaks
    weak_sessions = [s for s in metrics.get("sessions", []) if s["is_weak"] and s["n_trades"] >= 3]
    weak_symbols = [s for s in metrics.get("symbols", []) if s["is_weak"] and s["n_trades"] >= 3]
    weak_strategies = [s for s in metrics.get("strategies", []) if s["is_weak"] and s["n_trades"] >= 3]

    lines.append("### 🚨 WYKRYTE WYCIEKI KAPITAŁU:")
    if weak_sessions:
        for s in weak_sessions:
            lines.append(f"- 🔴 **Sesja {s['label']}**: EV `{s['ev_per_trade']:+.2f}R`, WR `{s['winrate']}%` (n={s['n_trades']})")
    if weak_symbols:
        for s in weak_symbols:
            lines.append(f"- 🔴 **Para {s['label']}**: EV `{s['ev_per_trade']:+.2f}R`, WR `{s['winrate']}%` (n={s['n_trades']})")
    if weak_strategies:
        for s in weak_strategies:
            lines.append(f"- 🔴 **Strategia {s['label']}**: EV `{s['ev_per_trade']:+.2f}R`, WR `{s['winrate']}%` (n={s['n_trades']})")
    if not (weak_sessions or weak_symbols or weak_strategies):
        lines.append("- ✅ Brak wykrytych statystycznie istotnych wycieków kapitału.")

    # MFE / BE Analysis
    diag = metrics.get("execution_diagnostics", {})
    p_be = diag.get("premature_be_count", 0)
    lines.append("\n### 🔍 ANALIZA EGZEKUCJI I BREAK-EVEN:")
    if p_be > 0:
        lines.append(f"- ⚠️ Wykryto **{p_be}** transakcji, które osiągnęły MFE >= 1.2R, lecz zakończyły się na BE (0R). Sugerowane odsunięcie BE triggera.")
    lines.append(f"- Średnie MAE na stratach: `{diag.get('avg_mae_losses', 0.0)}R`.")

    # Recommendations
    recommended_config: Dict[str, Any] = {}
    if p_be >= 3 and AUTO_BE_R_TRIGGER < 1.2:
        recommended_config["AUTO_BE_R_TRIGGER"] = 1.25

    lines.append("\n### 🎯 REKOMENDACJE:")
    lines.append("```json")
    lines.append(json.dumps({
        "summary": f"Deterministyczny audyt {total} transakcji. Wykryto {len(weak_sessions) + len(weak_symbols)} słabych kategorii.",
        "capital_leaks": [s["label"] for s in (weak_sessions + weak_symbols)],
        "strategy_critique": "Zalecana eliminacja słabych sesji i weryfikacja Break-Even.",
        "recommended_config": recommended_config,
        "action_items": ["Rozważ wykluczenie stratnych sesji", "Dostosuj próg Break-Even"]
    }, indent=2))
    lines.append("```")

    return "\n".join(lines)


def extract_recommendations_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Extracts structured JSON dictionary from an LLM response containing a ```json code block.
    """
    if not text:
        return None

    # Search for ```json ... ``` or general json block
    matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in matches:
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and "recommended_config" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue

    # Fallback search for bare { ... } containing recommended_config
    try:
        start_idx = text.find('{"summary"')
        if start_idx == -1:
            start_idx = text.find('{"recommended_config"')
        if start_idx != -1:
            end_idx = text.rfind("}")
            if end_idx > start_idx:
                parsed = json.loads(text[start_idx : end_idx + 1])
                if isinstance(parsed, dict):
                    return parsed
    except Exception:
        pass

    return None


def save_audit_report(
    report_text: str,
    output_dir: str = "docs/audits",
    prefix: str = "audit",
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    Persists markdown audit report and extracts latest recommendations.json.
    """
    os.makedirs(output_dir, exist_ok=True)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_filename = f"{prefix}_{ts_str}.md"
    md_path = os.path.join(output_dir, md_filename)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    rec_json_path = os.path.join(output_dir, "recommendations.json")
    rec_data = extract_recommendations_json(report_text)
    if rec_data:
        rec_data["generated_at"] = datetime.now(timezone.utc).isoformat()
        rec_data["source_report"] = md_filename
        with open(rec_json_path, "w", encoding="utf-8") as f:
            json.dump(rec_data, f, indent=2, ensure_ascii=False)

    return md_path, rec_json_path, rec_data


def apply_recommendations(
    rec_path: str = "docs/audits/recommendations.json",
    env_path: str = ".env",
    dry_run: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Applies validated parameter recommendations from recommendations.json to .env.
    Creates a timestamped backup of .env before making changes.
    """
    if not os.path.exists(rec_path):
        return False, [f"Plik rekomendacji '{rec_path}' nie istnieje."]

    try:
        with open(rec_path, "r", encoding="utf-8") as f:
            rec_data = json.load(f)
    except Exception as e:
        return False, [f"Błąd odczytu pliku JSON: {e}"]

    config_updates = rec_data.get("recommended_config", {})
    if not config_updates:
        return False, ["Brak sekcji 'recommended_config' w pliku rekomendacji."]

    validated_updates = {}
    validation_notes = []

    for key, val in config_updates.items():
        if key not in ALLOWED_CONFIG_KEYS:
            validation_notes.append(f"⚠️ Pomijam nieznany lub niedozwolony klucz: '{key}'")
            continue

        target_type = ALLOWED_CONFIG_KEYS[key]
        try:
            if target_type == bool:
                if isinstance(val, bool):
                    val_str = "True" if val else "False"
                else:
                    val_str = "True" if str(val).lower() in ("true", "1") else "False"
            elif target_type == int:
                val_str = str(int(val))
            elif target_type == float:
                val_str = str(round(float(val), 2))
            else:
                val_str = str(val)
            validated_updates[key] = val_str
        except Exception as conv_err:
            validation_notes.append(f"⚠️ Błąd konwersji typu dla '{key}' ({val}): {conv_err}")

    if not validated_updates:
        return False, ["Brak poprawnych parametrów do wdrożenia."] + validation_notes

    if dry_run:
        dry_msgs = ["🔍 [DRY-RUN] Proponowane zmiany w konfiguracji (.env):"]
        for k, v in validated_updates.items():
            dry_msgs.append(f"  • {k} = {v}")
        return True, dry_msgs + validation_notes

    # Create backup of .env
    if os.path.exists(env_path):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{env_path}.backup_{ts}"
        shutil.copyfile(env_path, backup_path)
        validation_notes.append(f"📦 Utworzono kopię zapasową: {backup_path}")
        with open(env_path, "r", encoding="utf-8") as f:
            env_lines = f.readlines()
    else:
        env_lines = []

    updated_keys = set()
    new_lines = []

    for line in env_lines:
        line_clean = line.strip()
        if line_clean.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue

        k = line_clean.split("=")[0].strip()
        if k in validated_updates:
            # Preserve comment if present on the line
            comment_part = ""
            if " #" in line:
                comment_part = " #" + line.split(" #", 1)[1]
            new_lines.append(f"{k}={validated_updates[k]}{comment_part}\n")
            updated_keys.add(k)
            validation_notes.append(f"✅ Zaktualizowano: {k} = {validated_updates[k]}")
        else:
            new_lines.append(line)

    # Append any validated keys not previously in .env
    for k, v in validated_updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}\n")
            validation_notes.append(f"➕ Dodano nową zmienną: {k} = {v}")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return True, validation_notes


def run_audit(
    range_spec: str = "30d",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    save_files: bool = True,
) -> Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Main orchestration function to run a complete AI audit.
    Returns (report_text, metrics_dict, recommendations_dict).
    """
    journal = TradeJournal()
    aggregator = AIAuditAggregator(journal, range_spec=range_spec)
    metrics = aggregator.collect_metrics()
    prompt = format_audit_prompt(metrics)

    key = api_key or GEMINI_API_KEY
    mdl = model or GEMINI_MODEL or "gemini-1.5-pro"

    if key:
        logger.info(f"Odpytuję Gemini Pro ({mdl}) dla audytu {range_spec}...")
        success, ai_text = query_gemini_api(prompt, key, mdl)
        if success:
            report_text = ai_text
        else:
            logger.warning(f"Gemini API zwróciło błąd: {ai_text}. Przełączam na deterministyczny fallback.")
            report_text = f"⚠️ **Uwaga: Gemini API błąd:** {ai_text}\n\n" + deterministic_fallback_report(metrics)
    else:
        logger.info("Brak GEMINI_API_KEY. Generuję deterministyczny raport statystyczny.")
        report_text = deterministic_fallback_report(metrics)

    rec_data = None
    if save_files:
        _, _, rec_data = save_audit_report(report_text, output_dir="docs/audits", prefix=f"audit_{range_spec}")

    return report_text, metrics, rec_data


def main():
    parser = argparse.ArgumentParser(description="AI Strategy & Trade Auditor")
    parser.add_argument("--range", default=AUDIT_DEFAULT_RANGE, help="Zakres analizy (np. 30d, 7d, all, 50)")
    parser.add_argument("--export", action="store_true", help="Eksportuje prompt i metryki do czatu z AI")
    parser.add_argument("--apply", action="store_true", help="Aplikuje najnowsze rekomendacje do .env")
    parser.add_argument("--dry-run", action="store_true", help="Symulacja zmian bez modyfikacji .env")
    parser.add_argument("--raw", action="store_true", help="Wypisuje surowe metryki JSON")
    args = parser.parse_args()

    if args.apply or args.dry_run:
        ok, msgs = apply_recommendations(dry_run=args.dry_run)
        print("\n".join(msgs))
        return

    journal = TradeJournal()
    aggregator = AIAuditAggregator(journal, range_spec=args.range)
    metrics = aggregator.collect_metrics()

    if args.raw:
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
        return

    if args.export:
        prompt = format_audit_prompt(metrics)
        print("=" * 65)
        print("   WYEKSPORTOWANY PROMPT DLA AI (DO CZATU / ZEWNĘTRZNEGO LLM)")
        print("=" * 65)
        print(prompt)
        return

    print(f"\n🔍 Uruchamiam audyt AI (zakres: {args.range})...")
    report, _, rec_data = run_audit(range_spec=args.range)
    print("\n" + "=" * 65)
    print("                 WYNIK AUDYTU AI")
    print("=" * 65 + "\n")
    print(report)

    if rec_data and rec_data.get("recommended_config"):
        print("\n💡 Znaleziono rekomendacje parametrów. Aby je wdrożyć, uruchom:")
        print("   python ai_auditor.py --apply")


if __name__ == "__main__":
    main()
