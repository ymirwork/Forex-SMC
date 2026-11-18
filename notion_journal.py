import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
import httpx

from config import ENABLE_NOTION, NOTION_API_KEY, NOTION_DATABASE_ID

logger = logging.getLogger("ForexSMC.NotionJournal")


class NotionJournal:
    """
    Automated Notion Trade Journal integration.
    Logs executed setups, confluence factors, and closed trade outcomes directly to a Notion Database.
    Compatible with institutional trading templates like maghdam/chatgpt-trading-strategy-assistant.
    """

    BASE_URL = "https://api.notion.com/v1"
    NOTION_VERSION = "2022-06-28"

    def __init__(
        self,
        api_key: str = NOTION_API_KEY,
        database_id: str = NOTION_DATABASE_ID,
        enabled: bool = ENABLE_NOTION,
    ):
        self.api_key = api_key.strip()
        # Clean 32-character database id (removes dashes if provided)
        raw_id = database_id.strip().replace("-", "")
        self.database_id = raw_id
        self.enabled = enabled and bool(self.api_key and self.database_id)
        self._db_properties: Optional[Dict[str, str]] = None

    def is_configured(self) -> bool:
        return bool(self.api_key and self.database_id)

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self.NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def verify_connection(self) -> Tuple[bool, str]:
        """Verifies Notion API token and retrieves database metadata."""
        if not self.is_configured():
            return False, "Brak skonfigurowanego NOTION_API_KEY lub NOTION_DATABASE_ID w .env."

        url = f"{self.BASE_URL}/databases/{self.database_id}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, headers=self._get_headers())
                if res.status_code == 200:
                    data = res.json()
                    title = "Bez tytułu"
                    if data.get("title") and len(data["title"]) > 0:
                        title = data["title"][0].get("plain_text", title)
                    # Cache available properties
                    self._db_properties = {
                        k.lower(): (k, v.get("type")) for k, v in data.get("properties", {}).items()
                    }
                    return True, f"Połączono z bazą Notion: '{title}' ({len(self._db_properties)} kolumn)"
                elif res.status_code == 404:
                    return False, f"Baza danych Notion nie znaleziona (sprawdź NOTION_DATABASE_ID oraz czy udostępniłeś bazę integracji)."
                elif res.status_code == 401:
                    return False, "Błąd autoryzacji: niepoprawny NOTION_API_KEY."
                else:
                    return False, f"Błąd Notion API ({res.status_code}): {res.text[:200]}"
        except Exception as e:
            return False, f"Wyjątek podczas łączenia z Notion: {e}"

    async def _fetch_schema_if_needed(self):
        if self._db_properties is None and self.is_configured():
            await self.verify_connection()

    def _build_page_properties(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dynamically maps trade fields into Notion property format matching user database columns.
        Supports fuzzy property name matching (e.g. Symbol / Pair, Realized R / R / RR, etc.).
        """
        props = {}
        db_props = self._db_properties or {}

        def find_prop(candidates: List[str]) -> Optional[Tuple[str, str]]:
            """Finds existing property in Notion database by candidate names."""
            for c in candidates:
                if c.lower() in db_props:
                    return db_props[c.lower()]
            return None

        # 1. Title / Trade ID (Required in Notion)
        title_prop = None
        for name, (orig_name, p_type) in db_props.items():
            if p_type == "title":
                title_prop = orig_name
                break
        if not title_prop:
            title_prop = "Trade ID"

        trade_title = f"#{trade.get('mt5_ticket') or trade.get('id') or 'SMC'} {trade.get('symbol', '')}"
        props[title_prop] = {"title": [{"text": {"content": trade_title}}]}

        # 2. Symbol / Pair
        match = find_prop(["Symbol", "Pair", "Instrument", "Ticker"])
        if match:
            prop_name, p_type = match
            if p_type == "select":
                props[prop_name] = {"select": {"name": str(trade.get("symbol", "N/A"))}}
            else:
                props[prop_name] = {"rich_text": [{"text": {"content": str(trade.get("symbol", "N/A"))}}]}

        # 3. Direction / Side
        match = find_prop(["Direction", "Side", "Typ", "Type"])
        if match:
            prop_name, p_type = match
            dir_val = str(trade.get("direction", "BUY")).upper()
            if p_type == "select":
                props[prop_name] = {"select": {"name": dir_val}}
            else:
                props[prop_name] = {"rich_text": [{"text": {"content": dir_val}}]}

        # 4. Status (OPEN, WIN, LOSS, BE)
        match = find_prop(["Status", "Result", "Wynik"])
        if match:
            prop_name, p_type = match
            st_val = str(trade.get("status", "OPEN")).upper()
            if p_type == "select":
                props[prop_name] = {"select": {"name": st_val}}
            else:
                props[prop_name] = {"rich_text": [{"text": {"content": st_val}}]}

        # 5. Prices: Entry, SL, TP
        entry_match = find_prop(["Entry", "Entry Price", "Cena Wejścia", "Open Price"])
        if entry_match and trade.get("entry_price"):
            props[entry_match[0]] = {"number": float(trade["entry_price"])}

        sl_match = find_prop(["Stop Loss", "SL", "Stop"])
        if sl_match and trade.get("sl_price"):
            props[sl_match[0]] = {"number": float(trade["sl_price"])}

        tp_match = find_prop(["Take Profit", "TP", "TP1", "Target"])
        if tp_match and trade.get("tp_price"):
            props[tp_match[0]] = {"number": float(trade["tp_price"])}

        # 6. Realized R
        r_match = find_prop(["Realized R", "R", "RR", "R-Multiple", "Realized RR"])
        if r_match and "realized_r" in trade:
            props[r_match[0]] = {"number": round(float(trade["realized_r"]), 2)}

        # 7. Max R (MFE)
        max_r_match = find_prop(["Max R", "MFE", "Potential R"])
        if max_r_match and "max_r" in trade:
            props[max_r_match[0]] = {"number": round(float(trade["max_r"]), 2)}

        # 8. Profit in USD
        pnl_match = find_prop(["PnL ($)", "PnL", "Profit", "Zysk ($)", "Profit (USD)"])
        if pnl_match and "profit" in trade:
            props[pnl_match[0]] = {"number": round(float(trade["profit"]), 2)}

        # 9. Confluence Score (%)
        conf_match = find_prop(["Confluence (%)", "Confluence", "Confluence Score", "Score (%)", "Koniunkcja"])
        if conf_match and trade.get("confluence_score"):
            props[conf_match[0]] = {"number": int(trade["confluence_score"])}

        # 10. Strategy
        strat_match = find_prop(["Strategy", "Strategia"])
        if strat_match:
            raw_strat = str(trade.get("strategy") or trade.get("strategy_name") or "SMC").strip()
            raw_upper = raw_strat.upper()
            if "ZUMIKO" in raw_upper or "KILLMLM" in raw_upper:
                strat_val = "Zumiko KillMLM"
            elif "GOLDEN" in raw_upper:
                strat_val = "GOLDEN_COMBO"
            elif "SMC" in raw_upper:
                strat_val = "SMC"
            else:
                strat_val = raw_strat
            props[strat_match[0]] = {"select": {"name": strat_val}}

        # 11. Grade (A+, A, B)
        grade_match = find_prop(["Grade", "Klasa", "Ocena"])
        if grade_match:
            grade_val = trade.get("grade")
            if not grade_val and trade.get("confluence_score"):
                cs = int(trade["confluence_score"])
                raw_upper = str(trade.get("strategy") or trade.get("strategy_name") or "").upper()
                if "ZUMIKO" in raw_upper or "KILLMLM" in raw_upper:
                    grade_val = "A+" if cs >= 90 else ("A" if cs >= 75 else "B")
                else:
                    grade_val = "A+" if cs >= 85 else ("A" if cs >= 70 else "B")
            if grade_val:
                props[grade_match[0]] = {"select": {"name": str(grade_val)}}

        # 12. Date / Timestamp
        date_match = find_prop(["Date", "Data", "Timestamp", "Created"])
        if date_match:
            prop_name, p_type = date_match
            ts_str = trade.get("timestamp")
            iso_date = datetime.now(timezone.utc).isoformat()
            if ts_str:
                try:
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    iso_date = dt.replace(tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass
            props[prop_name] = {"date": {"start": iso_date}}

        # 13. Notes / Strategy / Comment
        notes_match = find_prop(["Notes", "Comment", "Opis", "Uwagi", "Komentarz"])
        if notes_match:
            prop_name, _ = notes_match
            comment_text = trade.get("comment") or trade.get("setup_type") or "SMC Trade"
            props[prop_name] = {"rich_text": [{"text": {"content": str(comment_text)[:1900]}}]}

        # 14. Session
        session_match = find_prop(["Session", "Sesja", "Trading Session"])
        if session_match:
            prop_name, p_type = session_match
            session_val = str(trade.get("session_name") or "")
            if session_val and p_type == "select":
                props[prop_name] = {"select": {"name": session_val}}
            elif session_val:
                props[prop_name] = {"rich_text": [{"text": {"content": session_val}}]}

        # 15. Setup Type
        stype_match = find_prop(["Setup Type", "Typ Setupu", "Setup"])
        if stype_match:
            prop_name, p_type = stype_match
            stype_val = str(trade.get("setup_type") or trade.get("comment") or "")[:200]
            if stype_val:
                if p_type == "select":
                    props[prop_name] = {"select": {"name": stype_val[:100]}}
                else:
                    props[prop_name] = {"rich_text": [{"text": {"content": stype_val}}]}

        # 16. MAE (R) - Maximum Adverse Excursion
        mae_match = find_prop(["MAE (R)", "MAE", "Max Adverse Excursion"])
        if mae_match and trade.get("mae_r"):
            props[mae_match[0]] = {"number": round(float(trade["mae_r"]), 2)}

        # 17. Exit Reason
        exit_match = find_prop(["Exit Reason", "Powód Zamknięcia", "Exit", "Close Reason"])
        if exit_match:
            prop_name, p_type = exit_match
            exit_val = str(trade.get("exit_reason") or "")
            if exit_val:
                if p_type == "select":
                    props[exit_match[0]] = {"select": {"name": exit_val}}
                else:
                    props[exit_match[0]] = {"rich_text": [{"text": {"content": exit_val}}]}

        return props

    async def log_trade(self, trade: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Creates a new trade entry page in Notion.
        Returns (success, page_id).
        """
        if not self.enabled:
            return False, None

        await self._fetch_schema_if_needed()

        url = f"{self.BASE_URL}/pages"
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": self._build_page_properties(trade),
        }

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.post(url, headers=self._get_headers(), json=payload)
                if res.status_code in (200, 201):
                    data = res.json()
                    page_id = data.get("id")
                    logger.info(f"Trade #{trade.get('mt5_ticket')} logged to Notion! Page ID: {page_id}")
                    return True, page_id
                else:
                    logger.warning(f"Failed to log trade to Notion ({res.status_code}): {res.text}")
                    return False, None
        except Exception as e:
            logger.error(f"Error logging trade to Notion: {e}")
            return False, None

    async def update_closed_trade(
        self,
        ticket: int,
        realized_r: float,
        status: str,
        profit: float = 0.0,
        comment: str = "",
        max_r: Optional[float] = None,
        page_id: Optional[str] = None,
        strategy: Optional[str] = None,
        grade: Optional[str] = None,
    ) -> bool:
        """
        Updates an existing trade page in Notion when TP / SL or closure is detected.
        """
        if not self.enabled:
            return False

        await self._fetch_schema_if_needed()

        target_page_id = page_id
        # If no page_id cached, query database by MT5 ticket in title
        if not target_page_id:
            target_page_id = await self._find_page_id_by_ticket(ticket)

        if not target_page_id:
            logger.warning(f"Could not find Notion page for ticket #{ticket} to update.")
            return False

        url = f"{self.BASE_URL}/pages/{target_page_id}"
        patch_props: Dict[str, Any] = {}
        db_props = self._db_properties or {}

        def find_prop(candidates: List[str]) -> Optional[str]:
            for c in candidates:
                if c.lower() in db_props:
                    return db_props[c.lower()][0]
            return None

        status_col = find_prop(["Status", "Result", "Wynik"])
        if status_col:
            patch_props[status_col] = {"select": {"name": status.upper()}}

        r_col = find_prop(["Realized R", "R", "RR", "R-Multiple", "Realized RR"])
        if r_col:
            patch_props[r_col] = {"number": round(realized_r, 2)}

        max_r_col = find_prop(["Max R", "MFE", "Potential R", "Max RR"])
        if max_r_col and max_r is not None:
            patch_props[max_r_col] = {"number": round(max_r, 2)}

        pnl_col = find_prop(["PnL", "Profit", "Zysk ($)", "PnL ($)", "Profit (USD)"])
        if pnl_col:
            patch_props[pnl_col] = {"number": round(profit, 2)}

        notes_col = find_prop(["Notes", "Comment", "Opis"])
        if notes_col and comment:
            patch_props[notes_col] = {"rich_text": [{"text": {"content": comment[:1900]}}]}

        exit_col = find_prop(["Exit Reason", "Powód Zamknięcia", "Exit", "Close Reason"])
        if exit_col:
            exit_val = ""
            if status.upper() == "WIN":
                exit_val = "TP"
            elif status.upper() == "LOSS":
                exit_val = "SL"
            elif status.upper() == "BE":
                exit_val = "BE"
            else:
                exit_val = "MANUAL"
            patch_props[exit_col] = {"select": {"name": exit_val}}

        if strategy:
            strat_col = find_prop(["Strategy", "Strategia"])
            if strat_col:
                raw_upper = strategy.upper()
                strat_val = "Zumiko KillMLM" if ("ZUMIKO" in raw_upper or "KILLMLM" in raw_upper) else ("GOLDEN_COMBO" if "GOLDEN" in raw_upper else "SMC")
                patch_props[strat_col] = {"select": {"name": strat_val}}

        if grade:
            grade_col = find_prop(["Grade", "Klasa", "Ocena"])
            if grade_col:
                patch_props[grade_col] = {"select": {"name": str(grade)}}

        if not patch_props:
            return False

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.patch(url, headers=self._get_headers(), json={"properties": patch_props})
                if res.status_code == 200:
                    logger.info(f"Notion page for ticket #{ticket} updated successfully (Status={status}, PnL={profit:+.2f}).")
                    return True
                else:
                    logger.warning(f"Failed to update Notion page {target_page_id} ({res.status_code}): {res.text}")
                    return False
        except Exception as e:
            logger.error(f"Error updating Notion page: {e}")
            return False

    async def update_page_properties(self, page_id: str, patch_props: Dict[str, Any]) -> bool:
        """Directly patches properties of an existing Notion page by ID."""
        if not self.enabled:
            return False
        url = f"{self.BASE_URL}/pages/{page_id}"
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.patch(url, headers=self._get_headers(), json={"properties": patch_props})
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Error updating Notion page {page_id}: {e}")
            return False

    async def _find_page_id_by_ticket(self, ticket: int) -> Optional[str]:
        """Queries Notion database for a page containing the MT5 ticket."""
        await self._fetch_schema_if_needed()
        url = f"{self.BASE_URL}/databases/{self.database_id}/query"
        ticket_str = str(ticket)

        title_col = None
        if self._db_properties:
            for orig_name, p_type in self._db_properties.values():
                if p_type == "title":
                    title_col = orig_name
                    break

        payload: Dict[str, Any] = {"page_size": 20}
        if title_col:
            payload["filter"] = {
                "property": title_col,
                "title": {
                    "contains": f"#{ticket_str}"
                }
            }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=self._get_headers(), json=payload)
                # If filtered query failed (e.g. schema mismatch), fallback to broad search
                if res.status_code != 200 and "filter" in payload:
                    del payload["filter"]
                    payload["page_size"] = 100
                    res = await client.post(url, headers=self._get_headers(), json=payload)

                if res.status_code == 200:
                    results = res.json().get("results", [])
                    for page in results:
                        props = page.get("properties", {})
                        for p_val in props.values():
                            p_type = p_val.get("type")
                            if p_type == "title":
                                title_texts = p_val.get("title", [])
                                if any(ticket_str in t.get("plain_text", "") for t in title_texts):
                                    return page.get("id")
                            elif p_type == "rich_text":
                                rich_texts = p_val.get("rich_text", [])
                                if any(ticket_str in t.get("plain_text", "") for t in rich_texts):
                                    return page.get("id")
                            elif p_type == "number" and p_val.get("number") == ticket:
                                return page.get("id")
        except Exception as e:
            logger.error(f"Error searching Notion by ticket: {e}")
        return None
