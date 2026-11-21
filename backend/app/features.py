"""
פיצ'רים שנוספו בנובמבר 2026 (סבב 2):

  הקלטות         הרצאה/פגישה מוקלטת -> תמלול במקטעים -> סיכום, נקודות
                 מפתח ומשימות לאישור. נשמר בבסיס הידע.
  שיבוץ משימות   find_free_slots — חלונות פנויים ביומן, והמודל מציע
                 לשבץ בהם בלוקים (create_event רגיל, עם אישור).
  הכנה לפגישה    30 דק' לפני פגישה: משתתפים, מיילים אחרונים, פתקים,
                 פגישות קודמות.
  סיכום קישור    דף / PDF / YouTube -> סיכום בעברית, נשמר בבסיס הידע.
  חדשות שוק      שורה לכל מניה מובילה בתיק, לבריף הבוקר.
  הוצאות         צילום קבלה או "שילמתי 50 על דלק" -> טבלת הוצאות.
  Google Drive   חיפוש, קריאה (Docs/Sheets/PDF), הוספת שורה לגיליון.
  זיכרון שלומד   כל לילה: עובדות חדשות מהשיחות -> memory/profile.md,
                 שנכנס להנחיות של ניקול.

env (ברירות מחדל בסוגריים):
  PREP_MINUTES (30)  LEARN_HOUR (23) LEARN_MINUTE (40)
  WORK_START (08:00) WORK_END (22:00) SKIP_SHABBAT (1)
  NEWS_SYMBOLS (ריק = 4 הפוזיציות הגדולות)
"""

import asyncio
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from app import db, memfiles, policy, travel
from app.confirm import register_executor

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Jerusalem")

PREP_MINUTES = int(os.getenv("PREP_MINUTES", 30))
LEARN_HOUR = int(os.getenv("LEARN_HOUR", 23))
LEARN_MINUTE = int(os.getenv("LEARN_MINUTE", 40))
WORK_START = os.getenv("WORK_START", "08:00")
WORK_END = os.getenv("WORK_END", "22:00")
SKIP_SHABBAT = os.getenv("SKIP_SHABBAT", "1") != "0"

TG_MAX = 3900          # מתחת ל-4096 של טלגרם
TG_FILE_LIMIT = 20 * 1024 * 1024   # בוטים לא יכולים להוריד מעל זה

_calendar = None
_owner: Optional[int] = None
_background: set = set()   # משימות רקע חיות (שלא ייאספו באמצע)


def register(calendar_service, owner_chat_id) -> None:
    """נקרא מ-executors.set_calendar_service."""
    global _calendar, _owner
    _calendar = calendar_service
    try:
        _owner = int(owner_chat_id) if owner_chat_id else None
    except (TypeError, ValueError):
        _owner = None
    _init_tables()
    from app import agent
    for name, fn in READ_EXECUTORS.items():
        agent.set_read_executor(name, fn)


# ================================================================ עזרים

async def _llm(prompt: str, max_tokens: int = 2000, long: bool = False) -> Optional[str]:
    from app import agent
    return await agent.complete_text(prompt, max_tokens=max_tokens, long=long)


def _parse_json(raw: Optional[str]):
    if not raw:
        return None
    clean = raw.replace("```json", "").replace("```", "").strip()
    m = re.search(r"[\[{].*[\]}]", clean, re.S)
    try:
        return json.loads(m.group(0) if m else clean)
    except ValueError:
        logger.warning("JSON לא נקרא: %r", clean[:200])
        return None


async def send_long(chat_id: int, text: str) -> None:
    """שליחה מפוצלת — טלגרם חותך מעל 4096 תווים."""
    from app.services.telegram import send_telegram_message
    while text:
        if len(text) <= TG_MAX:
            await send_telegram_message(chat_id, text)
            return
        cut = text.rfind("\n", 0, TG_MAX)
        cut = cut if cut > TG_MAX // 2 else TG_MAX
        await send_telegram_message(chat_id, text[:cut])
        text = text[cut:].lstrip("\n")


def _is_all_day(e: dict) -> bool:
    return (e["start"].hour == 0 and e["start"].minute == 0
            and (e["end"] - e["start"]) >= timedelta(hours=23))


# ================================================================ 8. הוצאות

def _init_tables() -> None:
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS expenses ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " chat_id INTEGER NOT NULL, spent_on TEXT NOT NULL,"
            " amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'ILS',"
            " category TEXT, merchant TEXT, note TEXT, created_at TEXT)")


CUR = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}


def _money(amount: float, currency: str = "ILS") -> str:
    sym = CUR.get(currency, currency + " ")
    return f"{sym}{amount:,.2f}".replace(".00", "")


def _month_totals(chat_id: int, month: str) -> dict:
    """{currency: {category: total}} לחודש YYYY-MM."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT currency, COALESCE(category,'אחר') AS cat, SUM(amount) AS s "
            "FROM expenses WHERE chat_id = ? AND substr(spent_on,1,7) = ? "
            "GROUP BY currency, cat ORDER BY s DESC", (chat_id, month)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["currency"], {})[r["cat"]] = r["s"]
    return out


def add_expense(chat_id: int, amount: float, category: str = "אחר",
                merchant: str = "", currency: str = "ILS",
                spent_on: str = "", note: str = "") -> str:
    today = datetime.now(TZ).date()
    try:
        d = date.fromisoformat(spent_on) if spent_on else today
        if d > today or (today - d).days > 60:   # תאריך משונה מקבלה -> היום
            d = today
    except ValueError:
        d = today
    currency = (currency or "ILS").upper()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, spent_on, amount, currency, category,"
            " merchant, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (chat_id, d.isoformat(), float(amount), currency,
             (category or "אחר").strip(), (merchant or "").strip(),
             (note or "").strip(), datetime.now(TZ).isoformat()))
    month = d.strftime("%Y-%m")
    totals = _month_totals(chat_id, month).get(currency, {})
    where = f" ({merchant})" if merchant else ""
    line = (f"נרשמה הוצאה: {_money(float(amount), currency)} · {category or 'אחר'}{where}"
            f" · {d:%d/%m}")
    if totals:
        line += (f"\nהחודש: {_money(sum(totals.values()), currency)} · "
                 f"{category or 'אחר'}: {_money(totals.get(category or 'אחר', 0), currency)}")
    return line


async def log_expense(amount: float, category: str = "אחר", merchant: str = "",
                      currency: str = "ILS", date: str = "", note: str = "") -> str:
    return add_expense(_owner, amount, category, merchant, currency, date, note)


async def expenses_summary(month: str = "") -> str:
    month = month or datetime.now(TZ).strftime("%Y-%m")
    totals = _month_totals(_owner, month)
    if not totals:
        return f"לא נרשמו הוצאות ב-{month}."
    first = date.fromisoformat(month + "-01")
    prev = (first - timedelta(days=1)).strftime("%Y-%m")
    prev_totals = _month_totals(_owner, prev)
    lines = [f"💳 הוצאות {first:%m/%Y}"]
    for cur, cats in totals.items():
        total = sum(cats.values())
        p = sum(prev_totals.get(cur, {}).values())
        cmp = ""
        if p:
            diff = (total - p) / p * 100
            cmp = f" (חודש קודם {_money(p, cur)}, {'+' if diff >= 0 else ''}{diff:.0f}%)"
        lines.append(f"סה\"כ: {_money(total, cur)}{cmp}")
        for cat, s in cats.items():
            lines.append(f"  {cat}: {_money(s, cur)}")
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT spent_on, amount, currency, category, merchant FROM expenses "
            "WHERE chat_id = ? AND substr(spent_on,1,7) = ? ORDER BY id DESC LIMIT 5",
            (_owner, month)).fetchall()
    if rows:
        lines.append("אחרונות:")
        for r in rows:
            lines.append(f"  {r['spent_on'][8:10]}/{r['spent_on'][5:7]} "
                         f"{_money(r['amount'], r['currency'])} {r['merchant'] or r['category']}")
    return "\n".join(lines)


async def delete_last_expense() -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM expenses WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
                           (_owner,)).fetchone()
        if not row:
            return "אין הוצאות למחוק."
        conn.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))
    return f"נמחקה: {_money(row['amount'], row['currency'])} {row['merchant'] or row['category']}"


def expenses_week_line(chat_id: int, since: datetime) -> str:
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT currency, COALESCE(category,'אחר') AS cat, SUM(amount) AS s FROM expenses "
                "WHERE chat_id = ? AND spent_on >= ? GROUP BY currency, cat ORDER BY s DESC",
                (chat_id, since.strftime("%Y-%m-%d"))).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    ils = [r for r in rows if r["currency"] == "ILS"] or rows
    cur = ils[0]["currency"]
    total = sum(r["s"] for r in ils)
    top = ", ".join(f"{r['cat']} {_money(r['s'], cur)}" for r in ils[:3])
    return f"💳 הוצאות השבוע: {_money(total, cur)} · {top}"


# ================================================================ 2. חלונות פנויים

def _hm(value: str, default: str) -> tuple[int, int]:
    try:
        h, m = (value or default).split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


async def find_free_slots(duration_minutes: int = 60, days: int = 7,
                          earliest: str = "", latest: str = "",
                          start_date: str = "", max_slots: int = 10) -> list[dict]:
    """
    חלונות פנויים באורך duration_minutes לפחות. אירוע עם כתובת מחוץ לבית
    חוסם גם חצי שעה לפני ואחרי (נסיעה), בלי לקרוא לשירות המפות.
    """
    from app import executors
    now = datetime.now(TZ)
    first = now.date()
    if start_date:
        try:
            first = max(first, date.fromisoformat(start_date[:10]))
        except ValueError:
            pass
    days = max(1, min(int(days or 7), 21))
    duration = max(15, int(duration_minutes or 60))
    eh, em = _hm(earliest, WORK_START)
    lh, lm = _hm(latest, WORK_END)

    range_start = datetime.combine(first, datetime.min.time(), TZ)
    events = await executors.fetch_events(range_start, range_start + timedelta(days=days))
    busy = []
    for e in events:
        if _is_all_day(e):
            continue
        pad = timedelta(0)
        loc = (e.get("location") or "").strip()
        if loc and travel.classify(e) == "physical" and not travel.is_home(loc):
            pad = timedelta(minutes=30)
        busy.append((e["start"] - pad, e["end"] + pad))
    busy.sort()

    slots = []
    for i in range(days):
        d = first + timedelta(days=i)
        wd = d.weekday()          # 4=שישי 5=שבת
        if SKIP_SHABBAT and wd == 5:
            continue
        day_start = datetime(d.year, d.month, d.day, eh, em, tzinfo=TZ)
        day_end = datetime(d.year, d.month, d.day, lh, lm, tzinfo=TZ)
        if SKIP_SHABBAT and wd == 4:
            day_end = min(day_end, datetime(d.year, d.month, d.day, 14, 0, tzinfo=TZ))
        cursor = max(day_start, now + timedelta(minutes=15))
        # עיגול לרבע שעה
        if cursor.minute % 15:
            cursor += timedelta(minutes=15 - cursor.minute % 15)
            cursor = cursor.replace(second=0, microsecond=0)
        for b_start, b_end in busy:
            if b_end <= cursor or b_start >= day_end:
                continue
            if b_start > cursor and (b_start - cursor) >= timedelta(minutes=duration):
                slots.append((cursor, min(b_start, day_end)))
            cursor = max(cursor, b_end)
            if cursor >= day_end:
                break
        if cursor < day_end and (day_end - cursor) >= timedelta(minutes=duration):
            slots.append((cursor, day_end))

    he = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
    out = [{"date": s.strftime("%Y-%m-%d"), "day": he[s.weekday()],
            "from": s.strftime("%H:%M"), "to": e.strftime("%H:%M"),
            "free_minutes": int((e - s).total_seconds() // 60)}
           for s, e in slots[:max(1, min(int(max_slots or 10), 30))]]
    return out or [{"info": "לא נמצאו חלונות פנויים בטווח הזה."}]


# ================================================================ 5. סיכום קישור

YT = re.compile(r"(youtube\.com/(watch|shorts|live)|youtu\.be/)", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SUMMARY_PROMPT = (
    "סכם בעברית את התוכן הבא{focus}.\n"
    "מבנה: שורת כותרת קצרה, ואז 3-7 נקודות עיקריות, ואז שורה 'שורה תחתונה:'. "
    "תמציתי, בלי הקדמות. אם התוכן בשפה אחרת — הסיכום עדיין בעברית.\n\n"
)


def _yt_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", url)
    return m.group(1) if m else ""


def _yt_transcript_sync(video_id: str) -> str:
    """כתוביות (ידניות או אוטומטיות) — בלי וידאו בכלל. עלול להיחסם מ-IP של שרת."""
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    listing = api.list(video_id)
    try:
        tr = listing.find_transcript(["iw", "he", "en"])
    except Exception:
        tr = next(iter(listing))
    return " ".join(s.text for s in tr.fetch())


async def _summarize_youtube(url: str, focus_txt: str) -> str:
    """
    שני מסלולים:
      1. כתוביות (youtube-transcript-api) -> סיכום טקסט. זול ומהיר, וגם
         לסרטונים ארוכים מאוד. נכשל כשאין כתוביות או כש-YouTube חוסם את השרת.
      2. Gemini צופה בסרטון ב-Vertex, ברזולוציה נמוכה (~100 טוקנים לשנייה,
         כך ששעה ~360K טוקנים — בתוך מגבלת המודל; ברזולוציה רגילה שעה חורגת).
    """
    from app import vision
    vid = _yt_id(url)
    if not vid:
        return "לא זיהיתי מזהה סרטון בקישור."
    canonical = f"https://www.youtube.com/watch?v={vid}"
    summary, why = None, []

    try:
        transcript = await asyncio.to_thread(_yt_transcript_sync, vid)
        if len(transcript) > 300:
            summary = await _llm(
                SUMMARY_PROMPT.format(focus=focus_txt)
                + "(תמלול של סרטון YouTube)\n\n" + transcript[:400_000],
                max_tokens=4000, long=True)
    except ImportError:
        why.append("youtube-transcript-api לא מותקן")
    except Exception as exc:
        why.append(f"כתוביות: {type(exc).__name__}")
        logger.info("כתוביות YouTube נכשלו: %s", exc)

    if not summary:
        if vision._load_credentials() is None:
            return "סיכום YouTube דורש את חשבון השירות של Vertex."
        logger.info("YouTube: שולחת ל-Gemini (רזולוציה נמוכה): %s", canonical)
        summary = await vision.vertex_call(
            [{"file_data": {"file_uri": canonical, "mime_type": "video/*"}},
             {"text": SUMMARY_PROMPT.format(focus=focus_txt) + "(זה סרטון YouTube)"}],
            max_tokens=8192, timeout=600,
            extra_config={"mediaResolution": "MEDIA_RESOLUTION_LOW"})
        if not summary:
            err = (vision.last_error or "").lower()
            if "exceed" in err or "token" in err or "too long" in err:
                reason = "הסרטון ארוך מדי לעיבוד"
            elif "permission" in err or "private" in err or "not accessible" in err:
                reason = "הסרטון פרטי או חסום לגישה"
            elif not err:
                reason = "תם הזמן או שהשירות לא ענה"
            else:
                reason = "Vertex החזיר שגיאה"
            why.append(reason)
            logger.error("סיכום YouTube נכשל (%s): %s", url, vision.last_error[:300])
            return ("לא הצלחתי לסכם את הסרטון: " + "; ".join(why)
                    + ". הפרטים המלאים בלוג.")

    db.add_document(f"🎬 {summary.splitlines()[0][:80]}", f"{canonical}\n\n{summary}")
    return summary + "\n\n(נשמר בבסיס הידע)"


def _html_to_text(raw: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    try:
        import trafilatura
        text = trafilatura.extract(raw, include_comments=False) or ""
        if len(text) > 200:
            return title, text
    except ImportError:
        pass
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return title, text.strip()


async def summarize_link(url: str, focus: str = "") -> str:
    url = (url or "").strip()
    if not url.startswith("http"):
        return "צריך קישור מלא שמתחיל ב-http."
    focus_txt = f", עם דגש על: {focus}" if focus else ""

    if YT.search(url):
        # סרטון ארוך יכול לקחת כמה דקות — ברקע, כדי שהוובהוק של טלגרם
        # לא ימתין (ויישלח שוב) בזמן ש-Gemini צופה בסרטון.
        async def _job():
            result = await _summarize_youtube(url, focus_txt)
            await send_long(_owner, "🎬 " + result)
        task = asyncio.create_task(_job())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return ("התחלתי לסכם את הסרטון ברקע — הסיכום יגיע בהודעה נפרדת תוך "
                "כמה דקות. אמור למשתמש בקצרה שזה בטיפול.")

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                     headers={"User-Agent": UA}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            body = resp.content[:8 * 1024 * 1024]
    except Exception as exc:
        logger.warning("הורדת קישור נכשלה: %s", exc)
        return f"לא הצלחתי לפתוח את הקישור ({type(exc).__name__})."

    title = ""
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(body))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as exc:
            return f"לא הצלחתי לקרוא את ה-PDF ({exc})."
    else:
        title, text = _html_to_text(body.decode(resp.encoding or "utf-8", errors="replace"))

    text = text.strip()
    if len(text) < 200:
        return "לא מצאתי בדף מספיק טקסט לסכם (אולי דורש התחברות או נטען ב-JavaScript)."
    summary = await _llm(SUMMARY_PROMPT.format(focus=focus_txt) + text[:120_000],
                         max_tokens=4000, long=len(text) > 12_000)
    if not summary:
        return "לא הצלחתי לסכם כרגע. נסה שוב בעוד דקה."
    db.add_document(f"🔗 {(title or summary.splitlines()[0])[:80]}",
                    f"{url}\n\n{summary}\n\n---\n{text[:150_000]}")
    return summary + "\n\n(נשמר בבסיס הידע — אפשר לשאול עליו אחר כך)"


# ================================================================ 7. חדשות שוק

def _news_sync(symbol: str) -> list[dict]:
    from ddgs import DDGS
    with DDGS() as d:
        return list(d.news(f"{symbol} stock", region="us-en", timelimit="d",
                           max_results=4))


async def market_news(symbols: str = "") -> str:
    """חדשות 24 השעות האחרונות על המניות בתיק (או על symbols, מופרדים בפסיק)."""
    syms = [s.strip().upper() for s in (symbols or os.getenv("NEWS_SYMBOLS", "")).split(",")
            if s.strip()]
    if not syms:
        from app import ibkr
        s = ibkr.summary(max_positions=10)
        syms = [p["symbol"] for p in (s or {}).get("positions", [])
                if p.get("value", 0) > 100][:4]
    if not syms:
        return ""
    raw = []
    for sym in syms:
        try:
            items = await asyncio.to_thread(_news_sync, sym)
        except Exception as exc:
            logger.warning("חדשות %s נכשלו: %s", sym, exc)
            items = []
        for it in items:
            raw.append(f"[{sym}] {it.get('title','')} — {it.get('source','')}: "
                       f"{(it.get('body') or '')[:200]}")
    if not raw:
        return ""
    text = await _llm(
        "אלה כותרות חדשות מ-24 השעות האחרונות על מניות בתיק שלי.\n"
        f"כתוב שורה אחת בעברית לכל מניה מהרשימה {', '.join(syms)}, בפורמט "
        "'SYMBOL: מה קרה ולמה זה חשוב'. אם אין משהו מהותי — 'SYMBOL: אין חדש מהותי'. "
        "אם מוזכר דוח רבעוני קרוב — ציין את התאריך. בלי הקדמות.\n\n" + "\n".join(raw),
        max_tokens=1200)
    return text or ""


# ================================================================ 7. Google Drive

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/spreadsheets"]
NEED_REAUTH = ("אין לי עדיין הרשאה ל-Google Drive. שלח /reauth ואשר מחדש את "
               "ההרשאות של גוגל (כולל Drive ו-Sheets).")


def _creds():
    creds = getattr(_calendar, "credentials", None) if _calendar else None
    if creds is None:
        return None
    try:
        if not creds.has_scopes(DRIVE_SCOPES):
            return None
    except Exception:
        pass
    return creds


def _drive():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _search_sync(query: str, limit: int, mime: str = "") -> list[dict]:
    q = f"(name contains '{_q(query)}' or fullText contains '{_q(query)}') and trashed = false"
    if mime:
        q += f" and mimeType = '{mime}'"
    res = _drive().files().list(
        q=q, pageSize=limit, spaces="drive",
        fields="files(id,name,mimeType,modifiedTime,webViewLink,size)").execute()
    return res.get("files", [])


async def drive_search(query: str, limit: int = 8) -> list[dict] | str:
    if _creds() is None:
        return NEED_REAUTH
    try:
        files = await asyncio.to_thread(_search_sync, query, min(int(limit or 8), 20))
    except Exception as exc:
        logger.error("חיפוש בדרייב נכשל: %s", exc)
        return f"החיפוש בדרייב נכשל: {exc}"
    if not files:
        return f"לא מצאתי בדרייב קבצים עם '{query}'."
    return [{"id": f["id"], "name": f["name"], "type": f["mimeType"].split(".")[-1],
             "modified": f.get("modifiedTime", "")[:10], "link": f.get("webViewLink", "")}
            for f in files]


def _resolve_sync(file: str, mime: str = "") -> Optional[dict]:
    file = (file or "").strip()
    svc = _drive()
    if re.fullmatch(r"[\w-]{20,}", file):
        try:
            return svc.files().get(fileId=file,
                                   fields="id,name,mimeType,size,webViewLink").execute()
        except Exception:
            pass
    found = _search_sync(file, 1, mime)
    return found[0] if found else None


def _read_sync(meta: dict, max_chars: int) -> str:
    svc = _drive()
    mt = meta["mimeType"]
    fid = meta["id"]
    if mt == "application/vnd.google-apps.document" or mt == "application/vnd.google-apps.presentation":
        data = svc.files().export(fileId=fid, mimeType="text/plain").execute()
        return data.decode("utf-8", errors="replace")[:max_chars]
    if mt == "application/vnd.google-apps.spreadsheet":
        from googleapiclient.discovery import build
        sheets = build("sheets", "v4", credentials=_creds(), cache_discovery=False)
        book = sheets.spreadsheets().get(spreadsheetId=fid).execute()
        out = []
        for sh in book.get("sheets", [])[:5]:
            title = sh["properties"]["title"]
            vals = sheets.spreadsheets().values().get(
                spreadsheetId=fid, range=f"'{title}'!A1:Z200").execute().get("values", [])
            out.append(f"## {title}\n" + "\n".join(" | ".join(r) for r in vals))
        return "\n\n".join(out)[:max_chars]
    if mt.startswith("application/vnd.google-apps"):
        return f"(סוג קובץ {mt} לא נתמך לקריאה)"
    if int(meta.get("size") or 0) > 25 * 1024 * 1024:
        return "(הקובץ גדול מדי לקריאה)"
    raw = svc.files().get_media(fileId=fid).execute()
    if mt == "application/pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((p.extract_text() or "") for p in reader.pages)[:max_chars]
    if mt.startswith("text/") or mt in ("application/json", "application/csv"):
        return raw.decode("utf-8", errors="replace")[:max_chars]
    if mt.startswith("application/vnd.openxmlformats-officedocument.wordprocessingml"):
        try:
            import zipfile
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))[:max_chars]
        except Exception as exc:
            return f"(לא הצלחתי לקרוא docx: {exc})"
    return f"(סוג קובץ {mt} לא נתמך לקריאה)"


async def drive_read(file: str) -> str:
    """file = מזהה קובץ או שם (החיפוש לוקח את התוצאה הראשונה)."""
    if _creds() is None:
        return NEED_REAUTH
    try:
        meta = await asyncio.to_thread(_resolve_sync, file)
        if not meta:
            return f"לא מצאתי בדרייב קובץ בשם '{file}'."
        if meta["mimeType"].startswith(("audio/", "video/")):
            return (f"'{meta['name']}' הוא קובץ הקלטה. כדי לסכם אותו קרא ל-"
                    f"summarize_recording עם file='{meta['id']}'.")
        text = await asyncio.to_thread(_read_sync, meta, 12_000)
        return f"📄 {meta['name']} ({meta.get('webViewLink','')})\n\n{text}"
    except Exception as exc:
        logger.error("קריאה מהדרייב נכשלה: %s", exc)
        return f"הקריאה מהדרייב נכשלה: {exc}"


def _append_sync(spreadsheet: str, values: list, sheet: str) -> str:
    from googleapiclient.discovery import build
    meta = _resolve_sync(spreadsheet, "application/vnd.google-apps.spreadsheet")
    if not meta:
        return f"לא מצאתי גיליון בשם '{spreadsheet}'."
    sheets = build("sheets", "v4", credentials=_creds(), cache_discovery=False)
    if not sheet:
        book = sheets.spreadsheets().get(spreadsheetId=meta["id"]).execute()
        sheet = book["sheets"][0]["properties"]["title"]
    sheets.spreadsheets().values().append(
        spreadsheetId=meta["id"], range=f"'{sheet}'!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [values]}).execute()
    return f"נוספה שורה ל-'{meta['name']}' ({sheet}): {' | '.join(map(str, values))}"


@register_executor("sheet_append")
async def _sheet_append(payload: dict) -> str:
    if _creds() is None:
        return NEED_REAUTH
    values = payload.get("values") or []
    if isinstance(values, str):
        values = [v.strip() for v in values.split("|")]
    try:
        return await asyncio.to_thread(_append_sync, payload["spreadsheet"],
                                       values, payload.get("sheet") or "")
    except Exception as exc:
        logger.error("הוספת שורה נכשלה: %s", exc)
        return f"ההוספה לגיליון נכשלה: {exc}"


# ================================================================ 1. הקלטות

RECORDING_PROMPT = (
    "זה תמלול אוטומטי (עשויות להיות בו שגיאות) של {kind}{title}.\n"
    "החזר JSON בלבד, בלי גרשי קוד, במבנה:\n"
    '{{"title": "כותרת קצרה", "type": "lecture|meeting|other", '
    '"summary": "סיכום של 4-8 משפטים בעברית", '
    '"key_points": ["נקודה", "..."], '
    '"action_items": [{{"title": "משימה קונקרטית", "due": "YYYY-MM-DD או ריק"}}], '
    '"open_questions": ["..."]}}\n'
    "בהרצאה: key_points הם המושגים והטענות המרכזיים (עד 12), ו-action_items "
    "רק אם המרצה ביקש משהו (מטלה, קריאה, מועד). בפגישה: החלטות ומשימות עם אחראי. "
    "אל תמציא משימות. היום {today}.\n\nהתמלול:\n"
)


def media_of(message: dict) -> Optional[dict]:
    """מחזיר את קובץ האודיו/וידאו בהודעה, או None."""
    for key in ("voice", "audio", "video", "video_note"):
        if message.get(key):
            return {**message[key], "_kind": key}
    doc = message.get("document") or {}
    mt = doc.get("mime_type", "")
    if mt.startswith(("audio/", "video/")):
        return {**doc, "_kind": "document"}
    return None


def is_recording(message: dict, short_limit: int = 300) -> bool:
    """הקלטה ארוכה, קובץ אודיו, או קולית עם בקשת סיכום -> מסלול ההקלטות."""
    m = media_of(message)
    if not m:
        return False
    caption = message.get("caption") or ""
    if any(w in caption for w in ("סכמ", "סיכום", "הרצאה", "פגישה", "שיעור", "תמלל")):
        return True
    if m["_kind"] == "voice":
        return (m.get("duration") or 0) > short_limit
    return True


async def _tg_download(file_id: str) -> Optional[bytes]:
    from app.config import TELEGRAM_API_TOKEN as TOKEN
    async with httpx.AsyncClient(timeout=300.0) as client:
        meta = (await client.get(f"https://api.telegram.org/bot{TOKEN}/getFile",
                                 params={"file_id": file_id})).json()
        if not meta.get("ok"):
            return None
        path = meta["result"]["file_path"]
        resp = await client.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}")
        resp.raise_for_status()
        return resp.content


def _drive_download_sync(file: str) -> tuple[Optional[bytes], str]:
    meta = _resolve_sync(file)
    if not meta:
        return None, ""
    return _drive().files().get_media(fileId=meta["id"]).execute(), meta["name"]


def _split_audio(src: Path, workdir: Path, seconds: int = 600) -> tuple[list[Path], int]:
    """ממיר ל-opus מונו 16kHz ומפצל למקטעים של 10 דק' (Whisper מוגבל ל-25MB)."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg לא מותקן בשרת")
    duration = 0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        duration = int(float(out or 0))
    except Exception:
        pass
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn",
         "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
         "-f", "segment", "-segment_time", str(seconds), str(workdir / "part%03d.ogg")],
        check=True, timeout=1800)
    return sorted(workdir.glob("part*.ogg")), duration


async def _transcribe_parts(parts: list[Path]) -> str:
    from app import voice
    texts = []
    for i, p in enumerate(parts):
        data = p.read_bytes()
        text = None
        for attempt in range(3):
            text = await voice.transcribe(data, "audio/ogg")
            if text:
                break
            await asyncio.sleep(30 * (attempt + 1))   # מגבלת קצב של Groq
        texts.append(text or f"[מקטע {i + 1} לא תומלל]")
    return "\n".join(texts)


async def process_recording(chat_id: int, *, file_id: str = "", drive_file: str = "",
                            title: str = "", size: int = 0) -> None:
    """רץ ברקע. שולח הודעת התקדמות, ואז סיכום + הצעת משימות."""
    from app import confirm
    from app.services.telegram import send_telegram_message
    try:
        if file_id and size and size > TG_FILE_LIMIT:
            await send_telegram_message(chat_id, (
                f"הקובץ גדול מ-20MB — טלגרם לא מאפשר לבוטים להוריד אותו.\n"
                "אפשרויות: לשלוח כהודעה קולית (דחוסה יותר), או להעלות ל-Google Drive "
                "ולכתוב לי 'תסכמי את ההקלטה <שם הקובץ> מהדרייב'."))
            return
        if drive_file:
            if _creds() is None:
                await send_telegram_message(chat_id, NEED_REAUTH)
                return
            raw, name = await asyncio.to_thread(_drive_download_sync, drive_file)
            title = title or name
        else:
            raw = await _tg_download(file_id)
        if not raw:
            await send_telegram_message(chat_id, "לא הצלחתי להוריד את ההקלטה.")
            return

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            src = tmpdir / "input.bin"
            src.write_bytes(raw)
            parts, duration = await asyncio.to_thread(_split_audio, src, tmpdir)
            mins = max(1, duration // 60)
            await send_telegram_message(chat_id, (
                f"🎙 קיבלתי הקלטה של ~{mins} דק'. מתמללת ומסכמת — "
                f"זה ייקח בערך {max(1, len(parts) * 1)}-{max(2, len(parts) * 2)} דקות."))
            transcript = await _transcribe_parts(parts)

        if not transcript.strip() or transcript.count("[מקטע") == len(parts):
            await send_telegram_message(chat_id, "התמלול נכשל. נסה שוב מאוחר יותר.")
            return

        kind = "הקלטה" if not title else "הקלטה"
        raw_json = await _llm(
            RECORDING_PROMPT.format(kind=kind, title=f" בשם '{title}'" if title else "",
                                    today=datetime.now(TZ).strftime("%Y-%m-%d"))
            + transcript[:400_000], max_tokens=8000, long=True)
        data = _parse_json(raw_json) or {}
        heading = data.get("title") or title or "הקלטה"
        stamp = datetime.now(TZ).strftime("%d/%m/%Y")

        lines = [f"🎙 {heading} · {stamp} · ~{mins} דק'"]
        if data.get("summary"):
            lines += ["", data["summary"]]
        if data.get("key_points"):
            lines += ["", "נקודות מפתח:"] + [f"• {p}" for p in data["key_points"]]
        if data.get("open_questions"):
            lines += ["", "שאלות פתוחות:"] + [f"• {q}" for q in data["open_questions"]]
        if not data:
            lines += ["", (raw_json or "לא הצלחתי לסכם — התמלול נשמר.")[:3000]]
        lines += ["", "התמלול המלא נשמר בבסיס הידע — אפשר לשאול עליו."]
        summary_text = "\n".join(lines)

        db.add_document(f"🎙 {heading} {stamp}", summary_text + "\n\n---\nתמלול:\n" + transcript)
        await send_long(chat_id, summary_text)

        items = []
        for a in (data.get("action_items") or [])[:10]:
            t = (a.get("title") if isinstance(a, dict) else str(a)) or ""
            if not t.strip():
                continue
            due = (a.get("due") or "") if isinstance(a, dict) else ""
            payload = {"title": t.strip()[:200], "notes": f"מתוך: {heading}"}
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due or ""):
                payload["soft_due"] = due
            items.append({"action_type": "add_task", "payload": payload,
                          "summary": f"משימה: {payload['title']}"
                                     + (f" (עד {due})" if payload.get("soft_due") else ""),
                          "conflicts": []})
        if items:
            await confirm.propose_batch(chat_id, items)
    except Exception as exc:
        logger.exception("עיבוד הקלטה נכשל")
        await send_telegram_message(chat_id, f"עיבוד ההקלטה נכשל: {exc}")


def start_recording_job(chat_id: int, **kw) -> None:
    task = asyncio.create_task(process_recording(chat_id, **kw))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def summarize_recording(file: str) -> str:
    """כלי לסוכן: הקלטה מ-Google Drive. רץ ברקע."""
    if _creds() is None:
        return NEED_REAUTH
    start_recording_job(_owner, drive_file=file)
    return ("התחלתי לעבד את ההקלטה מהדרייב ברקע. הסיכום יגיע בהודעה נפרדת "
            "בעוד כמה דקות. אמור למשתמש בקצרה שזה בטיפול.")


# ================================================================ 3. הכנה לפגישה

MEETING_WORDS = ("פגישה", "פגישת", "שיחה", "ראיון", "meeting", "call", "sync", "1:1", "עם ")
_prepped: dict[str, datetime] = {}


def _looks_like_meeting(e: dict) -> bool:
    if e.get("attendees"):
        return True
    title = (e.get("summary") or "").lower()
    return any(w in title for w in MEETING_WORDS)


def _person_from_title(title: str) -> str:
    m = re.search(r"עם\s+(.+)$", title or "")
    if not m:
        return ""
    name = re.split(r"[-–,(|]", m.group(1))[0].strip()
    return name if 1 < len(name) <= 30 else ""


async def _emails_about(query: str, limit: int = 3) -> list[dict]:
    from app import gmail_reader
    try:
        return await asyncio.to_thread(gmail_reader._fetch_sync, query, limit)
    except Exception as exc:
        logger.info("חיפוש מיילים להכנה נכשל: %s", exc)
        return []


def _notes_mentioning(term: str) -> list[str]:
    hits = []
    if not term:
        return hits
    for name in memfiles.list_notes():
        body = memfiles.read_note(name)
        if term in body or term in name:
            idx = body.find(term)
            snippet = body[max(0, idx - 80): idx + 160].replace("\n", " ") if idx >= 0 else body[:160]
            hits.append(f"{name}: …{snippet.strip()}…")
    return hits[:3]


async def build_meeting_prep(e: dict) -> str:
    from app import executors
    title = e.get("summary") or "פגישה"
    start = e["start"].astimezone(TZ)
    attendees = e.get("attendees") or []
    person = _person_from_title(title)

    emails = []
    for addr in attendees[:3]:
        emails += await _emails_about(f"(from:{addr} OR to:{addr}) newer_than:90d", 3)
    if not attendees and person:
        emails += await _emails_about(f'"{person}" newer_than:60d', 3)

    notes = _notes_mentioning(person) if person else []
    for addr in attendees[:2]:
        notes += _notes_mentioning(addr.split("@")[0])

    past = []
    try:
        key = person or title
        found = await executors.search_events(key, months_back=6)
        past = [p for p in found if p["start"] < datetime.now(TZ)]
    except Exception:
        pass

    if not (attendees or emails or notes or past):
        return ""

    lines = [f"📋 הכנה ל\"{title}\" ב-{start:%H:%M}"]
    if e.get("hangoutLink"):
        lines.append(f"🔗 {e['hangoutLink']}")
    elif e.get("location"):
        lines.append(f"📍 {e['location']}")
    if attendees:
        lines.append("👥 " + ", ".join(attendees[:6]))
    if past:
        last = max(past, key=lambda p: p["start"])
        lines.append(f"🗓 נפגשתם {len(past)} פעמים בחצי השנה האחרונה, "
                     f"לאחרונה ב-{last['start'].astimezone(TZ):%d/%m}")
    if emails:
        lines.append("✉️ מיילים אחרונים:")
        seen = set()
        for m in emails:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            lines.append(f"  • {m['subject']} — {m['snippet'][:90]}")
            if len(seen) >= 4:
                break
    if notes:
        lines.append("📝 מהפתקים:")
        lines += [f"  • {n[:200]}" for n in notes[:3]]
    if e.get("description"):
        lines.append(f"ℹ️ {e['description'][:300]}")
    return "\n".join(lines)


async def meeting_prep_job(send, chat_id: int, fetch_events, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(TZ)
    if policy.in_quiet_hours(now):
        return 0
    for k, t in list(_prepped.items()):
        if now - t > timedelta(days=1):
            _prepped.pop(k, None)
    try:
        events = await fetch_events(now, now + timedelta(minutes=PREP_MINUTES + 10))
    except Exception:
        return 0
    sent = 0
    for e in events:
        if _is_all_day(e) or e["start"] <= now:
            continue
        key = f"{e.get('id')}@{e['start'].isoformat()}"
        if key in _prepped or not _looks_like_meeting(e):
            continue
        lead = e["start"] - now
        if timedelta(minutes=PREP_MINUTES - 5) <= lead <= timedelta(minutes=PREP_MINUTES + 5):
            _prepped[key] = now
            text = await build_meeting_prep(e)
            if text:
                await send(chat_id, text)
                sent += 1
    return sent


# ================================================================ 10. זיכרון שלומד

PROFILE_FILE = "profile.md"
PROFILE_MAX_LINES = 80


def _profile_path() -> Path:
    return Path(memfiles.MEMORY_DIR) / PROFILE_FILE


def profile_lines() -> list[str]:
    p = _profile_path()
    if not p.exists():
        return []
    return [l[2:].strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("- ")]


def _write_profile(lines: list[str]) -> None:
    p = _profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "# מה ניקול יודעת על רון\n\n" + "\n".join(f"- {l}" for l in lines[-PROFILE_MAX_LINES:]) + "\n"
    p.write_text(body, encoding="utf-8")


def profile_block() -> str:
    lines = profile_lines()
    if not lines:
        return ""
    return ("\n\n## מה שלמדת על רון משיחות קודמות — השתמש כשזה רלוונטי, "
            "אל תצטט סתם\n" + "\n".join(f"- {l}" for l in lines))


LEARN_PROMPT = (
    "אתה מנהל את הזיכרון ארוך-הטווח של עוזרת אישית בשם ניקול, עבור רון.\n"
    "לפניך מה שכבר ידוע, ושיחות מ-24 השעות האחרונות.\n"
    "חלץ רק עובדות יציבות ושימושיות לעתיד: אנשים בחייו ומי הם, מקומות קבועים, "
    "הרגלים ושגרה, העדפות (אוכל, זמנים, סגנון), פרויקטים ולימודים, מטרות.\n"
    "לא לשמור: דברים חד-פעמיים, מה שכבר כתוב, השערות שלך, סיסמאות, מספרי "
    "זהות/כרטיס/חשבון, פרטים רפואיים.\n"
    "אם משהו בשיחות סותר עובדה קיימת — הוסף את הגרסה החדשה ושים את הישנה ב-remove.\n"
    'החזר JSON בלבד: {"add": ["עובדה קצרה", ...], "remove": ["שורה קיימת מדויקת", ...]}\n'
    "אם אין מה לשמור: {\"add\": [], \"remove\": []}\n\n"
)


async def learn_from_today(chat_id: Optional[int] = None) -> str:
    chat_id = chat_id or _owner
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? AND created_at >= ? "
            "ORDER BY id", (chat_id, since)).fetchall()
    convo = "\n".join(f"{'רון' if r['role'] == 'user' else 'ניקול'}: {r['content'][:600]}"
                      for r in rows)
    if len(convo) < 80:
        return "אין מספיק שיחה ללמוד ממנה."
    existing = profile_lines()
    raw = await _llm(LEARN_PROMPT + "ידוע כבר:\n" + ("\n".join(f"- {l}" for l in existing) or "(ריק)")
                     + "\n\nשיחות:\n" + convo[-30_000:], max_tokens=2000, long=True)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        return "לא הצלחתי לעבד את הזיכרון הלילה."
    removed = {str(r).strip() for r in data.get("remove") or []}
    lines = [l for l in existing if l not in removed]
    added = []
    for fact in data.get("add") or []:
        fact = re.sub(r"\s+", " ", str(fact)).strip()
        if fact and fact not in lines and len(fact) < 200:
            lines.append(fact)
            added.append(fact)
    if added or removed & set(existing):
        _write_profile(lines)
    logger.info("זיכרון: +%s -%s", len(added), len(removed & set(existing)))
    return f"נוספו {len(added)} עובדות, הוסרו {len(removed & set(existing))}."


async def my_profile() -> str:
    lines = profile_lines()
    if not lines:
        return "עדיין לא שמרתי עובדות עליך מהשיחות. אני לומדת כל לילה."
    return "מה שלמדתי עליך:\n" + "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))


async def forget_fact(text: str) -> str:
    lines = profile_lines()
    keep = [l for l in lines if text.strip() not in l]
    if len(keep) == len(lines):
        return f"לא מצאתי עובדה שמכילה '{text}'."
    _write_profile(keep)
    return f"שכחתי {len(lines) - len(keep)} עובדות שמכילות '{text}'."


# ================================================================ רישום לסוכן

READ_EXECUTORS = {
    "log_expense": log_expense,
    "expenses_summary": expenses_summary,
    "delete_last_expense": delete_last_expense,
    "find_free_slots": find_free_slots,
    "summarize_link": summarize_link,
    "market_news": market_news,
    "drive_search": drive_search,
    "drive_read": drive_read,
    "summarize_recording": summarize_recording,
    "my_profile": my_profile,
    "forget_fact": forget_fact,
}
READ = set(READ_EXECUTORS)
WRITE = {"sheet_append"}

CATS = ["מזון", "מסעדות", "תחבורה", "דלק", "קניות", "בית", "חשבונות",
        "בריאות", "בילוי", "לימודים", "מנויים", "אחר"]


def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                       **({"required": required} if required else {})}}}


TOOLS = [
    _fn("log_expense", "רושם הוצאה ('שילמתי 50 על דלק'). מיידי, בלי אישור.", {
        "amount": {"type": "number"},
        "category": {"type": "string", "enum": CATS},
        "merchant": {"type": "string", "description": "שם העסק, אם ידוע"},
        "currency": {"type": "string", "description": "ILS כברירת מחדל"},
        "date": {"type": "string", "description": "YYYY-MM-DD, ריק = היום"},
        "note": {"type": "string"},
    }, ["amount"]),
    _fn("expenses_summary", "סיכום הוצאות לפי קטגוריה לחודש, עם השוואה לחודש הקודם.", {
        "month": {"type": "string", "description": "YYYY-MM, ריק = החודש"},
    }),
    _fn("delete_last_expense", "מוחק את ההוצאה האחרונה שנרשמה (כשנרשמה בטעות)."),
    _fn("find_free_slots", (
        "מוצא חלונות פנויים ביומן. השתמש כשמבקשים לשבץ משימה, זמן לימוד או "
        "עבודה ('תמצאי לי שעתיים השבוע ל-X'). אחרי זה הצע create_event "
        "בחלון המתאים (כותרת עם ⏳, קטגוריה מתאימה)."), {
        "duration_minutes": {"type": "integer"},
        "days": {"type": "integer", "description": "כמה ימים קדימה (ברירת מחדל 7)"},
        "earliest": {"type": "string", "description": "HH:MM, ריק = 08:00"},
        "latest": {"type": "string", "description": "HH:MM, ריק = 22:00"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD, ריק = היום"},
    }, ["duration_minutes"]),
    _fn("summarize_link", (
        "מסכם דף אינטרנט, PDF מקישור או סרטון YouTube, ושומר בבסיס הידע. "
        "השתמש כשהמשתמש שולח קישור (גם בלי לבקש במפורש)."), {
        "url": {"type": "string"},
        "focus": {"type": "string", "description": "על מה להתמקד, אם ביקש"},
    }, ["url"]),
    _fn("market_news", "חדשות 24 השעות האחרונות על מניות בתיק או על מניות שצוינו.", {
        "symbols": {"type": "string", "description": "למשל 'NVDA,AAPL'. ריק = התיק"},
    }),
    _fn("drive_search", "מחפש קבצים ב-Google Drive לפי שם או תוכן.", {
        "query": {"type": "string"},
    }, ["query"]),
    _fn("drive_read", "קורא קובץ מ-Google Drive (Doc, Sheet, PDF, טקסט) לפי מזהה או שם.", {
        "file": {"type": "string", "description": "מזהה קובץ או שם"},
    }, ["file"]),
    _fn("sheet_append", "מוסיף שורה לגיליון Google Sheets. דורש אישור.", {
        "spreadsheet": {"type": "string", "description": "שם הגיליון או מזהה"},
        "values": {"type": "array", "items": {"type": "string"},
                   "description": "ערכי התאים לפי סדר העמודות"},
        "sheet": {"type": "string", "description": "שם הלשונית, ריק = הראשונה"},
    }, ["spreadsheet", "values"]),
    _fn("summarize_recording", (
        "מתמלל ומסכם הקלטה (הרצאה/פגישה) שנמצאת ב-Google Drive. רץ ברקע."), {
        "file": {"type": "string", "description": "שם הקובץ או מזהה"},
    }, ["file"]),
    _fn("my_profile", "מה ניקול למדה על רון משיחות ('מה את יודעת עליי?')."),
    _fn("forget_fact", "מוחק עובדה מהזיכרון הנלמד ('תשכחי ש...').", {
        "text": {"type": "string", "description": "מילה או ביטוי מתוך העובדה"},
    }, ["text"]),
]
