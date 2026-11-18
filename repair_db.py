#!/usr/bin/env python3
"""
Forex & Gold SMC Scanner - Database Repair & Recovery Utility
Verifies the SQLite trade journal (trades.db), fixes corrupt B-Tree indexes,
recovers damaged tables, and optionally reconciles state with MetaTrader 5 and Notion.
"""

import argparse
import asyncio
import os
import sqlite3
import sys

from trade_journal import TradeJournal


def repair_database(db_path: str = "trades.db", force: bool = False) -> bool:
    print("=" * 65)
    print("   FOREX & GOLD SMC - NAPRAWA I WERYFIKACJA BAZY DANYCH")
    print("=" * 65)
    print(f"Plik bazy danych: {os.path.abspath(db_path)}")

    if not os.path.exists(db_path):
        print("ℹ️ Plik bazy danych nie istnieje. Inicjalizacja nowej, czystej bazy...")
        journal = TradeJournal(db_path)
        print("✅ Pomyślnie utworzono nową strukturę bazy danych.")
        return True

    # 1. Weryfikacja integralności struktury SQLite
    print("\n[1/3] Weryfikacja i naprawa bazy przez TradeJournal...")
    journal = TradeJournal(db_path)
    if force:
        print("   • Wymuszono rekonstrukcję bazy danych (--force)...")
        journal._recover_database()
    else:
        ok = journal.check_and_repair()
        if ok:
            print("✅ Integralność bazy danych: OK (brak uszkodzeń fizycznych).")
        else:
            print("⚠️ Baza została automatycznie naprawiona.")

    # 2. Test odczytu tabeli trades
    print("\n[2/3] Test odczytu tabeli trades...")
    try:
        open_trades = journal.get_open_trades()
        all_trades = journal.get_all_trades()
        print(f"   • Liczba wszystkich transakcji w bazie: {len(all_trades)}")
        print(f"   • Liczba aktywnych transakcji (OPEN): {len(open_trades)}")
    except Exception as e:
        print(f"⚠️ Błąd podczas odczytu tabeli: {e}. Uruchamiam auto-recovery...")
        journal._recover_database()
            print(f"   • Liczba wszystkich transakcji w bazie: {len(all_trades)}")
            print(f"   • Liczba aktywnych transakcji (OPEN): {len(open_trades)}")
        except Exception as e:
            print(f"⚠️ Błąd podczas odczytu tabeli: {e}. Uruchamiam auto-recovery...")
            journal._recover_database()

    # 3. Podsumowanie
    stats = journal.get_summary_stats()
    print("\n[3/3] Podsumowanie stanu bazy:")
    print(f"   • Transakcje ogółem: {stats['total']}")
    print(f"   • Zamknięte: {stats['closed']} (Wygrane: {stats['wins']}, Przegrane: {stats['losses']})")
    print(f"   • Win Rate: {stats['winrate']}% | Sumaryczne R: {stats['net_r']:+.2f}R")

    return True


async def main():
    parser = argparse.ArgumentParser(description="Narzędzie naprawy bazy trades.db")
    parser.add_argument("--db", default="trades.db", help="Ścieżka do pliku bazy SQLite")
    parser.add_argument("--force", action="store_true", help="Wymusza reindeksowanie i rekonstrukcję")
    parser.add_argument("--sync", action="store_true", help="Uruchamia synchronizację z MT5 i Notion po naprawie")
    args = parser.parse_args()

    ok = repair_database(db_path=args.db, force=args.force)
    if not ok:
        sys.exit(1)

    if args.sync:
        print("\n" + "=" * 65)
        print("   URUCHAMIANIE SYNCHRONIZACJI Z METATRADER 5 I NOTION...")
        print("=" * 65)
        try:
            from sync_journal import sync_all
            await sync_all()
        except Exception as e:
            print(f"⚠️ Ostrzeżenie podczas synchronizacji: {e}")

    print("\n" + "=" * 65)
    print("   OPERACJA ZAKOŃCZONA SUKCESEM!")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
