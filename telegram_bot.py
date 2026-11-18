import asyncio
import logging
from typing import Dict, Optional
import os

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
    TELEGRAM_ALERT_CHANNEL,
    TELEGRAM_BOT_TOKEN,
)
from mt5_client import MT5Client
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
    ):
        self.mt5_client = mt5_client
        self.token = token
        self.alert_channel = alert_channel
        self.authorized_chat_id = alert_channel  # alias for backward-compatibility
        self.allowed_chat_ids = allowed_ids if allowed_ids is not None else ALLOWED_CHAT_IDS
        self.app: Optional[Application] = None
        self.pending_setups: Dict[str, TradeSetup] = {}
        self.scanner_callback = None  # Reference to manual trigger scan
        self.journal = TradeJournal()

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
            "• `/be` - Przestawienie Stop Loss na Break-Even (cena wejścia)\n"
            "• `/scan` - Wymuś natychmiastowe przeskanowanie rynku\n"
            "• `/journal` - Dziennik zagrań i dotychczasowe statystyki R\n"
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
            icon = "🟢" if t["realized_r"] > 0 else "🔴"
            recent_lines += (
                f"• #{t['id']} {t['symbol']} [{t['direction']}]: "
                f"Wynik: {icon} `{t['realized_r']:+.1f}R` | Max R: `{t['max_r']:.1f}` | Confluence: `{t['confluence_score']}%`\n"
            )

        msg = (
            "📓 **DZIENNIK TRANZAKCJI (SMC JOURNAL)**\n\n"
            f"📊 **Statystyki łączne:**\n"
            f"• Liczba pozycji: `{stats['total']}` (Wygrane: `{stats['wins']}`, Przegrane: `{stats['losses']}`)\n"
            f"• Skuteczność (Win Rate): `{stats['winrate']}%`\n"
            f"• Łączny wygenerowany zysk: `{stats['net_r']:+.2f}R`\n"
            f"• Średni zysk na transakcję: `{stats['avg_r']:+.2f}R`\n\n"
            "📋 **Ostatnie transakcje:**\n"
            f"{recent_lines}\n"
            "💡 Wpisz `/optimizer`, aby obliczyć matematycznie najlepszy poziom Take Profit (BEST RR)!"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

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
        """Allows manual addition of trades from phone: /addtrade XAUUSD YES 1.0 2.8"""
        if not self._is_authorized(update):
            return

        args = context.args
        if not args or len(args) < 4:
            help_msg = (
                "ℹ️ **Instrukcja dodawania transakcji:**\n"
                "`/addtrade <SYMBOL> <YES/NO> <REALIZED_R> <MAX_R>`\n\n"
                "**Przykład:**\n"
                "`/addtrade XAUUSD YES 1.0 2.8`\n"
                "*(Oznacza wygraną pozycję na złocie zamkniętą przy 1.0R, gdzie rynek maksymalnie dał 2.8R)*"
            )
            await update.message.reply_text(help_msg, parse_mode="Markdown")
            return

        try:
            symbol = args[0].upper()
            is_win = args[1].upper() in ("YES", "Y", "TAK", "WIN", "1")
            realized_r = float(args[2].replace(",", "."))
            max_r = float(args[3].replace(",", "."))
            status = "WIN" if is_win else "LOSS"

            trade_id = self.journal.add_trade(
                symbol=symbol,
                direction="BUY" if is_win else "SELL",
                entry_price=0.0,
                sl_price=0.0,
                tp_price=0.0,
                realized_r=realized_r,
                max_r=max_r,
                confluence_score=80,
                status=status,
                comment="Dodano ręcznie przez Telegram",
            )
            await update.message.reply_text(
                f"✅ Dodano transakcję #{trade_id} do dziennika!\n"
                f"• {symbol} [{status}]: Wynik `{realized_r:+.1f}R` | Max R: `{max_r:.1f}R`\n\n"
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
            return

        if not setup:
            await query.edit_message_caption(caption=f"{query.message.caption}\n\n⚠️ **Ten setup wygasł lub został już przetworzony.**")
            return

        risk_pct = DEFAULT_RISK_PERCENT
        lot = self.mt5_client.calculate_lot_size(setup.symbol, risk_pct, setup.sl_price, setup.entry_price)

        if action == "market":
            success, msg, ticket = self.mt5_client.execute_market_order(
                symbol=setup.symbol,
                direction=setup.direction,
                volume=lot,
                sl=setup.sl_price,
                tp=setup.tp1_price,
                comment=f"SMC {setup.setup_type[:15]}",
            )
            if success:
                self.journal.add_trade(
                    symbol=setup.symbol,
                    direction=setup.direction,
                    entry_price=setup.entry_price,
                    sl_price=setup.sl_price,
                    tp_price=setup.tp1_price,
                    realized_r=0.0,
                    max_r=setup.rr_ratio,
                    confluence_score=setup.confluence_score,
                    status="OPEN",
                    mt5_ticket=ticket,
                    comment="Market order via Telegram",
                )
                new_caption = (
                    f"{query.message.caption}\n\n"
                    f"✅ **ZLECENIE WYKONANE W MT5!**\n"
                    f"🎫 **Ticket:** `#{ticket}`\n"
                    f"📦 **Wolumen:** `{lot} lot` (Ryzyko: {risk_pct}%)\n"
                    f"🎯 **SL:** `{setup.sl_price}` | **TP:** `{setup.tp1_price}`"
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
                comment=f"SMC Limit {setup.setup_type[:15]}",
            )
            if success:
                self.journal.add_trade(
                    symbol=setup.symbol,
                    direction=setup.direction,
                    entry_price=setup.entry_price,
                    sl_price=setup.sl_price,
                    tp_price=setup.tp1_price,
                    realized_r=0.0,
                    max_r=setup.rr_ratio,
                    confluence_score=setup.confluence_score,
                    status="OPEN",
                    mt5_ticket=ticket,
                    comment="Market order via Telegram",
                )
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

    async def send_setup_alert(self, setup: TradeSetup, chart_path: str, setup_id: str):
        """Sends rich alert with chart image and interactive execution buttons."""
        if not self.app or not self.alert_channel:
            logger.warning("Telegram Bot application not running or alert channel not set.")
            return

        self.pending_setups[setup_id] = setup

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

        # Strategy title
        strat_name = getattr(setup, "strategy_name", "SMC")
        if strat_name == "GOLDEN_COMBO":
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
            f"⚖️ **Risk / Reward:** `1 : {setup.rr_ratio}` {rr_badge}"
            f"{rr_src}"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    f"🟢 Wejdź Market ({DEFAULT_RISK_PERCENT}% ryzyka)",
                    callback_data=f"market:{setup_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🟡 Złóż Limit na {setup.entry_price}",
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
        self.app.add_handler(CommandHandler("be", self.be_cmd))
        self.app.add_handler(CommandHandler("scan", self.scan_cmd))
        self.app.add_handler(CommandHandler("journal", self.journal_cmd))
        self.app.add_handler(CommandHandler("optimizer", self.optimizer_cmd))
        self.app.add_handler(CommandHandler("bestrr", self.optimizer_cmd))
        self.app.add_handler(CommandHandler("addtrade", self.addtrade_cmd))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        return self.app
