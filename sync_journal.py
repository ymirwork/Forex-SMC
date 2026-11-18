import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import httpx
import MetaTrader5 as mt5

from config import ENABLE_NOTION
from mt5_client import MT5Client
from notion_journal import NotionJournal
from trade_journal import TradeJournal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ForexSMC.Sync")


async def sync_all(client: Optional[MT5Client] = None, journal: Optional[TradeJournal] = None):
    print("=" * 65)
    print("    SYNCHRONIZACJA DANYCH: MT5 <--> TRADES.DB <--> NOTION")
    print("=" * 65)

    created_client = False
    if client is None:
        client = MT5Client()
        if not client.connect():
            print("❌ BŁĄD: Nie można połączyć z MetaTrader 5.")
            return
        created_client = True

    if journal is None:
        journal = TradeJournal()
    notion = NotionJournal() if ENABLE_NOTION else None

    # 2. Get Open Positions from MT5
    mt5_positions = client.get_open_positions()
    open_tickets_mt5 = {p["ticket"]: p for p in mt5_positions}
    print(f"\n[1/4] Otwarte pozycje w MT5: {len(open_tickets_mt5)}")
    for t, p in open_tickets_mt5.items():
        print(f"  • #{t} {p['symbol']} [{p['type']}] | Open: {p['price_open']} | SL: {p['sl']} | TP: {p['tp']} | Zysk: {p['profit']:+.2f} USD")

    # 3. Get Closed Deals from MT5 (last 7 days + 1 day buffer for broker time)
    now = datetime.now()
    from_date = now - timedelta(days=7)
    to_date = now + timedelta(days=1)
    deals = mt5.history_deals_get(from_date, to_date)
    closed_deals_mt5: Dict[int, Dict[str, Any]] = {}
    if deals:
        for d in deals:
            if getattr(d, "entry", -1) == 1:  # DEAL_ENTRY_OUT
                pos_id = d.position_id
                closed_deals_mt5[pos_id] = {
                    "ticket": pos_id,
                    "deal_ticket": d.ticket,
                    "symbol": d.symbol,
                    "profit": d.profit,
                    "price_close": d.price,
                    "reason": d.reason,  # 4=SL, 5=TP, 0/1/3=client/expert
                    "comment": d.comment,
                    "time_close": d.time,
                }
    print(f"\n[2/4] Zamknięte pozycje w historii MT5 (ostatnie 7 dni): {len(closed_deals_mt5)}")
    for pos_id, d in closed_deals_mt5.items():
        print(f"  • #{pos_id} {d['symbol']} | Zysk: {d['profit']:+.2f} USD | Zamknięcie: {d['comment']} | Reason: {d['reason']}")

    # 4. Reconcile with trades.db
    print(f"\n[3/4] Weryfikacja i synchronizacja bazy SQLite (trades.db)...")
    db_trades = journal.get_all_trades()
    db_by_ticket = {t.get("mt5_ticket"): t for t in db_trades if t.get("mt5_ticket")}

    # A) Check open trades in DB that are actually closed in MT5
    for ticket, t in db_by_ticket.items():
        if t.get("status") == "OPEN" and ticket in closed_deals_mt5:
            d = closed_deals_mt5[ticket]
            profit = d["profit"]
            reason = d["reason"]
            price_close = d["price_close"]
            entry_p = t.get("entry_price", 0.0)
            sl_p = t.get("sl_price", 0.0)
            tp_p = t.get("tp_price", 0.0)
            is_buy = t.get("direction", "").upper() == "BUY"

            risk_dist = abs(entry_p - sl_p) if (entry_p > 0 and sl_p > 0) else 0.0
            if risk_dist > 0 and price_close > 0:
                p_diff = (price_close - entry_p) if is_buy else (entry_p - price_close)
                realized_r = round(p_diff / risk_dist, 2)
            else:
                realized_r = 1.8 if profit > 0 else -1.0

            if profit > 0 and realized_r > 0.1:
                status = "WIN"
                status_desc = "🎯 TAKE PROFIT OSIĄGNIĘTY!" if reason == 5 else "📈 ZAMKNIĘCIE W ZYSKU"
            elif profit < 0 or realized_r < -0.2:
                status = "LOSS"
                status_desc = "🛑 STOP LOSS TRAFIONY" if reason == 4 else "📉 ZAMKNIĘCIE ZE STRATĄ"
            else:
                status = "BE"
                status_desc = "🛡️ ZAMKNIĘCIE NA BREAK-EVEN (0.0R)"
                realized_r = 0.0

            calc_mfe = client.get_position_mfe(
                symbol=t["symbol"],
                is_buy=is_buy,
                entry_price=entry_p,
                initial_sl_price=sl_p,
                time_open=int(datetime.strptime(t["timestamp"], "%Y-%m-%d %H:%M:%S").timestamp()) if t.get("timestamp") else 0,
                time_close=d["time_close"],
            )
            max_r = max(calc_mfe, realized_r, float(t.get("max_r") or 0.0))
            comment_str = f"{status_desc} | Zysk: {profit:+.2f} USD | Max R: {max_r:.2f}R"

            journal.close_trade_by_ticket(ticket, realized_r, max_r=max_r, status=status, comment=comment_str)
            journal.update_trade_close_telemetry(ticket, exit_reason=("TP" if status == "WIN" else ("SL" if status == "LOSS" else "BE")), mae_r=0.0)
            print(f"  🔄 Zaktualizowano #{ticket} ({t['symbol']}) w trades.db -> {status} (PnL: {profit:+.2f} USD, R: {realized_r:+.2f}R)")

    # B) Add missing MT5 open positions to trades.db
    for ticket, p in open_tickets_mt5.items():
        if ticket not in db_by_ticket:
            raw_sym = p["symbol"].replace(".pro", "").replace(".raw", "").replace(".PRO", "").replace(".RAW", "")
            tid = journal.add_trade(
                symbol=raw_sym,
                direction=p["type"],
                entry_price=p["price_open"],
                sl_price=p["sl"],
                tp_price=p["tp"],
                realized_r=0.0,
                max_r=0.0,
                confluence_score=80,
                status="OPEN",
                chart_path=None,
                mt5_ticket=ticket,
                comment=p["comment"] or "Zsynchronizowano z MT5",
                is_pyramid=False,
                strategy="SMC",
            )
            print(f"  ➕ Dodano brakującą otwartą pozycję #{ticket} ({raw_sym}) do trades.db (ID: {tid})")

    # C) Backfill missing closed deals into trades.db
    for ticket, d in closed_deals_mt5.items():
        if ticket not in db_by_ticket and ticket not in open_tickets_mt5:
            raw_sym = d["symbol"].replace(".pro", "").replace(".raw", "").replace(".PRO", "").replace(".RAW", "")
            profit = d["profit"]
            reason = d["reason"]
            if profit > 0:
                status = "WIN"
                realized_r = 1.8
            elif profit < 0:
                status = "LOSS"
                realized_r = -1.0
            else:
                status = "BE"
                realized_r = 0.0

            tid = journal.add_trade(
                symbol=raw_sym,
                direction="BUY" if "BUY" in (d.get("comment") or "").upper() else "SELL",
                entry_price=0.0,
                sl_price=0.0,
                tp_price=0.0,
                realized_r=realized_r,
                max_r=realized_r if realized_r > 0 else 0.0,
                confluence_score=80,
                status=status,
                chart_path=None,
                mt5_ticket=ticket,
                comment=f"{d.get('comment') or 'Historia MT5'}",
                is_pyramid=False,
                strategy="SMC",
                exit_reason=("TP" if status == "WIN" else ("SL" if status == "LOSS" else "BE")),
            )
            print(f"  ➕ Dodano pozycję archiwalną #{ticket} ({raw_sym}) do trades.db -> {status} (PnL: {profit:+.2f} USD)")

    # Refresh db_trades
    db_trades = journal.get_all_trades()
    db_by_ticket = {t.get("mt5_ticket"): t for t in db_trades if t.get("mt5_ticket")}

    # 5. Reconcile with Notion
    if notion and notion.is_configured():
        print(f"\n[4/4] Weryfikacja i synchronizacja bazy Notion...")
        headers = {"Authorization": "Bearer " + notion.api_key, "Notion-Version": "2022-06-28"}
        async with httpx.AsyncClient(timeout=20.0) as http_client:
            res = await http_client.post(
                f"https://api.notion.com/v1/databases/{notion.database_id}/query",
                headers=headers,
                json={"page_size": 100},
            )
            if res.status_code == 200:
                notion_pages = res.json().get("results", [])
                notion_by_ticket: Dict[int, Dict[str, Any]] = {}
                for p in notion_pages:
                    name_p = p.get("properties", {}).get("Name", {}).get("title", [])
                    title = name_p[0]["plain_text"] if name_p else ""
                    # Title format: #<ticket> <symbol>
                    if title.startswith("#"):
                        try:
                            parts = title[1:].split()
                            t_num = int(parts[0])
                            notion_by_ticket[t_num] = p
                        except Exception:
                            pass

                print(f"  • Znaleziono {len(notion_by_ticket)} stron w Notion z numerem ticketu.")

                # A) Update closed trades in Notion that are still marked OPEN
                for ticket, page in notion_by_ticket.items():
                    props = page.get("properties", {})
                    st_prop = props.get("Status", {}).get("select") or {}
                    st_name = (st_prop.get("name") or "").upper()

                    # If Notion says OPEN, but it is closed in MT5:
                    if st_name == "OPEN" and ticket in closed_deals_mt5:
                        d = closed_deals_mt5[ticket]
                        t = db_by_ticket.get(ticket, {})
                        profit = d["profit"]
                        reason = d["reason"]
                        price_close = d["price_close"]
                        entry_p = t.get("entry_price", 0.0)
                        sl_p = t.get("sl_price", 0.0)
                        is_buy = t.get("direction", "").upper() == "BUY"

                        risk_dist = abs(entry_p - sl_p) if (entry_p > 0 and sl_p > 0) else 0.0
                        if risk_dist > 0 and price_close > 0:
                            p_diff = (price_close - entry_p) if is_buy else (entry_p - price_close)
                            realized_r = round(p_diff / risk_dist, 2)
                        else:
                            realized_r = 1.8 if profit > 0 else -1.0

                        if profit > 0 and realized_r > 0.1:
                            status = "WIN"
                            status_desc = "🎯 TAKE PROFIT" if reason == 5 else "📈 ZAMKNIĘCIE W ZYSKU"
                        elif profit < 0 or realized_r < -0.2:
                            status = "LOSS"
                            status_desc = "🛑 STOP LOSS" if reason == 4 else "📉 ZAMKNIĘCIE ZE STRATĄ"
                        else:
                            status = "BE"
                            status_desc = "🛡️ BREAK-EVEN"
                            realized_r = 0.0

                        max_r = max(realized_r, float(t.get("max_r") or 0.0))
                        comment_str = f"{status_desc} | Zysk: {profit:+.2f} USD | Max R: {max_r:.2f}R"

                        ok = await notion.update_closed_trade(
                            ticket=ticket,
                            realized_r=realized_r,
                            status=status,
                            profit=profit,
                            comment=comment_str,
                            max_r=max_r,
                            page_id=page["id"],
                        )
                        if ok:
                            print(f"  🔄 Zaktualizowano Notion dla #{ticket} -> {status} (PnL: {profit:+.2f} USD, {realized_r:+.2f}R)")
                        await asyncio.sleep(0.3)

                # B) Push any trades in trades.db missing from Notion
                for ticket, t in db_by_ticket.items():
                    if ticket not in notion_by_ticket:
                        ok, page_id = await notion.log_trade(t)
                        if ok and page_id:
                            journal.set_notion_page_id_by_ticket(ticket, page_id)
                            print(f"  ➕ Wysłano brakującą transakcję #{ticket} ({t['symbol']}) do Notion!")
                        await asyncio.sleep(0.3)
            else:
                print(f"  ❌ Błąd zapytania Notion: {res.status_code}")
    else:
        print("\n[4/4] Notion nie jest włączony lub brak konfiguracji w .env")

    if created_client:
        client.disconnect()
    print("\n" + "=" * 65)
    print("    SYNCHRONIZACJA ZAKOŃCZONA POMYŚLNIE!")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(sync_all())
