import asyncio
import logging
import signal
import sys
from config import TELEGRAM_ALERT_CHANNEL, TELEGRAM_BOT_TOKEN
from mt5_client import MT5Client
from scanner import SMCScanner
from telegram_bot import TelegramBotManager

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

    # 2. Telegram Bot Manager
    telegram_mgr = None
    telegram_task = None

    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "twoj_token_z_botfather":
        try:
            telegram_mgr = TelegramBotManager(mt5_client, TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHANNEL)
            app = telegram_mgr.build_application()
            await app.initialize()
            await app.start()
            telegram_task = asyncio.create_task(app.updater.start_polling())
            logger.info("Telegram Bot started in background polling mode.")
        except Exception as e:
            logger.error(f"Failed to start Telegram Bot: {e}")
            telegram_mgr = None
    else:
        print("\n⚠️ OSTRZEŻENIE: TELEGRAM_BOT_TOKEN nie został jeszcze skonfigurowany w pliku .env")
        print("   Skaner uruchamia się w trybie konsolowym (alerty i wykresy będą zapisywane w charts/).")
        print("   Aby odbierać alerty na telefonie i klikać zlecenia, wpisz token w pliku .env!\n")

    # 3. Initialize Scanner
    scanner = SMCScanner(mt5_client, telegram_mgr)
    if telegram_mgr:
        telegram_mgr.set_scanner_callback(scanner.scan_once)

    scanner_task = asyncio.create_task(scanner.run_loop())

    # Graceful stop handling
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Received termination signal.")
        scanner.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # On Windows, add_signal_handler may not be supported for all signals
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down...")
        scanner.stop()
        if scanner_task:
            scanner_task.cancel()
        if telegram_mgr and telegram_mgr.app:
            await telegram_mgr.app.stop()
            await telegram_mgr.app.shutdown()
        mt5_client.disconnect()
        logger.info("All services cleanly terminated.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram zakończony przez użytkownika.")
