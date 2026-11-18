import asyncio
import logging
import os
import time
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import (
    ALLOWED_CHAT_IDS,
    DEFAULT_RISK_PERCENT,
    ENABLE_NOTION,
    PROP_FIRM_MODE,
    PROP_INITIAL_BALANCE,
    PROP_MAX_DRAWDOWN_PERCENT,
    PROP_MAX_OPEN_TRADES,
    PROP_RISK_PER_TRADE_PERCENT,
    PROP_SAFETY_BUFFER_PERCENT,
    PYRAMID_LOCK_PREVIOUS_PROFIT,
    TELEGRAM_ALERT_CHANNEL,
    TELEGRAM_BOT_TOKEN,
)
from mt5_client import MT5Client
from notion_journal import NotionJournal
from rr_optimizer import RROptimizer
from smc_detector import TradeSetup
from trade_journal import TradeJournal

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
        """Enforces security whitelist: checks user, channel, or message chat ID."""
        if not self.allowed_chat_ids:
            return True
        user_id = update.effective_user.id if update.effective_user else 0
        chat_id = update.effective_chat.id if update.effective_chat else 0
        msg_chat_id = (
            update.callback_query.message.chat.id
            if update.callback_query and update.callback_query.message
            else 0
        )
        return (
            (user_id in self.allowed_chat_ids)
            or (chat_id in self.allowed_chat_ids)
            or (msg_chat_id in self.allowed_chat_ids)
        )

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await update.message.reply_text("⛔ Brak uprawnień do korzystania z tego bota.")
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
            "• `/addtrade` - Ręczne dodanie zagrania do dziennika"
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
            pos_text = "\n\n📈 **Otwarte pozycje:**\n"
            for p in positions:
                total_pnl += p["profit"]
                emoji = "🟢" if p["profit"] >= 0 else "🔴"
                pos_text += (
                    f"• {p['symbol']} [{p['type']}] #{p['ticket']}: "
                    f"Lot {p['volume']} @ {p['price_open']} | PnL: {emoji} `{p['profit']:.2f} {acc['currency']}`\n"
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
            pyr_tag = " 🔥[Pyr L2]" if t.get("is_pyramid") else ""
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
        """Syncs all historical or unsynced trades from trades.db to Notion."""
        if not self._is_authorized(update):
            return

        if not self.notion.is_configured():
            help_msg = (
                "📓 **INTEGRACJA NOTION (TRADE JOURNAL)**\n\n"
                "System obsługuje automatyczne zapisywanie każdego tradu w Notion (wzorowane na ChatGPT Assistant)!\n\n"
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

        await update.message.reply_text("🔄 Weryfikuję połączenie z bazą Notion...")
        ok, msg = await self.notion.verify_connection()
        if not ok:
            await update.message.reply_text(f"❌ {msg}\n\nUpewnij się, że dodałeś połączenie (Connection) do swojej bazy w Notion!")
            return

        await update.message.reply_text(f"✅ {msg}\nRozpoczynam wysyłanie transakcji...")

        trades = self.journal.get_all_trades()
        synced_count = 0
        for t in trades:
            if not t.get("notion_page_id"):
                success, page_id = await self.notion.log_trade(t)
                if success and page_id:
                    self.journal.set_notion_page_id_by_ticket(t.get("mt5_ticket") or t.get("id"), page_id)
                    synced_count += 1
                await asyncio.sleep(0.3)  # Respect Notion API rate limits

        await update.message.reply_text(
            f"🎉 **Synchronizacja zakończona!**\n"
            f"Wysłano `{synced_count}` nowych transakcji do Notion.\n"
            f"Wszystkie kolejne zagrania będą zapisywać się w Notion automatycznie!"
        )

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
        await query.answer()

        if not self._is_authorized(update):
            await query.edit_message_caption(caption="⛔ Brak uprawnień do wykonania tego zlecenia.")
            return

        data = query.data or ""
        parts = data.split(":")
        action = parts[0]
        setup_id = parts[1] if len(parts) > 1 else ""

        if action == "be":
            try:
                ticket_num = int(setup_id)
                ok, msg = self.mt5_client.move_sl_to_breakeven(ticket_num)
                await query.answer(text=msg[:190], show_alert=True)
                if ok and query.message:
                    await query.message.reply_text(msg, parse_mode="Markdown")
            except Exception as e:
                await query.answer(text=f"Błąd BE: {e}", show_alert=True)
            return

        setup = self.pending_setups.get(setup_id)

        if action == "dismiss":
            await query.edit_message_caption(caption=f"{query.message.caption}\n\n❌ **Setup odrzucony przez użytkownika.**")
            if setup_id in self.pending_setups:
                del self.pending_setups[setup_id]
            self.pending_setups_time.pop(setup_id, None)
            return

        if not setup:
            await query.edit_message_caption(caption=f"{query.message.caption}\n\n⚠️ **Ten setup wygasł lub został już przetworzony.**")
            return

        if action in ("market", "limit"):
            if PROP_FIRM_MODE:
                sl_dist = abs(setup.entry_price - setup.sl_price)
                can_trade, reason = self.mt5_client.can_open_new_trade(
                    setup.symbol,
                    sl_distance=sl_dist,
                    is_pyramid=getattr(setup, "is_pyramid", False),
                )
                if not can_trade:
                    await query.answer(text=reason[:190], show_alert=True)
                    await query.edit_message_caption(
                        caption=f"{query.message.caption}\n\n🛑 **Zlecenie zablokowane (Prop Firm Guardian):**\n{reason}"
                    )
                    return

        risk_pct = PROP_RISK_PER_TRADE_PERCENT if PROP_FIRM_MODE else DEFAULT_RISK_PERCENT
        lot = self.mt5_client.calculate_lot_size(setup.symbol, risk_pct, setup.sl_price, setup.entry_price)

        is_pyr = 1 if getattr(setup, "is_pyramid", False) else 0
        parent_t = getattr(setup, "parent_ticket", None)
        strat_label = f"PYRAMID_{setup.strategy_name}" if is_pyr else setup.setup_type

        if action == "market":
            success, msg, ticket = self.mt5_client.execute_market_order(
                symbol=setup.symbol,
                direction=setup.direction,
                volume=lot,
                sl=setup.sl_price,
                tp=setup.tp1_price,
                comment=f"SMC {'Pyr L2 ' if is_pyr else ''}{setup.setup_type[:10]}",
            )
            if success:
                trail_msg = ""
                if is_pyr and parent_t and PYRAMID_LOCK_PREVIOUS_PROFIT:
                    trail_ok, t_msg = self.mt5_client.trail_sl_to_profit(parent_t, profit_r=0.5)
                    if trail_ok:
                        trail_msg = f"\n🔒 **Zabezpieczono zysk bazowy (+0.5R):** {t_msg}"

                self.journal.add_trade(
                    symbol=setup.symbol,
                    direction=setup.direction,
                    entry_price=setup.entry_price,
                    sl_price=setup.sl_price,
                    tp_price=setup.tp1_price,
                    realized_r=0.0,
                    max_r=0.0,
                    confluence_score=setup.confluence_score,
                    status="OPEN",
                    mt5_ticket=ticket,
                    comment=f"{'Piramida L2' if is_pyr else 'Market order'} via Telegram | {setup.setup_type}",
                    parent_ticket=parent_t,
                    is_pyramid=is_pyr,
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
                    "status": "OPEN",
                    "mt5_ticket": ticket,
                    "comment": f"{strat_label} | via Telegram",
                }))
                new_caption = (
                    f"{query.message.caption}\n\n"
                    f"✅ **ZLECENIE WYKONANE W MT5!**\n"
                    f"🎫 **Ticket:** `#{ticket}`\n"
                    f"📦 **Wolumen:** `{lot} lot` (Ryzyko: {risk_pct}%)\n"
                    f"🎯 **SL:** `{setup.sl_price}` | **TP:** `{setup.tp1_price}`"
                    f"{trail_msg}"
                )
                await query.edit_message_caption(caption=new_caption, parse_mode="Markdown")
            else:
                await query.edit_message_caption(
                    caption=f"{query.message.caption}\n\n❌ **Błąd wykonania w MT5:** {msg}"
                )

        elif action == "limit":
            success, msg, ticket = self.mt5_client.execute_limit_order(
                symbol=setup.symbol,
                direction=setup.direction,
                price=setup.entry_price,
                volume=lot,
                sl=setup.sl_price,
                tp=setup.tp1_price,
                comment=f"SMC Limit {'Pyr ' if is_pyr else ''}{setup.setup_type[:10]}",
            )
            if success:
                self.journal.add_trade(
                    symbol=setup.symbol,
                    direction=setup.direction,
                    entry_price=setup.entry_price,
                    sl_price=setup.sl_price,
                    tp_price=setup.tp1_price,
                    realized_r=0.0,
                    max_r=0.0,
                    confluence_score=setup.confluence_score,
                    status="OPEN",
                    mt5_ticket=ticket,
                    comment=f"{'Piramida L2 Limit' if is_pyr else 'Limit order'} via Telegram | {setup.setup_type}",
                    parent_ticket=parent_t,
                    is_pyramid=is_pyr,
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
                    "status": "OPEN",
                    "mt5_ticket": ticket,
                    "comment": f"{strat_label} (Limit) | via Telegram",
                }))
                new_caption = (
                    f"{query.message.caption}\n\n"
                    f"🟡 **ZLECENIE LIMIT ZŁOŻONE W MT5!**\n"
                    f"🎫 **Ticket:** `#{ticket}`\n"
                    f"📍 **Cena Limit:** `{setup.entry_price}`\n"
                    f"📦 **Wolumen:** `{lot} lot`"
                )
                await query.edit_message_caption(caption=new_caption, parse_mode="Markdown")
            else:
                await query.edit_message_caption(
                    caption=f"{query.message.caption}\n\n❌ **Błąd zlecenia limit w MT5:** {msg}"
                )

        if setup_id in self.pending_setups:
            del self.pending_setups[setup_id]
        self.pending_setups_time.pop(setup_id, None)

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
        pyramid_banner = ""
        if is_pyr:
            header_title = f"🔥 **DOKŁADKA DO POZYCJI (PIRAMIDA L2): {setup.symbol}**"
            pyramid_banner = (
                f"🛡️ **Pozycja bazowa #{setup.parent_ticket}** jest zabezpieczona na BE (`+{setup.pyramid_profit_r:.1f}R`)!\n"
                f"⚖️ **Ryzyko netto portfela:** `ZEROWE` (Risk-Neutral Compounding)\n\n"
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

        caption = (
            f"{header_title}\n"
            f"🎯 **Ocena:** `{setup.confluence_score}%` {grade_emoji}\n\n"
            f"{pyramid_banner}"
            f"{news_banner}"
            f"📊 **Kierunek:** {dir_emoji}\n"
            f"⏱ **Interwał:** `{setup.timeframe}` (H4: **{setup.htf_trend}**) | {session_badge}\n"
            f"🎯 **Strategia:** `{setup.setup_type}`\n\n"
            f"{mtf_text}"
            f"{zumiko_text}"
            f"{fibo_text}"
            f"{confluence_lines}"
            f"📍 **Entry:** `{setup.entry_price:.{digits}f}` | 🛑 **SL:** `{setup.sl_price:.{digits}f}` ({sl_pips:.1f} p)\n"
            f"🎯 **TP1:** `{setup.tp1_price:.{digits}f}` (Target 1:{setup.rr_ratio})\n"
            f"🎯 **TP2:** `{setup.tp2_price:.{digits}f}` (Strukturalny szczyt/dołek)\n"
            f"⚖️ **Risk / Reward:** `1 : {setup.rr_ratio}` {rr_badge}\n"
            f"{prop_risk_line}"
            f"{rr_src}"
        )

        btn_market_text = (
            f"🟢 Dołóż do pozycji ({risk_pct}% ryzyka)"
            if is_pyr
            else f"🟢 Wejdź Market ({risk_pct}% ryzyka)"
        )
        btn_limit_text = (
            f"🟡 Dołóż Limit na {setup.entry_price}"
            if is_pyr
            else f"🟡 Złóż Limit na {setup.entry_price}"
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

        try:
            target_chat = self.alert_channel
            if chart_path and os.path.exists(chart_path):
                with open(chart_path, "rb") as photo:
                    if len(caption) <= 1024:
                        await self.app.bot.send_photo(
                            chat_id=target_chat,
                            photo=photo,
                            caption=caption,
                            parse_mode="Markdown",
                            reply_markup=reply_markup,
                        )
                    else:
                        # Truncate cleanly at 1000 characters to never exceed Telegram 1024 limit
                        safe_caption = caption[:1000].rsplit("\n", 1)[0]
                        msg = await self.app.bot.send_photo(
                            chat_id=target_chat,
                            photo=photo,
                            caption=safe_caption,
                            parse_mode="Markdown",
                            reply_markup=reply_markup,
                        )
                        # Send full specification as follow-up reply
                        await self.app.bot.send_message(
                            chat_id=target_chat,
                            text=f"📋 **Szczegóły setupu:**\n\n{caption}",
                            parse_mode="Markdown",
                            reply_to_message_id=msg.message_id,
                        )
            else:
                if len(caption) <= 4096:
                    await self.app.bot.send_message(
                        chat_id=target_chat,
                        text=caption,
                        parse_mode="Markdown",
                        reply_markup=reply_markup,
                    )
                else:
                    await self.app.bot.send_message(
                        chat_id=target_chat,
                        text=caption[:4000],
                        parse_mode="Markdown",
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

    def build_application(self) -> Application:
        """Configures the telegram application."""
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured in .env file.")

        builder = Application.builder().token(self.token)
        self.app = builder.build()

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
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        return self.app
