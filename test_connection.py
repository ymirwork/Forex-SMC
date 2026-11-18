import asyncio
import os
import sys
from config import (
    HTF_TIMEFRAME,
    LTF_TIMEFRAME,
    SYMBOLS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from mt5_client import MT5Client
from smc_detector import SMCDetector


async def test_all():
    print("=" * 60)
    print("   FOREX & GOLD SMC SCANNER - TEST SYSTEMU")
    print("=" * 60)

    # 1. MT5 Connection
    print("\n[1/5] Test połączenia z MetaTrader 5...")
    client = MT5Client()
    connected = client.connect()
    if not connected:
        print("❌ BŁĄD: Nie można połączyć z MT5!")
        print("   Upewnij się, że MetaTrader 5 jest uruchomiony na komputerze.")
        return

    acc = client.get_account_info()
    if acc:
        print(f"✅ Połączono pomyślnie z brokerem: {acc['company']}")
        print(f"   • Konto: {acc['login']} ({acc['name']})")
        print(f"   • Serwer: {acc['server']}")
        print(f"   • Saldo: {acc['balance']:.2f} {acc['currency']}")
        print(f"   • Equity: {acc['equity']:.2f} {acc['currency']}")
        print(f"   • Dźwignia: 1:{acc['leverage']}")
    else:
        print("⚠️ Połączono z terminalem, ale brak aktywnego konta.")

    # 2. Symbol Resolution & Live Candles
    print(f"\n[2/5] Test pobierania danych dla par ({LTF_TIMEFRAME} / {HTF_TIMEFRAME})...")
    for sym in SYMBOLS:
        resolved = client.resolve_symbol(sym)
        if resolved:
            df = client.fetch_ohlcv(sym, LTF_TIMEFRAME, 50)
            if df is not None and not df.empty:
                last_p = df["close"].iloc[-1]
                print(f"   • {sym:7s} -> {resolved:10s} | Ostatnia cena: {last_p:.4f} ({len(df)} świec OK)")
            else:
                print(f"   • {sym:7s} -> {resolved:10s} | ⚠️ Brak danych świecowych!")
        else:
            print(f"   • {sym:7s} -> ❌ Nie znaleziono u brokera!")

    # 3. SMC & Zumiko Detectors Dry Run
    print("\n[3/5] Test silnika SMC...")
    detector = SMCDetector()
    df_gold = client.fetch_ohlcv("XAUUSD", LTF_TIMEFRAME, 300)
    df_gold_htf = client.fetch_ohlcv("XAUUSD", HTF_TIMEFRAME, 300)
    if df_gold is not None and df_gold_htf is not None:
        trend = detector.determine_htf_trend(df_gold_htf)
        setups = detector.scan_for_setups("XAUUSD", df_gold, df_gold_htf)
        print(f"   • XAUUSD {HTF_TIMEFRAME} Trend: {trend}")
        print(f"   • Wykryte setupy SMC: {len(setups)}")
        print("✅ Silnik Smart Money Concepts działa poprawnie!")

    print("\n[4/5] Test silnika Zumiko KillMLM (MTF)...")
    from zumiko_detector import ZumikoDetector
    z_detector = ZumikoDetector()
    df_gold_h1 = client.fetch_ohlcv("XAUUSD", "H1", 300)
    if df_gold is not None and df_gold_h1 is not None and df_gold_htf is not None:
        z_mtf = z_detector.calculate_mtf_trend(df_gold, df_gold_h1, df_gold_htf)
        z_setups = z_detector.scan_for_setups("XAUUSD", df_gold, df_gold_h1, df_gold_htf)
        print(f"   • XAUUSD MTF Matrix: M15:({z_mtf.get('M15')}) | H1:({z_mtf.get('H1')}) | H4:({z_mtf.get('H4')})")
        print(f"   • Wykryte setupy Zumiko: {len(z_setups)}")
        print("✅ Silnik Zumiko KillMLM działa poprawnie!")

    # 5. Telegram Bot Token Check
    print("\n[5/5] Test konfiguracji Telegrama...")
    if not TELEGRAM_BOT_TOKEN:
        print("ℹ️ TELEGRAM_BOT_TOKEN nie jest jeszcze ustawiony w pliku .env")
        print("   (Uzupełnij .env tokenem od @BotFather, aby otrzymywać powiadomienia)")
    else:
        try:
            import httpx
            with httpx.Client(timeout=15.0) as http_client:
                res = http_client.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe")
                if res.status_code == 502:
                    print("⚠️ Serwer Telegrama zwrocił błąd 502 Bad Gateway (chwilowa awaria po stronie serwerów Telegrama lub nieaktualny token).")
                    print("   Wskazówka: Wygeneruj nowy token w @BotFather komendą /revoke i wklej go do .env")
                    data = {}
                else:
                    data = res.json()
            if data.get("ok"):
                bot_user = data["result"]["username"]
                print(f"✅ Bot Telegrama zweryfikowany: @{bot_user}")
                if TELEGRAM_CHAT_ID:
                    print(f"   • Zautoryzowany Chat ID: {TELEGRAM_CHAT_ID}")
                else:
                    print("   ⚠️ TELEGRAM_CHAT_ID nie jest ustawiony w .env (ustaw, by nikt inny nie miał dostępu).")
            elif res.status_code != 502:
                print(f"❌ Błędny token Telegrama: {data.get('description')}")
        except Exception as e:
            print(f"⚠️ Nie można połączyć z API Telegrama: {e}")

    client.disconnect()
    print("\n" + "=" * 60)
    print("   TEST ZAKOŃCZONY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_all())
