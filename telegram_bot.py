import asyncio
import logging
import os
import time
from typing import Dict, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from config import (
    ALLOWED_CHAT_IDS,
    DEFAULT_RISK_PERCENT,
    DISMISS_MUTE_HOURS,
    DYNAMIC_FILTER_MODE,
    ENABLE_DYNAMIC_EDGE_FILTER,
    ENABLE_NOTION,
    INSIGHTS_MIN_SAMPLE_SIZE,
    PROP_FIRM_MODE,
    PROP_INITIAL_BALANCE,
    PROP_MAX_DRAWDOWN_PERCENT,
    PROP_MAX_OPEN_TRADES,
    PROP_RISK_PER_TRADE_PERCENT,
    PROP_SAFETY_BUFFER_PERCENT,
    PYRAMID_LOCK_PREVIOUS_PROFIT,
    TELEGRAM_ALERT_CHANNEL,
    TELEGRAM_BOT_TOKEN,
    clean_symbol,
)
from mt5_client import MT5Client
from news_filter import NewsFilter
from notion_journal import NotionJournal
from rr_optimizer import RROptimizer
from smc_detector import TradeSetup
from trade_journal import TradeJournal
from trade_insights import TradeInsightEngine

logger = logging.getLogger("ForexSMC.TelegramBot")


class TelegramBotManager:
    """Manages Telegram bot lifecycle, commands, alert dispatch, and interactive order executions."""

    def __init__(
        self,
        mt5_client: MT5Client,
        token: str = TELEGRAM_BOT_TOKEN,
        alert_channel: int = TELEGRAM_ALERT_CHANNEL,
        allowed_ids: Optional[list] = None,
        journal: Optional[TradeJournal] = None,
        news_filter: Optional[NewsFilter] = None,
    ):
        self.mt5_client = mt5_client
        self.token = token
        self.alert_channel = alert_channel
        self.authorized_chat_id = alert_channel  # alias for backward-compatibility
        self.allowed_chat_ids = allowed_ids if allowed_ids is not None else ALLOWED_CHAT_IDS
        self.app: Optional[Application] = None
        self.pending_setups: Dict[str, TradeSetup] = {}
        self.pending_setups_time: Dict[str, float] = {}
        self.scanner_callback = None  # Reference to manual trigger scan
        self.journal = journal or TradeJournal()
        self.notion = NotionJournal()
        self.news_filter = news_filter or NewsFilter()
        self.insight_engine = TradeInsightEngine(self.journal, min_sample_size=INSIGHTS_MIN_SAMPLE_SIZE) if ENABLE_DYNAMIC_EDGE_FILTER else None
        self.muted_symbols: Dict[Tuple[str, str], float] = {}

    def mute_symbol(self, symbol: str, direction: str, hours: float = DISMISS_MUTE_HOURS):
        """Mutes alerts for this symbol & direction for a specified duration after dismissal."""
        key = (clean_symbol(symbol), direction.upper())
        self.muted_symbols[key] = time.time() + (hours * 3600)
        logger.info(f"Muted {key[0]} {key[1]} alerts for {hours:.1f} hours.")

    def is_symbol_muted(self, symbol: str, direction: str) -> bool:
        """Checks if alerts for this symbol & direction are currently muted."""
        key = (clean_symbol(symbol), direction.upper())
        exp = self.muted_symbols.get(key)
        if exp:
            if time.time() < exp:
                return True
            else:
                del self.muted_symbols[key]
        return False

    def _cleanup_expired_setups(self, max_age_hours: float = 6.0):
        """Cleans up setups in pending_setups older than max_age_hours to prevent memory leaks."""
        now = time.time()
        expired = [sid for sid, ts in self.pending_setups_time.items() if (now - ts) > (max_age_hours * 3600)]
        for sid in expired:
            self.pending_setups.pop(sid, None)
            self.pending_setups_time.pop(sid, None)

    def set_scanner_callback(self, callback):
        self.scanner_callback = callback

    def _is_authorized(self, update: Update) -> bool:
        """Enforces security whitelist: strictly authorizes personal user ID for trades/callbacks."""
        if not self.allowed_chat_ids:
            logger.warning("Rejecting Telegram action: TELEGRAM_CHAT_ID is not configured in whitelist.")
            return False
        user_id = update.effective_user.id if update.effective_user else 0
        chat_id = update.effective_chat.id if update.effective_chat else 0

        # Check if any personal positive user IDs are explicitly listed in whitelist
        positive_user_ids = [uid for uid in self.allowed_chat_ids if uid > 0]

        # For action callbacks (executing trades, BE, dismiss)
        if update.callback_query:
            if positive_user_ids:
                if user_id in positive_user_ids:
                    return True
                logger.warning(
                    f"Rejecting callback from user_id={user_id} (allowed user IDs: {positive_user_ids})"
                )
                return False
            # If only negative channel/group IDs are configured in .env, allow actions from that channel
            if chat_id in self.allowed_chat_ids:
                return True
            logger.warning(
                f"Rejecting callback from user_id={user_id} in chat_id={chat_id} (allowed: {self.allowed_chat_ids})"
            )
            return False

        # For commands and messages: check user ID or whitelisted chat ID
        if positive_user_ids:
            return (user_id in positive_user_ids) or (chat_id in self.allowed_chat_ids)
        return (user_id in self.allowed_chat_ids) or (chat_id in self.allowed_chat_ids)

    @staticmethod
    async def _safe_edit_message(query, text: str, parse_mode: str = "Markdown"):
        """Safely edits message caption (for photo alerts) or message text (for text alerts)."""
        if not query.message:
            return
        try:
            if query.message.caption is not None:
                await query.edit_message_caption(caption=text, parse_mode=parse_mode)
            else:
                await query.edit_message_text(text=text, parse_mode=parse_mode)
        except Exception as e:
            logger.debug(f"Error in _safe_edit_message: {e}")

    @staticmethod
    async def _safe_answer_query(query, text: Optional[str] = None, show_alert: bool = False):
        """Safely answers a Telegram callback query without crashing if the query is expired or timed out."""
        if not query:
            return
        try:
            if text:
                await query.answer(text=text, show_alert=show_alert)
            else:
                await query.answer()
        except Exception as e:
            logger.debug(f"Ignored callback query answer error (likely expired/timeout): {e}")

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            user_id = update.effective_user.id if update.effective_user else 0
            await update.message.reply_text(
                f"⛔ **Brak uprawnień do korzystania z tego bota.**\n\n"
                f"👤 Twoje prywatne User ID: `{user_id}`\n\n"
                f"Aby autoryzować swoje konto, dopisz to ID do `TELEGRAM_CHAT_ID` w pliku `.env`:\n"
                f"`TELEGRAM_CHAT_ID=-1004455323838,{user_id}`",
                parse_mode="Markdown",
            )
            return

        acc = self.mt5_client.get_account_info()
        status_text = "🟢 Połączono z MT5" if acc else "🔴 Brak połączenia z MT5"
        balance_info = f"{acc['balance']:.2f} {acc['currency']}" if acc else "N/A"

        msg = (
            "🤖 **Forex & Gold SMC Scanner Bot** jest aktywny!\n\n"
            f"📊 **Status MT5:** {status_text}\n"
            f"💰 **Saldo konta:** `{balance_info}`\n"
            f"👤 **Twój Chat ID:** `{update.effective_user.id}`\n\n"
            "**Dostępne komendy:**\n"
            "• `/status` - Stan konta i otwarte pozycje w MT5\n"
            "• `/prop` - Dashboard ryzyka konta Prop Firm (Drawdown, bufor, limity)\n"
            "• `/be` - Przestawienie Stop Loss na Break-Even (cena wejścia)\n"
            "• `/scan` - Wymuś natychmiastowe przeskanowanie rynku\n"
            "• `/journal` - Dziennik zagrań i dotychczasowe statystyki R\n"
            "• `/notionsync` - Synchronizacja dziennika z Notion Database\n"
            "• `/optimizer` - Optymalizator BEST RR (szukanie idealnego Take Profit)\n"
            "• `/addtrade` - Ręczne dodanie zagrania do dziennika\n"
            "• `/insights` - Analiza słabych stron i mocnych zagrań (Edge Report)\n"
            "• `/adaptive` - Silnik adaptacyjny (reżimy rynku, dynamiczny BE i trailing)\n"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return

        acc = self.mt5_client.get_account_info()
        if not acc:
            await update.message.reply_text("❌ Nie można pobrać danych konta MT5.")
            return

        positions = self.mt5_client.get_open_positions()
        pos_text = ""
        total_pnl = 0.0

        if positions:
            pos_text = "\n\n📈 **Otwarte pozycje i klastry:**\n"
            from collections import defaultdict
            by_sym = defaultdict(list)
            for p in positions:
                by_sym[clean_symbol(p["symbol"])].append(p)

            for sym_clean, plist in by_sym.items():
                if len(plist) > 1:
                    cluster_pnl = sum(p["profit"] for p in plist)
                    c_emoji = "🟢" if cluster_pnl >= 0 else "🔴"
                    pos_text += f"\n🔥 **Klaster Piramidy {sym_clean}** (PnL klastra: {c_emoji} `{cluster_pnl:+.2f} {acc['currency']}`):\n"
                    for idx, p in enumerate(plist):
                        total_pnl += p["profit"]
                        p_emoji = "🟢" if p["profit"] >= 0 else "🔴"
                        tier_label = f"L{idx+1}"
                        pos_text += (
                            f"  └ [{tier_label}] #{p['ticket']} [{p['type']}] "
                            f"Lot {p['volume']} @ {p['price_open']} | SL: {p['sl']} | PnL: {p_emoji} `{p['profit']:+.2f} {acc['currency']}`\n"
                        )
                else:
                    p = plist[0]
                    total_pnl += p["profit"]
                    emoji = "🟢" if p["profit"] >= 0 else "🔴"
                    pos_text += (
                        f"• {p['symbol']} [{p['type']}] #{p['ticket']}: "
                        f"Lot {p['volume']} @ {p['price_open']} | SL: {p['sl']} | PnL: {emoji} `{p['profit']:+.2f} {acc['currency']}`\n"
                    )
        else:
            pos_text = "\n\n📈 **Brak otwartych pozycji.**"

        msg = (
            f"💼 **Stan Konta MT5 ({acc['company']}):**\n"
            f"• Login: `{acc['login']}` ({acc['server']})\n"
            f"• Saldo (Balance): `{acc['balance']:.2f} {acc['currency']}`\n"
            f"• Wycena (Equity): `{acc['equity']:.2f} {acc['currency']}`\n"
            f"• Wolny Margin: `{acc['margin_free']:.2f} {acc['currency']}`\n"
            f"• Dźwignia: `1:{acc['leverage']}`"
            f"{pos_text}\n"
            f"📊 Łączny PnL pozycji: `{total_pnl:+.2f} {acc['currency']}`"
        )
        if PROP_FIRM_MODE:
            metrics = self.mt5_client.get_prop_metrics()
            if metrics:
                msg += (
                    f"\n\n🛡️ **Tryb Prop Firm:** Aktywny\n"
                    f"• Drawdown: `-{metrics['current_dd_pct']:.2f}%` / `10.0%`\n"
                    f"• Pozostały bufor: `{metrics['remaining_buffer_usd']:.2f} {metrics['currency']}`\n"
                    f"• Slot pozycji: `{metrics['open_positions_count']}/{metrics['max_open_trades']}`"
                )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def prop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays comprehensive Prop Firm Risk Dashboard."""
        if not self._is_authorized(update):
            return

        metrics = self.mt5_client.get_prop_metrics()
        if not metrics:
            await update.message.reply_text("❌ Nie można pobrać parametrów konta MT5.")
            return

        status_icon = "🟢 BEZPIECZNY"
        if metrics["is_breached"]:
            status_icon = "🔴 PRZEKROCZONY (BREACHED)"
        elif metrics["is_safety_halt"]:
            status_icon = "🟡 OSTRZEGAWCZY (HALT)"

        open_positions = self.mt5_client.get_open_positions()
        total_pnl = sum(p["profit"] for p in open_positions)
        risk_per_trade_usd = metrics["initial_balance"] * (metrics["risk_per_trade_pct"] / 100.0)

        msg = (
            "🛡️ **PROP FIRM RISK DASHBOARD**\n\n"
            f"🚥 **Status Konta:** `{status_icon}`\n"
            f"💵 **Początkowy Balans:** `{metrics['initial_balance']:.2f} {metrics['currency']}`\n"
            f"💰 **Aktualne Saldo:** `{metrics['current_balance']:.2f} {metrics['currency']}`\n"
            f"📊 **Aktualne Equity:** `{metrics['current_equity']:.2f} {metrics['currency']}`\n\n"
            "📉 **Statystyki Drawdownu (Static DD):**\n"
            f"• Aktualna strata: `-{metrics['current_dd_pct']:.2f}%` (`-{metrics['current_loss_usd']:.2f} {metrics['currency']}`)\n"
            f"• Maksymalny dozwolony Drawdown: `10.0%` (`{metrics['max_loss_usd']:.2f} {metrics['currency']}`)\n"
            f"• Poziom krytyczny konta: `{metrics['breach_level_usd']:.2f} {metrics['currency']}`\n"
            f"• Pozostały bufor bezpieczeństwa: `{metrics['remaining_buffer_usd']:.2f} {metrics['currency']}` (`{metrics['remaining_buffer_pct']:.2f}%`)\n\n"
            "📦 **Ekspozycja i Ryzyko:**\n"
            f"• Otwarte pozycje: `{metrics['open_positions_count']} / {metrics['max_open_trades']}`\n"
            f"• Ryzyko na transakcję: `{metrics['risk_per_trade_pct']}%` (~`{risk_per_trade_usd:.2f} {metrics['currency']}`)\n"
            f"• Niezrealizowany PnL: `{total_pnl:+.2f} {metrics['currency']}`\n\n"
            "💡 Użyj komendy `/be`, aby przestawiać zyskowne pozycje na Break-Even!"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def scan_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return

        await update.message.reply_text("🔍 Rozpoczynam natychmiastowe skanowanie rynku SMC...")
        if self.scanner_callback:
            found_count = await self.scanner_callback()
            await update.message.reply_text(f"✅ Skanowanie zakończone. Wykryte nowe setupy: {found_count}")
        else:
            await update.message.reply_text("ℹ️ Pętla skanera działa automatycznie w tle.")

    async def journal_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return

        stats = self.journal.get_summary_stats()
        recent = self.journal.get_recent_trades(limit=6)

        recent_lines = ""
        for t in recent:
            if t.get("status") == "OPEN":
                icon = "⏳"
                res_str = "W toku"
            elif t["realized_r"] > 0:
                icon = "🟢"
                res_str = f"{t['realized_r']:+.2f}R"
            elif t["realized_r"] < 0:
                icon = "🔴"
                res_str = f"{t['realized_r']:+.2f}R"
            else:
                icon = "🛡️"
                res_str = "0.0R (BE)"
            pyr_tag = f" 🔥[Pyr L{t.get('pyramid_level', 2)}]" if t.get("is_pyramid") else ""
            recent_lines += (
                f"• #{t['id']} {t['symbol']} [{t['direction']}]{pyr_tag}: "
                f"{icon} `{res_str}` | Max R: `{t['max_r']:.2f}R` | Confluence: `{t['confluence_score']}%`\n"
            )

        notion_status = ""
        if ENABLE_NOTION and self.notion.is_configured():
            notion_status = "🌐 **Notion:** 🟢 Aktywny (Automatyczna synchronizacja)\n\n"
        elif self.notion.is_configured():
            notion_status = "🌐 **Notion:** ⚪ Skonfigurowany (Wpisz `/notionsync` lub włącz `ENABLE_NOTION=True`)\n\n"
        else:
            notion_status = "🌐 **Notion:** ⚪ Niepołączony (Wpisz `/notionsync` po instrukcję)\n\n"

        msg = (
            "📓 **DZIENNIK TRANZAKCJI (SMC JOURNAL)**\n\n"
            f"📊 **Statystyki łączne:**\n"
            f"• Liczba pozycji: `{stats['total']}` (Wygrane: `{stats['wins']}`, Przegrane: `{stats['losses']}`)\n"
            f"• Skuteczność (Win Rate): `{stats['winrate']}%`\n"
            f"• Łączny wygenerowany zysk: `{stats['net_r']:+.2f}R`\n"
            f"• Średni zysk na transakcję: `{stats['avg_r']:+.2f}R`\n\n"
            f"{notion_status}"
            "📋 **Ostatnie transakcje:**\n"
            f"{recent_lines}\n"
            "💡 Wpisz `/optimizer`, aby obliczyć Take Profit, lub `/notionsync` dla bazy Notion!"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def notionsync_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Syncs all historical or unsynced trades between MT5, trades.db, and Notion."""
        if not self._is_authorized(update):
            return

        if not self.notion.is_configured():
            help_msg = (
                "📓 **INTEGRACJA NOTION (TRADE JOURNAL)**\n\n"
                "System obsługuje automatyczne zapisywanie każdego tradu w Notion!\n\n"
                "**Aby połączyć Notion w 2 minuty:**\n"
                "1. Wejdź na: [notion.so/profile/integrations](https://www.notion.so/profile/integrations)\n"
                "2. Kliknij **'New integration'**, nazwij np. `Forex-SMC` i kliknij **Save**.\n"
                "3. Skopiuj **'Internal Integration Secret'** (`secret_...`) do `.env` jako:\n"
                "   `NOTION_API_KEY=secret_twoj_klucz`\n"
                "4. Stwórz tabelę/bazę w Notion lub użyj gotowego szablonu.\n"
                "5. W Notion kliknij **••• (prawy górny róg) -> Connections -> Dodaj Forex-SMC**.\n"
                "6. Skopiuj 32-znakowe ID z linku bazy do `.env` jako:\n"
                "   `NOTION_DATABASE_ID=twoj_database_id`\n"
                "7. Ustaw `ENABLE_NOTION=True` w `.env` i wpisz tutaj `/notionsync`!"
            )
            await update.message.reply_text(help_msg, parse_mode="Markdown")
            return

        await update.message.reply_text("🔄 Rozpoczynam pełną synchronizację (MT5 <--> trades.db <--> Notion)...")
        try:
            from sync_journal import sync_all
            await sync_all()
            await update.message.reply_text(
                "🎉 **Pełna synchronizacja zakończona sukcesem!**\n\n"
                "✅ Wszystkie transakcje z terminala MT5, bazy lokalnej trades.db oraz Notion są teraz w 100% zgodne.\n"
                "Wpisz `/status` lub `/journal`, aby sprawdzić aktualne podsumowanie."
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Błąd podczas synchronizacji: {e}")

    async def optimizer_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return

        trades = self.journal.get_all_trades()
        if len(trades) < 2:
            await update.message.reply_text("⚠️ Zbyt mało transakcji w bazie. Wymagane minimum 2 pozycje.")
            return

        report_msg = RROptimizer.format_optimizer_report(
            RROptimizer.calculate_best_rr(trades)
        )
        await update.message.reply_text(report_msg, parse_mode="Markdown")

    async def adaptive_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays status and statistics of Adaptive Position Management engine."""
        if not self._is_authorized(update):
            return

        from config import (
            ENABLE_ADAPTIVE_MANAGER,
            ADAPTIVE_MODE,
            ADAPTIVE_MIN_REGIME_SAMPLES,
            ADAPTIVE_M15_TRAILING,
            ADAPTIVE_BE_TREND_R,
            ADAPTIVE_BE_CHOP_R,
            ADAPTIVE_BE_VOLATILE_R,
        )

        all_trades = self.journal.get_all_trades()
        trend_trades = [t for t in all_trades if str(t.get("regime", "")).upper() == "STRONG_TREND"]
        chop_trades = [t for t in all_trades if str(t.get("regime", "")).upper() == "COMPRESSION"]
        vol_trades = [t for t in all_trades if str(t.get("regime", "")).upper() == "HIGH_VOLATILITY"]

        msg = (
            f"🧠 **ADAPTIVE POSITION MANAGER**\n\n"
            f"⚙️ **Status modułu:** `{'WŁĄCZONY' if ENABLE_ADAPTIVE_MANAGER else 'WYŁĄCZONY'}`\n"
            f"🎯 **Tryb egzekucji:** `{ADAPTIVE_MODE}` ({'Modyfikacja MT5 aktywna' if ADAPTIVE_MODE == 'LIVE' else 'Telemetria bez dotykania zleceń'})\n"
            f"📐 **M15 Trailing SL:** `{'WŁĄCZONY' if ADAPTIVE_M15_TRAILING else 'WYŁĄCZONY'}`\n\n"
            f"📊 **Parametry Reżimów Rynkowych:**\n"
            f"• 🟢 **STRONG_TREND:** BE: `+{ADAPTIVE_BE_TREND_R:.2f}R` | Próba w bazie: `{len(trend_trades)}/{ADAPTIVE_MIN_REGIME_SAMPLES}` | Piramida: `DOZWOLONA`\n"
            f"• 🟡 **COMPRESSION:** BE: `+{ADAPTIVE_BE_CHOP_R:.2f}R` | Próba w bazie: `{len(chop_trades)}/{ADAPTIVE_MIN_REGIME_SAMPLES}` | Piramida: `ZABLOKOWANA`\n"
            f"• 🔴 **HIGH_VOLATILITY:** BE: `+{ADAPTIVE_BE_VOLATILE_R:.2f}R` | Próba w bazie: `{len(vol_trades)}/{ADAPTIVE_MIN_REGIME_SAMPLES}` | Piramida: `ZABLOKOWANA`\n\n"
            f"💡 *Gdy próbka w reżimie osiągnie {ADAPTIVE_MIN_REGIME_SAMPLES} zagrań, Take Profit jest optymalizowany automatycznie pod kątem najwyższego EV.*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def addtrade_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Allows manual addition of trades from phone: /addtrade XAUUSD BUY YES 1.0 2.8"""
        if not self._is_authorized(update):
            return

        args = context.args
        if not args or len(args) < 4:
            help_msg = (
                "ℹ️ **Instrukcja dodawania transakcji:**\n"
                "`/addtrade <SYMBOL> <BUY/SELL> <YES/NO> <REALIZED_R> <MAX_R>`\n\n"
                "**Przykład:**\n"
                "`/addtrade XAUUSD BUY YES 1.0 2.8`\n"
                "*(Wygrana długa pozycja na złocie zamknięta przy +1.0R, max peak MFE 2.8R)*"
            )
            await update.message.reply_text(help_msg, parse_mode="Markdown")
            return

        try:
            symbol = args[0].upper()
            if len(args) >= 5:
                direction = "BUY" if "B" in args[1].upper() else "SELL"
                is_win = args[2].upper() in ("YES", "Y", "TAK", "WIN", "1")
                realized_r = float(args[3].replace(",", "."))
                max_r = float(args[4].replace(",", "."))
            else:
                # 4 arguments backward compatibility
                if args[1].upper() in ("BUY", "SELL"):
                    direction = args[1].upper()
                    realized_r = float(args[2].replace(",", "."))
                    max_r = float(args[3].replace(",", "."))
                    is_win = realized_r > 0
                else:
                    direction = "MANUAL"
                    is_win = args[1].upper() in ("YES", "Y", "TAK", "WIN", "1")
                    realized_r = float(args[2].replace(",", "."))
                    max_r = float(args[3].replace(",", "."))

            status = "WIN" if is_win else "LOSS"

            trade_id = self.journal.add_trade(
                symbol=symbol,
                direction=direction,
                entry_price=0.0,
                sl_price=0.0,
                tp_price=0.0,
                realized_r=realized_r,
                max_r=max_r,
                confluence_score=80,
                status=status,
                comment=f"Dodano ręcznie przez Telegram [{direction}]",
            )
            await update.message.reply_text(
                f"✅ Dodano transakcję #{trade_id} do dziennika!\n"
                f"• {symbol} [{direction}] [{status}]: Wynik `{realized_r:+.1f}R` | Max R: `{max_r:.1f}R`\n\n"
                f"Wpisz `/optimizer`, aby zobaczyć zaktualizowany Best RR!"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Błąd zapisu: {e}")

    async def insights_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generates and sends trade insights report analyzing weak setups."""
        if not self._is_authorized(update):
            return

        if not self.insight_engine:
            await update.message.reply_text("ℹ️ Silnik analityczny jest wyłączony. Ustaw `ENABLE_DYNAMIC_EDGE_FILTER=True` w `.env`.")
            return

        await update.message.reply_text("🔍 Analizuję historię transakcji...")
        report = self.insight_engine.generate_insights_report()

        # Split long reports for Telegram's 4096 char limit
        if len(report) <= 4096:
            await update.message.reply_text(report, parse_mode="Markdown")
        else:
            parts = report.split("\n\n")
            chunk = ""
            for part in parts:
                if len(chunk) + len(part) + 2 > 4000:
                    await update.message.reply_text(chunk, parse_mode="Markdown")
                    chunk = part
                else:
                    chunk = chunk + "\n\n" + part if chunk else part
            if chunk:
                await update.message.reply_text(chunk, parse_mode="Markdown")

    async def be_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Moves Stop Loss of open position to Break-Even."""
        if not self._is_authorized(update):
            return

        if context.args:
            try:
                ticket_num = int(context.args[0])
                ok, msg = self.mt5_client.move_sl_to_breakeven(ticket_num)
                await update.message.reply_text(msg, parse_mode="Markdown")
                return
            except ValueError:
                await update.message.reply_text("❌ Podaj poprawny numer ticketu, np. `/be 105626168`", parse_mode="Markdown")
                return

        positions = self.mt5_client.get_open_positions()
        if not positions:
            await update.message.reply_text("ℹ️ Brak otwartych pozycji do przestawienia na Break-Even.")
            return

        keyboard = []
        for p in positions:
            keyboard.append([
                InlineKeyboardButton(
                    f"🛡️ Przestaw BE: {p['symbol']} #{p['ticket']} (PnL: {p['profit']:+.2f})",
                    callback_data=f"be:{p['ticket']}",
                )
            ])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Wybierz pozycję, którą chcesz przestawić na Break-Even (SL = cena wejścia):",
            reply_markup=reply_markup,
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query

        if not self._is_authorized(update):
            user_id = update.effective_user.id if update.effective_user else 0
            await self._safe_answer_query(
                query,
                text=f"⛔ Brak uprawnień do wykonania tego zlecenia!\nTwoje User ID to: {user_id}.\nDopisz je do TELEGRAM_CHAT_ID w pliku .env",
                show_alert=True,
            )
            return

        await self._safe_answer_query(query)

        data = query.data or ""
        parts = data.split(":")
        action = parts[0]
        setup_id = parts[1] if len(parts) > 1 else ""

        if action == "be":
            try:
                ticket_num = int(setup_id)
                ok, msg = self.mt5_client.move_sl_to_breakeven(ticket_num)
                await self._safe_answer_query(query, text=msg[:190], show_alert=True)
                if ok and query.message:
                    await query.message.reply_text(msg, parse_mode="Markdown")
            except Exception as e:
                await self._safe_answer_query(query, text=f"Błąd BE: {e}", show_alert=True)
            return

        setup = self.pending_setups.pop(setup_id, None)
        self.pending_setups_time.pop(setup_id, None)

        orig_caption = (
            query.message.caption
            if (query.message and query.message.caption is not None)
            else (query.message.text if query.message else "")
        )

        if action == "dismiss":
            mute_txt = ""
            if setup:
                self.mute_symbol(setup.symbol, setup.direction, hours=DISMISS_MUTE_HOURS)
                mute_txt = f"\n\n🔇 **Powiadomienia dla {setup.symbol} [{setup.direction}] wyciszone na {DISMISS_MUTE_HOURS:.1f}h.**"

            await self._safe_edit_message(query, f"{orig_caption}\n\n❌ **Setup odrzucony przez użytkownika.**{mute_txt}")
            return

        if not setup:
            await self._safe_edit_message(query, f"{orig_caption}\n\n⚠️ **Ten setup wygasł lub został już przetworzony.**")
            return

        if action in ("market", "limit"):
            from config import BLOCK_ON_HIGH_IMPACT_NEWS, BLOCK_ROLLOVER_SESSION, PROP_ENFORCE_NEWS_FILTER
            from session_manager import SessionManager

            if BLOCK_ROLLOVER_SESSION and action == "market":
                curr_session = SessionManager.get_current_session()
                if "Rollover" in curr_session.get("session_name", ""):
                    await self._safe_answer_query(
                        query,
                        text="⛔ Zablokowano: Sesja Rollover (niska płynność i szerokie spready)!",
                        show_alert=True,
                    )
                    await self._safe_edit_message(
                        query,
                        f"{orig_caption}\n\n🛑 **Zlecenie Market zablokowane (Sesja Rollover / Low Liquidity):**\n"
                        f"Wstrzymano natychmiastowe wejście rynkowe ze względu na brokerski rollover i rozszerzone spready."
                    )
                    return

            should_block_news = BLOCK_ON_HIGH_IMPACT_NEWS or (PROP_FIRM_MODE and PROP_ENFORCE_NEWS_FILTER)
            if should_block_news:
                # Live news verification at the exact time of order placement (async non-blocking)
                has_news, news_warn, _ = await self.news_filter.check_news_async(setup.symbol)
                if has_news and news_warn:
                    await self._safe_answer_query(query, text="Zablokowano: zbliżają się wiadomości High-Impact!", show_alert=True)
                    await self._safe_edit_message(
                        query,
                        f"{orig_caption}\n\n🛑 **Zlecenie zablokowane na żywo (Filtr Wiadomości High-Impact):**\n{news_warn}"
                    )
                    return

            if PROP_FIRM_MODE:
                sl_dist = abs(setup.entry_price - setup.sl_price)
                can_trade, reason = self.mt5_client.can_open_new_trade(
                    setup.symbol,
                    sl_distance=sl_dist,
                    is_pyramid=getattr(setup, "is_pyramid", False),
                )
                if not can_trade:
                    await self._safe_answer_query(query, text=reason[:190], show_alert=True)
                    await self._safe_edit_message(
                        query,
                        f"{orig_caption}\n\n🛑 **Zlecenie zablokowane (Prop Firm Guardian):**\n{reason}"
                    )
                    return

        risk_pct = PROP_RISK_PER_TRADE_PERCENT if PROP_FIRM_MODE else DEFAULT_RISK_PERCENT
        # Edge filter risk reduction
        if hasattr(setup, "_risk_multiplier") and setup._risk_multiplier < 1.0 and setup._risk_multiplier > 0:
            risk_pct = risk_pct * setup._risk_multiplier
            logger.info(f"Edge filter reduced risk to {risk_pct:.2f}% for {setup.symbol}")

        is_pyr = 1 if getattr(setup, "is_pyramid", False) else 0
        parent_t = getattr(setup, "parent_ticket", None)
        pyr_level = getattr(setup, "pyramid_level", 2) if is_pyr else 1

        # Strict Risk-Neutral volume sizing for pyramiding
        if is_pyr and hasattr(setup, "_pyramid_lot") and setup._pyramid_lot > 0:
            lot = setup._pyramid_lot
        elif is_pyr:
            pyr_status = self.mt5_client.get_pyramid_cluster_status(setup.symbol)
            calc_lot, _, _, _ = self.mt5_client.calculate_pyramid_position_size(
                symbol=setup.symbol,
                entry_price=setup.entry_price,
                sl_price=setup.sl_price,
                setup_grade=getattr(setup, "grade", "A"),
                cluster_status=pyr_status,
            )
            lot = calc_lot if calc_lot > 0 else self.mt5_client.calculate_lot_size(setup.symbol, risk_pct, setup.sl_price, setup.entry_price)
        else:
            lot = self.mt5_client.calculate_lot_size(setup.symbol, risk_pct, setup.sl_price, setup.entry_price)

        strat_label = f"PYRAMID_L{pyr_level}_{setup.strategy_name}" if is_pyr else setup.setup_type

        raw_strat = getattr(setup, "strategy_name", "SMC")
        strat_upper = str(raw_strat).upper()
        if "ZUMIKO" in strat_upper or "KILLMLM" in strat_upper:
            strat_name_db = "Zumiko KillMLM"
            strat_prefix = "Zumiko"
        elif "GOLDEN" in strat_upper:
            strat_name_db = "GOLDEN_COMBO"
            strat_prefix = "GoldCombo"
        else:
            strat_name_db = "SMC"
            strat_prefix = "SMC"

        grade_val = getattr(setup, "grade", "")
        if not grade_val and setup.confluence_score > 0:
            if strat_name_db == "Zumiko KillMLM":
                grade_val = "A+" if setup.confluence_score >= 90 else ("A" if setup.confluence_score >= 75 else "B")
            else:
                grade_val = "A+" if setup.confluence_score >= 85 else ("A" if setup.confluence_score >= 70 else "B")

        if action == "market":
            comment_str = f"{strat_prefix} {'Pyr L' + str(pyr_level) + ' ' if is_pyr else ''}{setup.setup_type[:10]}"[:31]
            success, msg, ticket, fill_price = self.mt5_client.execute_market_order(
                symbol=setup.symbol,
                direction=setup.direction,
                volume=lot,
                sl=setup.sl_price,
                tp=setup.tp1_price,
                comment=comment_str,
            )
            if success:
                actual_entry = fill_price if fill_price > 0 else setup.entry_price
                trail_msg = ""
                if is_pyr and PYRAMID_LOCK_PREVIOUS_PROFIT:
                    sync_results = self.mt5_client.sync_cluster_stop_loss(
                        symbol=setup.symbol,
                        new_sl=setup.sl_price,
                    )
                    synced_count = sum(1 for _, ok, _ in sync_results if ok)
                    trail_msg = f"\n🔒 **Zsynchronizowano Stop Loss klastra:** {synced_count} pozycji przestawionych na `{setup.sl_price}`"

                self.journal.add_trade(
                    symbol=setup.symbol,
                    direction=setup.direction,
                    entry_price=actual_entry,
                    sl_price=setup.sl_price,
                    tp_price=setup.tp1_price,
                    realized_r=0.0,
                    max_r=0.0,
                    confluence_score=setup.confluence_score,
                    status="OPEN",
                    mt5_ticket=ticket,
                    comment=f"{'Piramida L' + str(pyr_level) if is_pyr else 'Market order'} via Telegram | {setup.setup_type}",
                    parent_ticket=parent_t,
                    is_pyramid=is_pyr,
                    pyramid_level=pyr_level,
                    strategy=strat_name_db,
                    setup_type=setup.setup_type,
                    grade=grade_val,
                    regime=getattr(setup, "regime", ""),
                )
                asyncio.create_task(self._log_trade_to_notion({
                    "symbol": setup.symbol,
                    "direction": setup.direction,
                    "entry_price": actual_entry,
                    "sl_price": setup.sl_price,
                    "tp_price": setup.tp1_price,
                    "realized_r": 0.0,
                    "max_r": 0.0,
                    "confluence_score": setup.confluence_score,
                    "status": "OPEN",
                    "mt5_ticket": ticket,
                    "strategy": strat_name_db,
                    "grade": grade_val,
                    "setup_type": setup.setup_type,
                    "comment": f"{strat_label} | via Telegram",
                }))
                new_caption = (
                    f"{orig_caption}\n\n"
                    f"✅ **ZLECENIE WYKONANE W MT5!**\n"
                    f"🎫 **Ticket:** `#{ticket}`\n"
                    f"📍 **Cena egzekucji:** `{actual_entry}`\n"
                    f"📦 **Wolumen:** `{lot} lot` (Ryzyko: {risk_pct}%)\n"
                    f"🎯 **SL:** `{setup.sl_price}` | **TP:** `{setup.tp1_price}`"
                    f"{trail_msg}"
                )
                await self._safe_edit_message(query, new_caption)
            else:
                await self._safe_edit_message(
                    query,
                    f"{orig_caption}\n\n❌ **Błąd wykonania w MT5:** {msg}"
                )

        elif action == "limit":
            comment_str = f"{strat_prefix} Limit {'Pyr L' + str(pyr_level) + ' ' if is_pyr else ''}{setup.setup_type[:10]}"[:31]
            success, msg, ticket, _ = self.mt5_client.execute_limit_order(
                symbol=setup.symbol,
                direction=setup.direction,
                price=setup.entry_price,
                volume=lot,
                sl=setup.sl_price,
                tp=setup.tp1_price,
                comment=comment_str,
            )
            if success:
                # Audit fix: Pending limit orders start as PENDING in journal!
                self.journal.add_trade(
                    symbol=setup.symbol,
                    direction=setup.direction,
                    entry_price=setup.entry_price,
                    sl_price=setup.sl_price,
                    tp_price=setup.tp1_price,
                    realized_r=0.0,
                    max_r=0.0,
                    confluence_score=setup.confluence_score,
                    status="PENDING",
                    mt5_ticket=ticket,
                    comment=f"{'Piramida L' + str(pyr_level) + ' Limit' if is_pyr else 'Limit order'} via Telegram | {setup.setup_type}",
                    parent_ticket=parent_t,
                    is_pyramid=is_pyr,
                    pyramid_level=pyr_level,
                    strategy=strat_name_db,
                    setup_type=setup.setup_type,
                    grade=grade_val,
                    regime=getattr(setup, "regime", ""),
                )
                asyncio.create_task(self._log_trade_to_notion({
                    "symbol": setup.symbol,
                    "direction": setup.direction,
                    "entry_price": setup.entry_price,
                    "sl_price": setup.sl_price,
                    "tp_price": setup.tp1_price,
                    "realized_r": 0.0,
                    "max_r": 0.0,
                    "confluence_score": setup.confluence_score,
                    "status": "PENDING",
                    "mt5_ticket": ticket,
                    "strategy": strat_name_db,
                    "grade": grade_val,
                    "setup_type": setup.setup_type,
                    "comment": f"{strat_label} (Limit) | via Telegram",
                }))
                new_caption = (
                    f"{orig_caption}\n\n"
                    f"🟡 **ZLECENIE LIMIT ZŁOŻONE W MT5!**\n"
                    f"🎫 **Ticket:** `#{ticket}`\n"
                    f"📍 **Cena Limit:** `{setup.entry_price}`\n"
                    f"📦 **Wolumen:** `{lot} lot`"
                )
                await self._safe_edit_message(query, new_caption)
            else:
                await self._safe_edit_message(
                    query,
                    f"{orig_caption}\n\n❌ **Błąd zlecenia limit w MT5:** {msg}"
                )

    async def send_setup_alert(self, setup: TradeSetup, chart_path: str, setup_id: str):
        """Sends rich alert with chart image and interactive execution buttons."""
        if not self.app or not self.alert_channel:
            logger.warning("Telegram Bot application not running or alert channel not set.")
            return

        self._cleanup_expired_setups()
        self.pending_setups[setup_id] = setup
        self.pending_setups_time[setup_id] = time.time()

        dir_emoji = "🟢 BUY (DŁUGA)" if setup.direction == "BUY" else "🔴 SELL (KRÓTKA)"
        digits = 2 if "JPY" in setup.symbol or "XAU" in setup.symbol else 4
        point_scale = 0.01 if digits == 2 else 0.0001
        sl_pips = abs(setup.entry_price - setup.sl_price) / point_scale

        fibo_text = ""
        if setup.fibo_618 > 0:
            fibo_text = f"📐 **Fibo OTE:** 61.8%: `{setup.fibo_618:.{digits}f}` | 78.6%: `{setup.fibo_786:.{digits}f}`\n"

        # Confluence Breakdown Formatting (compact top items)
        confluence_lines = ""
        if setup.confluence_breakdown:
            confluence_lines = "📋 **Kluczowe koniunkcje:**\n"
            achieved_items = [item for item in setup.confluence_breakdown if item.achieved][:4]
            if not achieved_items:
                achieved_items = setup.confluence_breakdown[:3]
            for item in achieved_items:
                confluence_lines += f"• ✅ `[+{item.weight}%]` {item.factor}\n"
            confluence_lines += "\n"

        session_badge = f"🕒 **Sesja:** `{setup.session_name}`" + (" ⚡" if setup.is_killzone else "")

        news_banner = ""
        if setup.news_warning:
            news_banner = f"⚠️ {setup.news_warning}\n\n"

        grade_emoji = "🔥 [Grade A+ Setup]" if setup.grade == "A+" else "⭐ [Grade A Setup]"
        rr_badge = "🔥 [Best RR]" if setup.is_optimal_rr else "⚙️ [Domyślne]"
        rr_src = f"\n💡 `{setup.rr_source_info}`" if setup.rr_source_info else ""

        regime_badge = ""
        setup_regime = getattr(setup, "regime", "")
        if setup_regime:
            setup_be = getattr(setup, "dynamic_be_trigger", 0.85)
            regime_badge = f"🧠 **Reżim rynkowy:** `[{setup_regime}]` (Dynamic BE: `+{setup_be:.2f}R`)\n"

        # Prop Firm metrics for alert
        risk_pct = PROP_RISK_PER_TRADE_PERCENT if PROP_FIRM_MODE else DEFAULT_RISK_PERCENT
        acc = self.mt5_client.get_account_info()
        bal = acc['balance'] if acc else PROP_INITIAL_BALANCE
        curr = acc['currency'] if acc else "USD"
        risk_usd = bal * (risk_pct / 100.0)

        prop_risk_line = ""
        if PROP_FIRM_MODE:
            open_count = len(self.mt5_client.get_open_positions())
            prop_risk_line = f"🛡️ **Prop Risk:** `{risk_pct}%` (`~{risk_usd:.2f} {curr}`) | Slot: `{open_count}/{PROP_MAX_OPEN_TRADES}`\n"

        # Strategy title & Pyramiding Scale-In Info
        is_pyr = getattr(setup, "is_pyramid", False)
        strat_name = getattr(setup, "strategy_name", "SMC")
        pyr_level = getattr(setup, "pyramid_level", 2) if is_pyr else 1
        pyramid_banner = ""
        if is_pyr:
            header_title = f"🔥 **DOKŁADKA DO POZYCJI (PIRAMIDA L{pyr_level}): {setup.symbol}**"
            locked_usd = getattr(setup, "cluster_locked_profit_usd", 0.0)
            pyr_lot_info = f" | Obliczony wolumen: `{setup._pyramid_lot:.2f} lot`" if hasattr(setup, "_pyramid_lot") else ""
            pyramid_banner = (
                f"🛡️ **Klaster Piramidy Poziom L{pyr_level}** (Baza: #{setup.parent_ticket})\n"
                f"🔒 **Zablokowany zysk klastra:** `+{setup.pyramid_profit_r:.1f}R` (`+${locked_usd:.2f}`){pyr_lot_info}\n"
                f"📍 **Nowy zsynchronizowany SL:** `{setup.sl_price:.{digits}f}` (Dla wszystkich pozycji)\n"
                f"🎯 **Master TP:** `{setup.tp1_price:.{digits}f}` (Wspólny cel klastra)\n"
                f"⚖️ **Ryzyko netto portfela:** `ZEROWE` (Strict Risk-Neutral Scale-In)\n\n"
            )
        elif strat_name == "GOLDEN_COMBO":
            header_title = f"🔥 **ZŁOTY SETUP (SMC + ZUMIKO): {setup.symbol}**"
        elif strat_name == "ZUMIKO":
            header_title = f"⚡ **ZUMIKO KILLMLM SETUP: {setup.symbol}**"
        else:
            header_title = f"🔔 **SMC SETUP: {setup.symbol}**"

        # Multi-timeframe trend table (single clean line)
        mtf_text = ""
        if getattr(setup, "mtf_table", None):
            mtf_m15 = setup.mtf_table.get("M15", "NEUTRAL")
            mtf_h1 = setup.mtf_table.get("H1", "NEUTRAL")
            mtf_h4 = setup.mtf_table.get("H4", "NEUTRAL")
            m15_i = "🟢" if mtf_m15 == "BUY" else ("🔴" if mtf_m15 == "SELL" else "⚪")
            h1_i = "🟢" if mtf_h1 == "BUY" else ("🔴" if mtf_h1 == "SELL" else "⚪")
            h4_i = "🟢" if mtf_h4 == "BUY" else ("🔴" if mtf_h4 == "SELL" else "⚪")
            mtf_text = f"📊 **Trend MTF:** M15: {m15_i} ({mtf_m15}) | H1: {h1_i} ({mtf_h1}) | H4: {h4_i} ({mtf_h4})\n"

        # Zumiko indicators line
        zumiko_text = ""
        if getattr(setup, "ema_238", 0.0) > 0:
            zumiko_text = f"📈 **Zumiko:** EMA 10/21: `{setup.ema_fast:.{digits}f}/{setup.ema_slow:.{digits}f}` | 🌟 EMA 238: `{setup.ema_238:.{digits}f}` | {setup.swing_pattern}\n"

        # Swept Liquidity highlight line
        liq_line = ""
        swept_levels = getattr(setup, "swept_levels", [])
        if swept_levels:
            swept_names = [f"**{s.get('name')}** (`{s.get('price'):.{digits}f}`)" for s in swept_levels]
            multi_tag = " 🌟 [MULTI-SWEEP]" if getattr(setup, "is_multi_sweep", False) else ""
            liq_line = f"⚡ **Wymieciona płynność:** {', '.join(swept_names)}{multi_tag}\n"
        elif getattr(setup, "liquidity_summary", ""):
            liq_line = f"⚡ **Płynność:** {setup.liquidity_summary}\n"

        sl_mode_txt = f"[{setup.sl_mode}] " if getattr(setup, "sl_mode", "") else ""
        spread_info = f" [w tym spread: +{setup.spread_used / point_scale:.1f}p]" if getattr(setup, "spread_used", 0.0) > 0 else ""

        # Edge Filter Warning Badge
        edge_warning_text = ""
        if hasattr(setup, "_edge_warnings") and setup._edge_warnings:
            edge_warning_text = "\n⚠️ **OSTRZEŻENIE EDGE FILTRA:**\n"
            for w in setup._edge_warnings[:3]:
                edge_warning_text += f"{w}\n"
            if hasattr(setup, "_risk_multiplier") and setup._risk_multiplier < 1.0:
                edge_warning_text += f"📉 _Ryzyko automatycznie zredukowane o {int((1-setup._risk_multiplier)*100)}%_\n"
            edge_warning_text += "\n"

        caption = (
            f"{header_title}\n"
            f"🎯 **Ocena:** `{setup.confluence_score}%` {grade_emoji}\n\n"
            f"{pyramid_banner}"
            f"{news_banner}"
            f"{edge_warning_text}"
            f"📊 **Kierunek:** {dir_emoji}\n"
            f"⏱ **Interwał:** `{setup.timeframe}` (H4: **{setup.htf_trend}**) | {session_badge}\n"
            f"{regime_badge}"
            f"🎯 **Strategia:** `{setup.setup_type}`\n\n"
            f"{mtf_text}"
            f"{zumiko_text}"
            f"{fibo_text}"
            f"{liq_line}"
            f"{confluence_lines}"
            f"📍 **Entry:** `{setup.entry_price:.{digits}f}` | 🛑 **SL:** `{setup.sl_price:.{digits}f}` ({sl_mode_txt}{sl_pips:.1f} p{spread_info})\n"
            f"🎯 **TP1:** `{setup.tp1_price:.{digits}f}` (Target 1:{setup.rr_ratio})\n"
            f"🎯 **TP2:** `{setup.tp2_price:.{digits}f}` (Strukturalny szczyt/dołek)\n"
            f"⚖️ **Risk / Reward:** `1 : {setup.rr_ratio}` {rr_badge}\n"
            f"{prop_risk_line}"
            f"{rr_src}"
        )

        btn_market_text = (
            f"🔥 Wejdź MARKET (Dokładka L{pyr_level})"
            if is_pyr
            else f"🟢 Wejdź Market ({risk_pct}% ryzyka)"
        )
        btn_limit_text = (
            f"⏳ Zlecenie LIMIT (Dokładka L{pyr_level})"
            if is_pyr
            else f"🟡 Złóż Limit na {setup.entry_price:.{digits}f}"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    btn_market_text,
                    callback_data=f"market:{setup_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    btn_limit_text,
                    callback_data=f"limit:{setup_id}",
                ),
                InlineKeyboardButton("❌ Pomiń", callback_data=f"dismiss:{setup_id}"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if not self.app or not self.app.bot:
            logger.warning(f"Telegram Bot is not connected. Alert for {setup.symbol} saved locally only.")
            return

        try:
            target_chat = self.alert_channel
            if chart_path and os.path.exists(chart_path):
                with open(chart_path, "rb") as photo:
                    if len(caption) <= 1024:
                        try:
                            await self.app.bot.send_photo(
                                chat_id=target_chat,
                                photo=photo,
                                caption=caption,
                                parse_mode="Markdown",
                                reply_markup=reply_markup,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send photo with Markdown, retrying plain text: {e}")
                            photo.seek(0)
                            await self.app.bot.send_photo(
                                chat_id=target_chat,
                                photo=photo,
                                caption=caption,
                                parse_mode=None,
                                reply_markup=reply_markup,
                            )
                    else:
                        # Truncate cleanly at 1000 characters to never exceed Telegram 1024 limit
                        safe_caption = caption[:1000].rsplit("\n", 1)[0]
                        if safe_caption.count("`") % 2 != 0:
                            safe_caption += "`"
                        if safe_caption.count("**") % 2 != 0:
                            safe_caption += "**"
                        if safe_caption.count("*") % 2 != 0:
                            safe_caption += "*"
                        if safe_caption.count("_") % 2 != 0:
                            safe_caption += "_"
                        try:
                            msg = await self.app.bot.send_photo(
                                chat_id=target_chat,
                                photo=photo,
                                caption=safe_caption,
                                parse_mode="Markdown",
                                reply_markup=reply_markup,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send truncated photo alert with Markdown: {e}")
                            photo.seek(0)
                            msg = await self.app.bot.send_photo(
                                chat_id=target_chat,
                                photo=photo,
                                caption=safe_caption,
                                parse_mode=None,
                                reply_markup=reply_markup,
                            )
                        # Send full specification as follow-up reply (capped to fit Telegram limit)
                        follow_up = f"📋 **Szczegóły setupu:**\n\n{caption}"
                        if len(follow_up) > 4000:
                            follow_up = follow_up[:3996] + "..."
                        try:
                            await self.app.bot.send_message(
                                chat_id=target_chat,
                                text=follow_up,
                                parse_mode="Markdown",
                                reply_to_message_id=msg.message_id,
                            )
                        except Exception:
                            await self.app.bot.send_message(
                                chat_id=target_chat,
                                text=follow_up,
                                parse_mode=None,
                                reply_to_message_id=msg.message_id,
                            )
            else:
                if len(caption) <= 4096:
                    try:
                        await self.app.bot.send_message(
                            chat_id=target_chat,
                            text=caption,
                            parse_mode="Markdown",
                            reply_markup=reply_markup,
                        )
                    except Exception:
                        await self.app.bot.send_message(
                            chat_id=target_chat,
                            text=caption,
                            parse_mode=None,
                            reply_markup=reply_markup,
                        )
                else:
                    safe_msg = caption[:4000]
                    try:
                        await self.app.bot.send_message(
                            chat_id=target_chat,
                            text=safe_msg,
                            parse_mode="Markdown",
                            reply_markup=reply_markup,
                        )
                    except Exception:
                        await self.app.bot.send_message(
                            chat_id=target_chat,
                            text=safe_msg,
                            parse_mode=None,
                            reply_markup=reply_markup,
                        )
            logger.info(f"Alert sent to Telegram for {setup.symbol} [{setup.direction}]")
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")

    async def _log_trade_to_notion(self, trade_dict: dict):
        """Asynchronously logs an executed trade to Notion in the background."""
        if not self.notion.is_configured():
            return
        try:
            ok, page_id = await self.notion.log_trade(trade_dict)
            if ok and page_id and trade_dict.get("mt5_ticket"):
                self.journal.set_notion_page_id_by_ticket(trade_dict["mt5_ticket"], page_id)
        except Exception as e:
            logger.error(f"Error logging trade to Notion in background: {e}")

    async def send_message(self, text: str):
        """Sends a markdown text message to the alert channel."""
        if not self.app or not self.alert_channel:
            return
        try:
            await self.app.bot.send_message(
                chat_id=self.alert_channel,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    async def _on_telegram_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler for Telegram application."""
        err = getattr(context, "error", None)
        logger.warning(f"Telegram error encountered: {err}")

    def build_application(self) -> Application:
        """Configures the telegram application."""
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured in .env file.")

        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
        )
        builder = Application.builder().token(self.token).request(request)
        self.app = builder.build()

        self.app.add_error_handler(self._on_telegram_error)

        self.app.add_handler(CommandHandler("start", self.start_cmd))
        self.app.add_handler(CommandHandler("status", self.status_cmd))
        self.app.add_handler(CommandHandler("prop", self.prop_cmd))
        self.app.add_handler(CommandHandler("be", self.be_cmd))
        self.app.add_handler(CommandHandler("scan", self.scan_cmd))
        self.app.add_handler(CommandHandler("journal", self.journal_cmd))
        self.app.add_handler(CommandHandler("notionsync", self.notionsync_cmd))
        self.app.add_handler(CommandHandler("optimizer", self.optimizer_cmd))
        self.app.add_handler(CommandHandler("bestrr", self.optimizer_cmd))
        self.app.add_handler(CommandHandler("addtrade", self.addtrade_cmd))
        self.app.add_handler(CommandHandler("insights", self.insights_cmd))
        self.app.add_handler(CommandHandler("adaptive", self.adaptive_cmd))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        return self.app
