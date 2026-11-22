"""
המבצעים: מה שקורה אחרי שלחצת "אשר".

העטיפה של CaliBOT התבררה כלא מתאימה בשלושה דברים:
  1. היא לא שומרת ולא מחזירה location — כלומר כל חישוב הנסיעה מת
  2. create_event מצפה ל-date + start_time נפרדים, לא ל-ISO
  3. query_events יודעת יום בודד בלבד, ולא טווח כמו "השבוע"
לכן כאן פונים ישירות לאובייקט השירות של גוגל. get_calendar_service()
עדיין מטפל בכל ה-OAuth והרענון — לא נגענו בזה.

חיבור ב-main.py:
    import executors
    executors.set_calendar_service(calendar_service)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import agent
from app import db
from app import websearch, habits as habits_mod, nutrition, ibkr, memfiles, gmail_sender
from app.confirm import register_executor

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")
CAL_ID = "primary"

# פלטת הצבעים של יומן גוגל קבועה — 11 צבעים בלבד (colorId).
# אין חום אמיתי בפלטה; "נסיעה" ממופה לכתום, הקרוב ביותר.
CATEGORY_COLORS = {
    "עבודה":   "10",  # ירוק כהה (Basil)
    "נסיעה":   "6",   # כתום (Tangerine)
    "אימון":   "11",  # אדום (Tomato)
    "לימודים": "3",   # סגול (Grape)
    "אישי":    "7",   # טורקיז (Peacock)
    "פרוייקט": "9",   # כחול כהה (Blueberry)
    "טלפוני":  "8",   # אפור (Graphite)
}

_calendar = None
_owner_chat_id: Optional[int] = None

# אירועים שנוצרו לאחרונה, לטובת ביטול
_last_created: dict[int, str] = {}


def set_calendar_service(service, owner_chat_id: int = None) -> None:
    global _calendar, _owner_chat_id
    _calendar = service
    _owner_chat_id = owner_chat_id
    agent.set_read_executor("query_events", fetch_events)
    from app import features
    features.register(service, owner_chat_id)
    agent.set_read_executor("list_tasks", list_tasks)
    agent.set_read_executor("search_events", search_events)
    agent.set_read_executor("remember_preference", remember_preference)
    agent.set_read_executor("web_search", web_search)
    agent.set_read_executor("search_knowledge", search_knowledge)
    agent.set_read_executor("list_scheduled", list_scheduled)
    agent.set_read_executor("check_email", check_email)
    agent.set_read_executor("workout_stats", workout_stats)
    agent.set_read_executor("log_habit", log_habit)
    agent.set_read_executor("habits_status", habits_status)
    agent.set_read_executor("log_meal", log_meal)
    agent.set_read_executor("nutrition_today", nutrition_today)
    agent.set_read_executor("portfolio", portfolio)
    agent.set_read_executor("read_notes", read_notes)
    agent.set_read_executor("read_journal", read_journal)
    agent.set_read_executor("shopping", shopping)
    agent.set_read_executor("forget_preference", forget_preference)
    agent.set_read_executor("meeting_stats", meeting_stats)
    agent.set_read_executor("search_events", search_events)
    agent.set_read_executor("remember_preference", remember_preference)
    agent.set_read_executor("web_search", web_search)
    agent.set_read_executor("search_knowledge", search_knowledge)
    agent.set_read_executor("list_scheduled", list_scheduled)
    agent.set_read_executor("check_email", check_email)
    agent.set_read_executor("workout_stats", workout_stats)
    agent.set_read_executor("log_habit", log_habit)
    agent.set_read_executor("habits_status", habits_status)
    agent.set_read_executor("log_meal", log_meal)
    agent.set_read_executor("nutrition_today", nutrition_today)
    agent.set_read_executor("portfolio", portfolio)
    agent.set_read_executor("read_notes", read_notes)
    agent.set_read_executor("read_journal", read_journal)
    agent.set_read_executor("shopping", shopping)
    agent.set_read_executor("forget_preference", forget_preference)
    agent.set_read_executor("meeting_stats", meeting_stats)


def _service():
    """אובייקט ה-API של גוגל, או None אם אין אימות."""
    if _calendar is None:
        return None
    return _calendar.get_calendar_service()


def _tz_name() -> str:
    try:
        return _calendar.get_user_timezone() or "Asia/Jerusalem"
    except Exception:
        return "Asia/Jerusalem"


def _wall(value: str) -> str:
    """
    שעון קיר מקומי, בלי היסט. המודל מעתיק לפעמים +03:00 גם בשעון חורף,
    וגוגל מכבד את ההיסט על פני timeZone — האירוע זז שעה. השעה שהמשתמש
    אמר היא שעון קיר בישראל, אז זורקים את ההיסט ונותנים ל-timeZone להכריע.
    """
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        return value


# ------------------------------------------------- כלי קריאה

async def fetch_events(start: datetime, end: datetime) -> list[dict]:
    """
    [{'id','summary','start','end','location','description'}] עם start/end כ-datetime.
    זו גם הפונקציה שהמתזמן מקבל דרך Deps.
    """
    service = _service()
    if service is None:
        return []

    # מחרוזת מהמודל = שעון קיר בישראל; ההיסט שלו לא אמין (קיץ/חורף)
    if isinstance(start, str):
        start = datetime.fromisoformat(_wall(start))
    if isinstance(end, str):
        end = datetime.fromisoformat(_wall(end))
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    if end.tzinfo is None:
        end = end.replace(tzinfo=TZ)

    try:
        result = service.events().list(
            calendarId=CAL_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
    except Exception as exc:
        logger.error("שליפת אירועים נכשלה: %s", exc)
        return []

    events = []
    for ev in result.get("items", []):
        try:
            events.append({
                "id": ev.get("id"),
                "summary": ev.get("summary", "ללא כותרת"),
                "start": _parse(ev["start"]),
                "end": _parse(ev.get("end", ev["start"])),
                "location": ev.get("location", "") or "",
                "description": ev.get("description", "") or "",
                "colorId": ev.get("colorId", ""),
                # משתתפים אחרים (בלי עצמי) — להכנה לפגישה
                "attendees": [a.get("email", "") for a in ev.get("attendees", [])
                              if a.get("email") and not a.get("self")
                              and not a.get("resource")],
                "hangoutLink": ev.get("hangoutLink", "") or "",
            })
        except Exception as exc:
            logger.warning("דילוג על אירוע שלא נפרסר: %s", exc)
    return events


def _parse(value) -> datetime:
    """גוגל מחזיר dateTime, או date לאירוע יום שלם."""
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


async def search_events(query: str, days_back: int = 180) -> list[dict]:
    """
    חיפוש טקסט חופשי ביומן. גוגל מחפשת בכותרת, בתיאור ובמיקום.
    היומן הוא ארכיון — זו הדרך לשאול אותו שאלות.
    """
    service = _service()
    if service is None:
        return []

    now = datetime.now(TZ)
    try:
        result = service.events().list(
            calendarId=CAL_ID,
            q=query,
            timeMin=(now - timedelta(days=days_back)).isoformat(),
            timeMax=(now + timedelta(days=365)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=25,
        ).execute()
    except Exception as exc:
        logger.error("חיפוש אירועים נכשל: %s", exc)
        return []

    found = []
    for ev in result.get("items", []):
        try:
            found.append({
                "summary": ev.get("summary", "ללא כותרת"),
                "when": _parse(ev["start"]).strftime("%d/%m/%Y %H:%M"),
                "location": ev.get("location", "") or "",
            })
        except Exception:
            continue
    return found


async def meeting_stats(days: int = 30) -> dict:
    """כמה פגישות ובכמה שעות, בתקופה האחרונה."""
    now = datetime.now(TZ)
    events = await fetch_events(now - timedelta(days=days), now)

    total_minutes = 0
    for ev in events:
        total_minutes += int((ev["end"] - ev["start"]).total_seconds() // 60)

    return {
        "תקופה_בימים": days,
        "מספר_אירועים": len(events),
        "סך_שעות": round(total_minutes / 60, 1),
        "ממוצע_לאירוע_בדקות": round(total_minutes / len(events)) if events else 0,
    }


async def list_tasks() -> list[dict]:
    if _owner_chat_id is None:
        return []
    return [{"id": t["id"], "title": t["title"], "soft_due": t["soft_due"]}
            for t in db.list_open_tasks(_owner_chat_id)]


# ------------------------------------------------ כלי כתיבה

def _rrule(recurrence) -> Optional[str]:
    """
    'WEEKLY' או 'WEEKLY,10' -> כלל RRULE של גוגל.
    בלי מספר חזרות האירוע נמשך ללא הגבלה.
    """
    if not recurrence:
        return None
    parts = str(recurrence).split(",")
    freq = parts[0].strip().upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return None
    rule = f"RRULE:FREQ={freq}"
    if len(parts) > 1 and parts[1].strip().isdigit():
        rule += f";COUNT={parts[1].strip()}"
    return rule


@register_executor("create_event")
async def _create_event(payload: dict) -> str:
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."

    tz = _tz_name()
    location = "" if payload.get("virtual") else (payload.get("location") or "")

    body = {
        "summary": payload["title"],
        "description": payload.get("description", ""),
        "start": {"dateTime": _wall(payload["start"]), "timeZone": tz},
        "end": {"dateTime": _wall(payload["end"]), "timeZone": tz},
    }
    if location:
        body["location"] = location

    category = (payload.get("category") or "").strip()
    if payload.get("virtual") and not category:
        category = "טלפוני"
    color = CATEGORY_COLORS.get(category)
    if color:
        body["colorId"] = color

    if payload.get("rrule"):
        rule = payload["rrule"]
        body["recurrence"] = [rule if rule.startswith("RRULE") else f"RRULE:{rule}"]
    if payload.get("virtual"):
        body["description"] = (body["description"] + "\nפגישה טלפונית").strip()

    rule = _rrule(payload.get("recurrence"))
    if rule:
        body["recurrence"] = [rule]

    try:
        created = service.events().insert(calendarId=CAL_ID, body=body).execute()
    except Exception as exc:
        logger.error("יצירת אירוע נכשלה: %s", exc)
        return "לא הצלחתי לקבוע את האירוע."

    if _owner_chat_id:
        _last_created[_owner_chat_id] = created["id"]

    when = _parse(payload["start"]).strftime("%d/%m בשעה %H:%M")
    line = f"נקבע: {payload['title']} — {when}"

    if location:
        from app.travel import waze_link
        line += f"\nניווט: {waze_link(location)}"
    line += "\n\n/undo לביטול"
    return line


@register_executor("update_event")
async def _update_event(payload: dict) -> str:
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."

    tz = _tz_name()
    body = {}
    if payload.get("title"):
        body["summary"] = payload["title"]
    if payload.get("location"):
        body["location"] = payload["location"]
    if payload.get("start"):
        body["start"] = {"dateTime": _wall(payload["start"]), "timeZone": tz}
    if payload.get("end"):
        body["end"] = {"dateTime": _wall(payload["end"]), "timeZone": tz}

    if not body:
        return "לא היה מה לעדכן."

    try:
        service.events().patch(
            calendarId=CAL_ID, eventId=payload["event_id"], body=body
        ).execute()
        return "עודכן."
    except Exception as exc:
        logger.error("עדכון אירוע נכשל: %s", exc)
        return "העדכון נכשל."


@register_executor("delete_event")
async def _delete_event(payload: dict) -> str:
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."
    try:
        service.events().delete(
            calendarId=CAL_ID, eventId=payload["event_id"]
        ).execute()
        return "נמחק."
    except Exception as exc:
        logger.error("מחיקת אירוע נכשלה: %s", exc)
        return "המחיקה נכשלה."


@register_executor("add_task")
async def _add_task(payload: dict) -> str:
    db.add_task(
        _owner_chat_id, payload["title"],
        payload.get("notes"), payload.get("soft_due"),
    )
    return f"נוספה משימה: {payload['title']}"


@register_executor("complete_task")
async def _complete_task(payload: dict) -> str:
    ok = db.complete_task(int(payload["task_id"]))
    return "סומן כבוצע." if ok else "לא מצאתי את המשימה."


@register_executor("log_activity")
async def _log_activity(payload: dict) -> str:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    db.log_activity(_owner_chat_id, today, payload["content"])
    try:
        memfiles.append_journal(payload["content"])
    except Exception as exc:
        logger.warning("כתיבה ליומן הקבצים נכשלה: %s", exc)
    return "נרשם."


# ------------------------------------------------------- ביטול

async def undo(chat_id: int) -> str:
    """/undo — מוחק את האירוע האחרון שנוצר."""
    event_id = _last_created.pop(chat_id, None)
    if not event_id:
        return "אין מה לבטל."
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."
    try:
        service.events().delete(calendarId=CAL_ID, eventId=event_id).execute()
        return "בוטל."
    except Exception as exc:
        logger.error("ביטול נכשל: %s", exc)
        return "הביטול נכשל."


async def search_events(query: str, months_back: int = 6) -> list[dict]:
    """חיפוש טקסטואלי ביומן אחורה — 'מתי נפגשתי עם X לאחרונה'."""
    service = _service()
    if service is None:
        return []
    from datetime import timedelta
    now = datetime.now(TZ)
    try:
        result = service.events().list(
            calendarId=CAL_ID,
            q=query,
            timeMin=(now - timedelta(days=30 * max(1, months_back))).isoformat(),
            timeMax=now.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
    except Exception as exc:
        logger.error("חיפוש אירועים נכשל: %s", exc)
        return []
    return [{
        "summary": ev.get("summary", ""),
        "start": str(_parse(ev["start"])),
        "location": ev.get("location", ""),
    } for ev in result.get("items", [])]


async def meeting_stats(start, end) -> dict:
    """כמה פגישות וכמה שעות בטווח."""
    events = await fetch_events(start, end)
    total_minutes = sum(
        int((e["end"] - e["start"]).total_seconds() // 60) for e in events
    )
    return {
        "count": len(events),
        "hours": round(total_minutes / 60, 1),
        "busiest": max(events, key=lambda e: e["end"] - e["start"])["summary"]
        if events else None,
    }


async def remember_preference(key: str, value: str) -> str:
    db.set_preference(_owner_chat_id, key, value)
    return f"נשמר: {key} = {value}"


async def forget_preference(key: str) -> str:
    ok = db.delete_preference(_owner_chat_id, key)
    return "נמחק." if ok else "לא מצאתי העדפה כזו."


async def web_search(query: str) -> list[dict]:
    return await websearch.search(query)


async def search_knowledge(query: str) -> list[dict]:
    results = db.search_documents(query)
    return results or [{"title": "", "snippet": "לא נמצא כלום בבסיס הידע."}]


async def list_scheduled() -> list[dict]:
    return db.list_scheduled_prompts(_owner_chat_id)


# רישום ג'וב חי נעשה דרך scheduler — מוזרק באתחול כדי למנוע ייבוא מעגלי
_register_prompt_job = None

def set_prompt_job_registrar(fn) -> None:
    global _register_prompt_job
    _register_prompt_job = fn


@register_executor("schedule_prompt")
async def _schedule_prompt(payload: dict) -> str:
    sid = db.add_scheduled_prompt(
        _owner_chat_id, payload["day_of_week"],
        int(payload["hour"]), int(payload["minute"]), payload["prompt"],
    )
    if _register_prompt_job:
        row = {"id": sid, "chat_id": _owner_chat_id, **payload}
        _register_prompt_job(row)
    return f"תוזמן (#{sid}). לביטול: 'תבטלי את התזמון {sid}'."


@register_executor("cancel_scheduled")
async def _cancel_scheduled(payload: dict) -> str:
    sid = int(payload["schedule_id"])
    ok = db.deactivate_scheduled_prompt(sid)
    if ok and _remove_prompt_job:
        _remove_prompt_job(sid)
    return "בוטל." if ok else "לא מצאתי תזמון פעיל כזה."


_remove_prompt_job = None

def set_prompt_job_remover(fn) -> None:
    global _remove_prompt_job
    _remove_prompt_job = fn


async def check_email() -> list[dict]:
    from app import gmail_reader
    emails = await gmail_reader.unread_recent(limit=5)
    return emails or [{"info": "אין מיילים חדשים ביממה האחרונה."}]


# --- אימונים: זיהוי לפי הצבע שאנחנו עצמנו קובעים, או מילות מפתח ---

WORKOUT_COLOR = CATEGORY_COLORS["אימון"]
WORKOUT_WORDS = ("אימון", "כושר", "ריצה", "חדר כושר", "שחייה",
                 "אופניים", "כדורגל", "כדורסל", "יוגה", "הליכה")


def _is_workout(ev: dict) -> bool:
    if ev.get("colorId") == WORKOUT_COLOR:
        return True
    title = (ev.get("summary") or "")
    return any(w in title for w in WORKOUT_WORDS)


async def workout_stats(days: int = 7) -> dict:
    """כמה אימונים, סה"כ זמן, ומתי האחרון — עבור המאמן."""
    from datetime import timedelta
    now = datetime.now(TZ)
    events = await fetch_events(now - timedelta(days=max(1, days)), now)
    workouts = [e for e in events if _is_workout(e)]

    total_min = sum(
        int((e["end"] - e["start"]).total_seconds() // 60) for e in workouts
    )
    last = max((e["end"] for e in workouts), default=None)
    return {
        "count": len(workouts),
        "total_hours": round(total_min / 60, 1),
        "days_since_last": (now - last).days if last else None,
        "titles": [e["summary"] for e in workouts],
    }


# ------------------------------------------------- הרגלים / תזונה / תיק / זיכרון

async def log_habit(name: str) -> str:
    return habits_mod.mark(_owner_chat_id, name)


async def habits_status() -> list[dict]:
    rows = habits_mod.status(_owner_chat_id)
    return rows or [{"info": "אין הרגלים במעקב. אפשר להוסיף עם 'תעקבי אחרי X'."}]


async def log_meal(description: str, kcal: int = None, protein_g: int = None) -> str:
    return nutrition.log(_owner_chat_id, description, kcal, protein_g)


async def nutrition_today(date: str = None) -> str:
    out = nutrition.day_summary(_owner_chat_id, date)
    return out or "לא נרשמו ארוחות ביום הזה."


async def portfolio() -> dict | str:
    if not ibkr.configured():
        return "חיבור IBKR לא מוגדר עדיין (חסר IBKR_FLEX_TOKEN בשרת)."
    s = ibkr.summary()
    if s is None:
        # אין צילום — ננסה למשוך עכשיו (איטי, אבל חד-פעמי)
        await ibkr.refresh()
        s = ibkr.summary()
    return s or "לא הצלחתי למשוך נתונים מ-IBKR. בדוק את הטוקן וה-Query."


async def read_notes(topic: str = None) -> str:
    if topic:
        note = memfiles.read_note(topic)
        return note or f"אין פתק בנושא '{topic}'. הפתקים הקיימים: {', '.join(memfiles.list_notes()) or 'אין'}."
    notes = memfiles.list_notes()
    return "הפתקים: " + ", ".join(notes) if notes else "אין פתקים עדיין."


async def read_journal(days: int = 7) -> str:
    out = memfiles.recent_journal(days=days)
    return out or "היומן ריק בימים האלה."


@register_executor("add_habit")
async def _add_habit(payload: dict) -> str:
    name = payload["name"].strip()
    if db.find_habit(_owner_chat_id, name):
        return f"כבר יש הרגל בשם '{name}'."
    db.add_habit(_owner_chat_id, name)
    return f"מעכשיו אני עוקבת אחרי '{name}'. תגיד לי כשביצעת ואסמן."


@register_executor("remove_habit")
async def _remove_habit(payload: dict) -> str:
    h = db.find_habit(_owner_chat_id, payload["name"].strip())
    if not h:
        return "לא מצאתי הרגל כזה."
    db.remove_habit(_owner_chat_id, h["name"])
    return f"הפסקתי לעקוב אחרי '{h['name']}'."


@register_executor("save_note")
async def _save_note(payload: dict) -> str:
    name = memfiles.write_note(payload["topic"], payload["content"])
    return f"נשמר בפתק '{name}'."


# ------------------------------------------------- קניות ומייל

async def shopping(action: str, item: str = None) -> str:
    if action == "add" and item:
        db.shopping_add(_owner_chat_id, item.strip())
        items = db.shopping_list(_owner_chat_id)
        return f"הוספתי. ברשימה ({len(items)}): " + ", ".join(items)
    if action == "remove" and item:
        ok = db.shopping_remove(_owner_chat_id, item.strip())
        return "הורדתי מהרשימה." if ok else f"לא מצאתי '{item}' ברשימה."
    if action == "clear":
        n = db.shopping_clear(_owner_chat_id)
        return f"הרשימה נוקתה ({n} פריטים)."
    items = db.shopping_list(_owner_chat_id)
    if not items:
        return "רשימת הקניות ריקה."
    return "🛒 רשימת הקניות:\n" + "\n".join(f"• {i}" for i in items)


@register_executor("send_email")
async def _send_email(payload: dict) -> str:
    ok = await gmail_sender.send(payload["to"], payload["subject"], payload["body"])
    return f"המייל נשלח אל {payload['to']}." if ok else "השליחה נכשלה — בדוק את הלוג."


@register_executor("reply_email")
async def _reply_email(payload: dict) -> str:
    return await gmail_sender.send_reply(payload["email_id"], payload["body"])
