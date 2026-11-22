"""
תיק השקעות — Interactive Brokers דרך Flex Web Service.

למה דווקא Flex ולא ה-API ה"אמיתי": ה-TWS API דורש להריץ IB Gateway
(תהליך Java עם התחברות ו-2FA שבועי) על השרת — שביר ומסורבל. Flex הוא
שני GET-ים ב-HTTPS עם טוקן, בלי שום תהליך רץ. המחיר: הנתונים הם נכון
לסוף יום המסחר האחרון — בדיוק מה שצריך לבריף בוקר, לא למסחר חי.

הגדרה חד-פעמית ב-Client Portal (יש מדריך בקובץ ההוראות):
    IBKR_FLEX_TOKEN     טוקן מ-Flex Web Service Configuration
    IBKR_FLEX_QUERY_ID  מזהה ה-Activity Flex Query שיצרת
"""

import os
import json
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app import db

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Jerusalem")

FLEX_TOKEN = os.getenv("IBKR_FLEX_TOKEN", "")
FLEX_QUERY_ID = os.getenv("IBKR_FLEX_QUERY_ID", "")
BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
HEADERS = {"User-Agent": "Python/3.10"}   # חובה לפי התיעוד של IBKR


def configured() -> bool:
    return bool(FLEX_TOKEN and FLEX_QUERY_ID)


async def _fetch_xml() -> ET.Element | None:
    """שלב 1: SendRequest מחזיר ReferenceCode. שלב 2: GetStatement (עם המתנה)."""
    async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
        r = await client.get(f"{BASE}/SendRequest",
                             params={"t": FLEX_TOKEN, "q": FLEX_QUERY_ID, "v": "3"})
        r.raise_for_status()
        root = ET.fromstring(r.text)
        if (root.findtext("Status") or "").strip() != "Success":
            logger.error("IBKR SendRequest נכשל: %s", root.findtext("ErrorMessage"))
            return None
        ref = (root.findtext("ReferenceCode") or "").strip()

        for attempt in range(6):           # הדוח נוצר בצד שלהם — לפעמים לוקח רגע
            await asyncio.sleep(3 + attempt * 3)
            r = await client.get(f"{BASE}/GetStatement",
                                 params={"t": FLEX_TOKEN, "q": ref, "v": "3"})
            r.raise_for_status()
            if r.text.lstrip().startswith("<FlexQueryResponse"):
                return ET.fromstring(r.text)
            root = ET.fromstring(r.text)
            code = (root.findtext("ErrorCode") or "").strip()
            if code not in {"1019", "1021"}:   # "עוד לא מוכן" — נחכה; אחרת שגיאה
                logger.error("IBKR GetStatement נכשל: %s", root.findtext("ErrorMessage"))
                return None
    logger.error("IBKR: הדוח לא היה מוכן אחרי 6 ניסיונות")
    return None


def _parse(root: ET.Element) -> dict | None:
    """מחלץ פוזיציות, מזומן ושווי — עמיד לזה שלא כל הסקשנים קיימים בשאילתה."""
    positions = []
    for pos in root.iter("OpenPosition"):
        a = pos.attrib
        try:
            positions.append({
                "symbol": a.get("symbol", "?"),
                "qty": float(a.get("position", 0)),
                "price": float(a.get("markPrice", 0)),
                "value": float(a.get("positionValue", 0)),
            })
        except ValueError:
            continue

    cash = None
    for row in root.iter("CashReportCurrency"):
        if row.attrib.get("currency") == "BASE_SUMMARY":
            try:
                cash = float(row.attrib.get("endingCash", 0))
            except ValueError:
                pass

    nav = None
    eq_rows = list(root.iter("EquitySummaryByReportDateInBase"))
    if eq_rows:
        try:
            nav = float(eq_rows[-1].attrib.get("total", 0))
        except ValueError:
            pass
    if nav is None:                        # fallback: פוזיציות + מזומן
        if positions or cash is not None:
            nav = sum(p["value"] for p in positions) + (cash or 0)

    if nav is None:
        return None
    return {"nav": round(nav, 2), "cash": round(cash or 0, 2),
            "positions": sorted(positions, key=lambda p: -abs(p["value"]))}


async def refresh() -> dict | None:
    """מושך דוח טרי ושומר צילום יומי. מחזיר את הסיכום או None."""
    if not configured():
        return None
    try:
        root = await _fetch_xml()
    except Exception as exc:
        logger.error("IBKR fetch נכשל: %s", exc)
        return None
    if root is None:
        return None
    data = _parse(root)
    if data is None:
        logger.error("IBKR: לא הצלחתי לחלץ שווי מהדוח — בדוק את סקשני ה-Flex Query")
        return None
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    db.save_portfolio_snapshot(today, data["nav"], data["cash"],
                               json.dumps(data["positions"], ensure_ascii=False))
    return data


def summary(max_positions: int = 8) -> dict | None:
    """הסיכום מהצילום האחרון + שינוי מול הצילום הקודם. לשימוש הכלי והדאשבורד."""
    hist = db.portfolio_history(limit=2)
    if not hist:
        return None
    latest = hist[0]
    positions = json.loads(latest.get("positions") or "[]")[:max_positions]

    prev_prices = {}
    if len(hist) > 1:
        for p in json.loads(hist[1].get("positions") or "[]"):
            if p.get("price"):
                prev_prices[p["symbol"]] = p["price"]
    for p in positions:
        prev = prev_prices.get(p["symbol"])
        p["chg"] = round((p["price"] / prev - 1) * 100, 2) if prev and p.get("price") else None
    out = {"date": latest["snap_date"], "nav": latest["nav"],
           "cash": latest["cash"], "positions": positions,
           "change": None, "change_pct": None}
    if len(hist) > 1 and hist[1]["nav"]:
        out["change"] = round(latest["nav"] - hist[1]["nav"], 2)
        out["change_pct"] = round(out["change"] / hist[1]["nav"] * 100, 2)
    return out


def brief_line() -> str:
    """שורת התיק לבריף. ריק אם אין נתונים או שהחיבור לא מוגדר."""
    s = summary()
    if not s:
        return ""
    line = f"📈 תיק IBKR: ${s['nav']:,.0f}"
    if s["change_pct"] is not None:
        arrow = "▲" if s["change"] >= 0 else "▼"
        line += f" ({arrow}{abs(s['change_pct'])}% · {'+' if s['change'] >= 0 else '-'}${abs(s['change']):,.0f})"
    return line
