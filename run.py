import asyncio
import logging
import signal
import sys
from config import TELEGRAM_ALERT_CHANNEL, TELEGRAM_BOT_TOKEN
from mt5_client import MT5Client
from news_filter import NewsFilter
from scanner import SMCScanner
from telegram_bot import TelegramBotManager
from trade_journal import TradeJournal

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ForexSMC.Main")


async def main():
    print("=" * 65)
    print("       FOREX & GOLD SMC SCANNER + TELEGRAM BOT (SEMI-AUTO)       ")
    print("=" * 65)

    # 1. Initialize MT5 Client
    mt5_client = MT5Client()
    if not mt5_client.connect():
        logger.error("Failed to connect to MetaTrader 5 terminal. Exiting.")
        return

    # Shared Single-Instance Resources (TradeJournal & NewsFilter)
    shared_journal = TradeJournal()
    shared_news_filter = NewsFilter()

    # Auto-reconcile MT5, trades.db and Notion on startup
    try:
        from sync_journal import sync_all
        logger.info("Reconciling MT5, trades.db and Notion...")
        await sync_all(client=mt5_client, journal=shared_journal)
    except Exception as e:
        logger.warning(f"Startup trade reconciliation warning: {e}")

    # 2. Telegram Bot Manager
    telegram_mgr = None
    telegram_task = None

    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "twoj_token_z_botfather":
        telegram_mgr = TelegramBotManager(
            mt5_client=mt5_client,
            token=TELEGRAM_BOT_TOKEN,
            alert_channel=TELEGRAM_ALERT_CHANNEL,
            journal=shared_journal,
            news_filter=shared_news_filter,
        )

        async def init_telegram_with_retry():
            nonlocal telegram_task
            app = telegram_mgr.build_application()
            for attempt in range(1, 4):
                try:
                    logger.info(f"Connecting to Telegram Bot API (attempt {attempt}/3)...")
                    await app.initialize()
                    await app.start()
                    telegram_task = asyncio.create_task(app.updater.start_polling())
                    logger.info("Telegram Bot started in background polling mode.")
                    return
                except Exception as e:
                    logger.warning(f"Telegram Bot connection attempt {attempt} failed: {e}")
                    if attempt < 3:
                        await asyncio.sleep(5)

            logger.error("Initial Telegram connection failed (server 502/timeout).")
            logger.info("Bot will continue scanning and retry connecting to Telegram in the background.")

            async def background_reconnect_loop():
                nonlocal telegram_task
                while True:
                    await asyncio.sleep(30)
                    try:
                        logger.info("Retrying Telegram connection...")
                        await app.initialize()
                        await app.start()
                        telegram_task = asyncio.create_task(app.updater.start_polling())
                        logger.info("✅ Telegram Bot successfully reconnected!")
                        break
                    except Exception as e:
                        logger.debug(f"Background Telegram reconnect attempt failed: {e}")

            asyncio.create_task(background_reconnect_loop())

        await init_telegram_with_retry()
    else:
        print("\n⚠️ OSTRZEŻENIE: TELEGRAM_BOT_TOKEN nie został jeszcze skonfigurowany w pliku .env")
        print("   Skaner uruchamia się w trybie konsolowym (alerty i wykresy będą zapisywane w charts/).")
        print("   Aby odbierać alerty na telefonie i klikać zlecenia, wpisz token w pliku .env!\n")

    # 3. Initialize Scanner with Shared Resources
    scanner = SMCScanner(
        mt5_client=mt5_client,
        telegram_manager=telegram_mgr,
        journal=shared_journal,
        news_filter=shared_news_filter,
    )
    if telegram_mgr:
        telegram_mgr.set_scanner_callback(scanner.scan_once)

    scanner_task = asyncio.create_task(scanner.run_loop())

    # Graceful stop handling
    stop_event = asyncio.Event()

    def signal_handler(*args):
        logger.info("Received termination signal.")
        scanner.stop()
        loop.call_soon_threadsafe(stop_event.set)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # On Windows, add_signal_handler is not implemented by ProactorEventLoop
            try:
                signal.signal(sig, signal_handler)
            except Exception:
                pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down...")
        scanner.stop()
        if scanner_task and not scanner_task.done():
            scanner_task.cancel()
        if telegram_task and not telegram_task.done():
            telegram_task.cancel()
        if telegram_mgr and telegram_mgr.app:
            try:
                if telegram_mgr.app.updater and telegram_mgr.app.updater.running:
                    await telegram_mgr.app.updater.stop()
                await telegram_mgr.app.stop()
                await telegram_mgr.app.shutdown()
            except Exception as e:
                logger.warning(f"Error shutting down Telegram app: {e}")
        if shared_journal:
            shared_journal.checkpoint()
        mt5_client.disconnect()
        logger.info("All services cleanly terminated.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram zakończony przez użytkownika.")
