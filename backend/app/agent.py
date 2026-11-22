"""
ליבת הסוכן — קריאה אחת למודל עם כלים, במקום שלוש קריאות ושרשרת if/else.

עיקרון: כלי קריאה (query_events, list_tasks) מתבצעים מיד ומוחזרים
למודל להמשך ניסוח. כלי כתיבה (create_event, delete_event, add_task)
לא מתבצעים כאן בכלל — הם נכנסים ל-confirm.propose ומחכים לאישור.

זה מה שהופך את "מסכם ומחכה לאישור" מכוונה לארכיטקטורה: אין מסלול
שבו המודל כותב ליומן בלי שאישרת.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from litellm import acompletion

from app import confirm
from app import db
from app import prompts
from app import travel

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")
# הספק והמודל נקבעים מהסביבה. ברירת המחדל: Groq, כי Gemini חסום
# גיאוגרפית מ-IP של דאטהסנטרים (נכון לאוגוסט 2026).
# החלפת מודל = שינוי LLM_MODEL ב-.env והפעלה מחדש, בלי לגעת בקוד.
MODEL = os.getenv("LLM_MODEL", "groq/openai/gpt-oss-120b")

# מודל גיבוי: כש-Groq עמוס/נופל עוברים ל-Gemini ב-Vertex, עם חשבון השירות
# שכבר משמש לתמונות. ריק ב-.env (FALLBACK_MODEL=) = בלי גיבוי.
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "vertex_ai/gemini-2.5-flash")


def _fallback_kwargs() -> Optional[dict]:
    sa = os.getenv("VERTEX_SA_FILE", "")
    if not FALLBACK_MODEL or not sa or not os.path.exists(sa):
        return None
    try:
        with open(sa, encoding="utf-8") as f:
            creds = f.read()
        return {
            "model": FALLBACK_MODEL,
            "vertex_credentials": creds,
            "vertex_project": json.loads(creds).get("project_id"),
            "vertex_location": os.getenv("VERTEX_REGION", "europe-west1"),
        }
    except Exception as exc:
        logger.error("טעינת מודל הגיבוי נכשלה: %s", exc)
        return None
MAX_ROUNDS = 15

WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

# כלים שמחזירים מידע ומתבצעים מיד
# העדפות מתבצעות מיד — הן זולות, הפיכות, ולא נוגעות ביומן
READ_TOOLS = {"query_events", "list_tasks", "search_events", "meeting_stats",
              "remember_preference", "forget_preference",
              "web_search", "search_knowledge", "list_scheduled",
              "check_email", "workout_stats",
              "log_habit", "habits_status", "log_meal", "nutrition_today",
              "portfolio", "read_notes", "read_journal", "shopping"}

# כלים שמשנים משהו ולכן עוברים דרך אישור
WRITE_TOOLS = {"create_event", "update_event", "delete_event",
               "add_task", "complete_task", "log_activity",
               "schedule_prompt", "cancel_scheduled",
               "add_habit", "remove_habit", "save_note",
               "send_email", "reply_email"}

# מטופל בנפרד: הוא לא פעולה חדשה אלא הצעה מחדש של פעולה קיימת
RESTORE_TOOL = "restore_last"

TOOLS = [
    {"type": "function", "function": {
        "name": "create_event",
        "description": "קובע אירוע חדש ביומן. דורש אישור מהמשתמש.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "כותרת האירוע"},
            "start": {"type": "string", "description": "התחלה ISO בשעון ישראל, למשל 2026-11-10T18:00:00"},
            "end": {"type": "string", "description": "סיום ISO בשעון ישראל"},
            "location": {"type": "string", "description": "כתובת. ריק לאירוע וירטואלי"},
            "virtual": {"type": "boolean",
                        "description": "true לפגישה טלפונית/זום — אין נסיעה"},
            "category": {"type": "string",
                         "enum": ["עבודה", "נסיעה", "אימון", "לימודים",
                                  "אישי", "פרוייקט", "טלפוני"],
                         "description": "קטגוריית האירוע — קובעת את הצבע ביומן"},
            "rrule": {"type": "string",
                      "description": ("חזרתיות בפורמט RRULE, למשל "
                                      "RRULE:FREQ=WEEKLY;BYDAY=TU לכל יום שלישי. "
                                      "ריק לאירוע חד-פעמי")},
            "recurrence": {"type": "string", "description": (
                "לאירוע חוזר. אחד מ: DAILY, WEEKLY, MONTHLY, YEARLY. "
                "אפשר להוסיף מספר חזרות אחרי פסיק, למשל 'WEEKLY,10'. "
                "השאר ריק לאירוע חד-פעמי.")},
        }, "required": ["title", "start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "query_events",
        "description": "מחזיר אירועים מהיומן בטווח תאריכים.",
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "תחילת טווח ISO"},
            "end": {"type": "string", "description": "סוף טווח ISO"},
        }, "required": ["start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "update_event",
        "description": "מעדכן אירוע קיים. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "location": {"type": "string"},
        }, "required": ["event_id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_event",
        "description": "מוחק אירוע. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "title": {"type": "string", "description": "לתצוגה באישור"},
        }, "required": ["event_id"]},
    }},
    {"type": "function", "function": {
        "name": "add_task",
        "description": "מוסיף משימה בלי זמן קבוע. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "notes": {"type": "string"},
            "soft_due": {"type": "string", "description": "יעד רך YYYY-MM-DD"},
            "linked_event": {"type": "string", "description": (
                "כותרת האירוע שהמשימה מכינה אליו, אם זו משימת הכנה")},
            "linked_date": {"type": "string", "description": (
                "YYYY-MM-DD של אותו אירוע. המשימה תעלה בבריף של אותו בוקר")},
        }, "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "מחזיר את המשימות הפתוחות.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "מסמן משימה כבוצעה. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "title": {"type": "string", "description": "לתצוגה באישור"},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "check_email",
        "description": ("מיילים שלא נקראו מהיממה האחרונה. לשאלות כמו "
                        "'יש לי מיילים חדשים?' או 'מה מחכה לי בתיבה?'"),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "workout_stats",
        "description": ("סטטיסטיקת אימונים: כמה אימונים ומתי היה האחרון. "
                        "לשאלות על כושר, אימונים, והתקדמות."),
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "כמה ימים אחורה. ברירת מחדל 7"},
        }},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": ("חיפוש באינטרנט למידע עדכני — שעות פתיחה, חדשות, "
                        "מחירים, עובדות שאתה לא בטוח בהן. החזר לרון תשובה "
                        "עם המקור."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "שאילתת חיפוש קצרה"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": ("חיפוש במסמכים שרון שמר אצלך — חוזים, מסמכים, "
                        "הערות. לשאלות על 'המסמך ש...' או פרטים אישיים "
                        "ששמורים בבסיס הידע."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "schedule_prompt",
        "description": ("תזמון הוראה קבועה — 'כל יום ראשון ב-8 תשלחי לי X'. "
                        "ניקול תריץ את ההוראה בזמן שנקבע ותשלח את התוצאה."),
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "מה להריץ, כלשונו"},
            "day_of_week": {"type": "string",
                            "description": ("יום בשבוע באנגלית: sun/mon/tue/wed/"
                                            "thu/fri/sat, או * לכל יום")},
            "hour": {"type": "integer"}, "minute": {"type": "integer"},
        }, "required": ["prompt", "day_of_week", "hour", "minute"]},
    }},
    {"type": "function", "function": {
        "name": "cancel_scheduled",
        "description": "ביטול הוראה מתוזמנת לפי המזהה שלה (מ-list_scheduled).",
        "parameters": {"type": "object", "properties": {
            "schedule_id": {"type": "integer"},
        }, "required": ["schedule_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_scheduled",
        "description": "רשימת ההוראות המתוזמנות הפעילות.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "add_habit",
        "description": "יצירת הרגל למעקב יומי ('תעקבי אחרי אימון').",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "שם ההרגל, קצר"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "remove_habit",
        "description": "הפסקת מעקב אחרי הרגל.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "log_habit",
        "description": "סימון הרגל כבוצע היום ('התאמנתי', 'קראתי'). מבוצע מיד.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "habits_status",
        "description": "מצב ההרגלים: מה סומן היום ומה הרצף של כל אחד.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "log_meal",
        "description": ("רישום ארוחה ('אכלתי X'). אתה מעריך קלוריות וחלבון "
                        "בעצמך לפי התיאור. מבוצע מיד."),
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string", "description": "מה נאכל"},
            "kcal": {"type": "integer", "description": "הערכת קלוריות"},
            "protein_g": {"type": "integer", "description": "הערכת חלבון בגרם"},
        }, "required": ["description", "kcal", "protein_g"]},
    }},
    {"type": "function", "function": {
        "name": "nutrition_today",
        "description": "סיכום התזונה של היום (או תאריך אחר).",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD, ריק=היום"},
        }},
    }},
    {"type": "function", "function": {
        "name": "portfolio",
        "description": "תיק ההשקעות ב-Interactive Brokers: שווי, שינוי, פוזיציות.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "save_note",
        "description": ("שמירת מידע לפתק נושא בזיכרון ארוך הטווח "
                        "('תרשמי לך שהרכב בטסט בנובמבר')."),
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "נושא הפתק, מילה-שתיים"},
            "content": {"type": "string"},
        }, "required": ["topic", "content"]},
    }},
    {"type": "function", "function": {
        "name": "read_notes",
        "description": ("קריאת פתק נושא, או רשימת כל הפתקים אם topic ריק. "
                        "לשאלות 'מה רשום לך על X'."),
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "read_journal",
        "description": "היומן האישי מהימים האחרונים — מה קרה, מה הספקת.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "כמה ימים אחורה (ברירת מחדל 7)"},
        }},
    }},
    {"type": "function", "function": {
        "name": "shopping",
        "description": ("רשימת הקניות. action: add/remove/show/clear. "
                        "'תוסיפי חלב' -> add. מבוצע מיד."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "show", "clear"]},
            "item": {"type": "string", "description": "הפריט (ל-add/remove)"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "שליחת מייל חדש. דורש אישור לפני שליחה.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "כתובת מייל"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "גוף המייל, בשפת המקור של הנמען"},
        }, "required": ["to", "subject", "body"]},
    }},
    {"type": "function", "function": {
        "name": "reply_email",
        "description": ("תשובה למייל נכנס לפי id מ-check_email. "
                        "דורש אישור לפני שליחה."),
        "parameters": {"type": "object", "properties": {
            "email_id": {"type": "string", "description": "ה-id מתוצאות check_email"},
            "body": {"type": "string"},
        }, "required": ["email_id", "body"]},
    }},
    {"type": "function", "function": {
        "name": "remember_preference",
        "description": ("שמירת העדפה קבועה של רון. כשהוא אומר 'תמיד', "
                        "'מעכשיו', 'כל פעם' — למשל 'אימון זה תמיד שעה וחצי'. "
                        "מתבצע מיד, בלי אישור."),
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "נושא קצר, למשל 'משך אימון'"},
            "value": {"type": "string", "description": "ההעדפה, למשל 'שעה וחצי'"},
        }, "required": ["key", "value"]},
    }},
    {"type": "function", "function": {
        "name": "forget_preference",
        "description": "מחיקת העדפה קבועה לפי הנושא שלה.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"},
        }, "required": ["key"]},
    }},
    {"type": "function", "function": {
        "name": "search_events",
        "description": ("חיפוש ביומן אחורה בזמן. למשל 'מתי נפגשתי עם דנה "
                        "לאחרונה'. מחזיר אירועים שתואמים לטקסט."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "מה לחפש (שם, מילה)"},
            "months_back": {"type": "integer", "description": "כמה חודשים אחורה (ברירת מחדל 6)"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "meeting_stats",
        "description": ("סטטיסטיקה על זמן בפגישות בטווח תאריכים — "
                        "כמה פגישות וכמה שעות."),
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "תחילת טווח ISO"},
            "end": {"type": "string", "description": "סוף טווח ISO"},
        }, "required": ["start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "restore_last",
        "description": ("מחזיר את האירוע האחרון שבוטל או נמחק, עם כל הפרטים "
                        "המקוריים. השתמש כשהמשתמש מבקש להחזיר/לשחזר אירוע."),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "log_activity",
        "description": "מתעד מה המשתמש עשה בפועל. לא דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string"},
        }, "required": ["content"]},
    }},
]

# כלים מ-features.py (הקלטות, דרייב, הוצאות, חלונות פנויים, קישורים...)
from app import features as _features  # noqa: E402
TOOLS += _features.TOOLS
READ_TOOLS |= _features.READ
WRITE_TOOLS |= _features.WRITE

# מוזרק מבחוץ כדי שהמודול יישאר נבדק בלי גוגל
_read_executors: dict = {}


def set_read_executor(name: str, fn) -> None:
    _read_executors[name] = fn


def _system_prompt(chat_id: int) -> str:
    now = datetime.now(TZ)
    prefs = db.get_preferences(chat_id)
    prefs_block = ""
    if prefs:
        lines = "\n".join(f"- {k}: {v}" for k, v in prefs.items())
        prefs_block = f"\n\n## העדפות קבועות של רון — כבד אותן בלי לשאול שוב\n{lines}"
    base = prompts.SYSTEM_PROMPT.format(
        now=now.strftime("%Y-%m-%d %H:%M"),
        weekday=WEEKDAYS[now.weekday()],
    )
    try:
        learned = _features.profile_block()
    except Exception:
        learned = ""
    return base + prefs_block + learned


def _occurrences(args: dict) -> int:
    """
    כמה מפגשים אירוע מייצג: 1 לחד-פעמי, COUNT לחוזר. חוזר בלי COUNT/UNTIL
    נחשב כמכסה את כל הרשימה (999), כדי שבדיקת "חסרים אירועים" לא תדחוף
    את המודל לפרק אותו לאירועים בודדים.
    """
    rec = str(args.get("recurrence") or "")
    rule = str(args.get("rrule") or "")
    if not rec and not rule:
        return 1
    m = re.search(r"COUNT=(\d+)", rule)
    if m:
        return int(m.group(1))
    parts = rec.split(",")
    if len(parts) > 1 and parts[1].strip().isdigit():
        return int(parts[1].strip())
    return 999


def _summarize(name: str, args: dict) -> str:
    """הטקסט שמוצג לך בהודעת האישור."""
    if name == "create_event":
        start = _local(args["start"])
        when = start.strftime("%d/%m בשעה %H:%M")
        where = ""
        if args.get("virtual"):
            where = " (טלפוני)"
        if args.get("rrule"):
            where += " (חוזר)"
        if args.get("category") and not args.get("virtual"):
            where += f" · {args['category']}"
        elif args.get("location"):
            where = f" — {args['location']}"
        repeat = ""
        if args.get("recurrence"):
            names = {"DAILY": "כל יום", "WEEKLY": "כל שבוע",
                     "MONTHLY": "כל חודש", "YEARLY": "כל שנה"}
            freq = str(args["recurrence"]).split(",")[0].upper()
            repeat = f" ({names.get(freq, 'חוזר')}"
            n = _occurrences(args)
            repeat += f", {n} מפגשים)" if n > 1 else ")"
        return f"לקבוע \"{args['title']}\" ב-{when}{where}{repeat}?"

    if name == "update_event":
        return f"לעדכן את \"{args.get('title', 'האירוע')}\"?"
    if name == "delete_event":
        return f"למחוק את \"{args.get('title', 'האירוע')}\"?"
    if name == "add_task":
        due = f" (עד {args['soft_due']})" if args.get("soft_due") else ""
        return f"להוסיף משימה: \"{args['title']}\"{due}?"
    if name == "complete_task":
        return f"לסמן כבוצע: \"{args.get('title', 'המשימה')}\"?"
    if name == "schedule_prompt":
        day = args.get("day_of_week", "*")
        day_he = {"sun": "יום ראשון", "mon": "יום שני", "tue": "יום שלישי",
                  "wed": "יום רביעי", "thu": "יום חמישי", "fri": "יום שישי",
                  "sat": "שבת", "*": "כל יום"}.get(day, day)
        return (f'לתזמן — {day_he} ב-{args.get("hour", 0):02d}:'
                f'{args.get("minute", 0):02d}: "{args.get("prompt", "")}"?')
    if name == "cancel_scheduled":
        return f'לבטל את ההוראה המתוזמנת #{args.get("schedule_id")}?'
    if name == "log_activity":
        return f"לתעד: \"{args['content']}\"?"
    if name == "sheet_append":
        vals = args.get("values") or []
        vals = " | ".join(map(str, vals)) if isinstance(vals, list) else str(vals)
        return f"להוסיף לגיליון '{args.get('spreadsheet')}': {vals}?"
    return "לבצע את הפעולה?"


def _local(value) -> datetime:
    """שעון קיר בישראל. ההיסט שהמודל כותב (לפעמים +03:00 בחורף) נזרק."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=TZ)


async def _conflicts_for(args: dict) -> list:
    """בדיקת התנגשויות לפני שמציגים הצעה לאירוע חדש."""
    fetch = _read_executors.get("query_events")
    if fetch is None:
        return []
    try:
        start = _local(args["start"])
        end = _local(args["end"])
        day_start = start.astimezone(TZ).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        events = await fetch(day_start, day_start + timedelta(days=1))
        return await travel.check_conflicts(
            start, end, args.get("location", ""), events,
            new_virtual=bool(args.get("virtual")),
        )
    except Exception as exc:
        logger.warning("בדיקת התנגשויות נכשלה: %s", exc)
        return []


async def _departure_line(args: dict) -> str:
    """
    שורת "צא ב-" שנוספת להצעה. עונה על המקרה של הפגישה הראשונה ביום,
    שבו אין אירוע קודם ולכן בדיקת ההתנגשויות שותקת — אבל עדיין צריך
    לצאת מהבית בזמן.
    """
    fetch = _read_executors.get("query_events")
    if fetch is None:
        return ""
    try:
        start = _local(args["start"])
        day_start = start.astimezone(TZ).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        events = await fetch(day_start, day_start + timedelta(days=1))
        leave_at, origin, minutes = await travel.departure_hint(
            start, args.get("location", ""), events,
            new_virtual=bool(args.get("virtual")),
        )
        if leave_at is None:
            return ""
        return (f"\n\nצא ב-{leave_at.astimezone(TZ):%H:%M} "
                f"({minutes} דק' נסיעה + {travel.BUFFER_MINUTES} חיץ)")
    except Exception as exc:
        logger.warning("חישוב שעת יציאה נכשל: %s", exc)
        return ""


def _dedupe(proposals: list[dict]) -> list[dict]:
    """סבב השלמה עלול להציע שוב אירוע שכבר נוסף — אותה כותרת ואותה התחלה."""
    seen, out = set(), []
    for p in proposals:
        a = p.get("payload") or {}
        key = (p["action_type"], a.get("title"), str(a.get("start", ""))[:16])
        if p["action_type"] == "create_event" and key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


_GROQ_TOOLS: Optional[list] = None


def _groq_tools() -> list:
    """
    Groq מאמת ארגומנטים לפי הסכמה בקפדנות, ו-gpt-oss שולח לפעמים null
    לפרמטר רשות ("focus": null) — מה שנדחה. לכן ל-Groq בלבד: כל פרמטר
    רשות מקבל גם null. (Gemini לא אוהב type כרשימה — הוא מקבל את המקור.)
    """
    global _GROQ_TOOLS
    if _GROQ_TOOLS is None:
        import copy
        tools = copy.deepcopy(TOOLS)
        for t in tools:
            params = t["function"].get("parameters") or {}
            required = set(params.get("required") or [])
            for name, prop in (params.get("properties") or {}).items():
                if name in required or "type" not in prop:
                    continue
                if isinstance(prop["type"], str) and "enum" not in prop:
                    prop["type"] = [prop["type"], "null"]
                elif "enum" in prop:
                    prop["enum"] = list(prop["enum"]) + [None]
                    prop["type"] = [prop["type"], "null"]
        _GROQ_TOOLS = tools
    return _GROQ_TOOLS


def _clean_assistant(choice) -> dict:
    """
    הודעת עוזר נקייה להיסטוריה. תשובה מ-Gemini מגיעה עם שדות נוספים
    (images, thinking_blocks...) ש-Groq דוחה בסבב הבא — אז שומרים רק
    role/content/tool_calls.
    """
    out = {"role": "assistant", "content": choice.get("content") or ""}
    calls = []
    for i, tc in enumerate(choice.get("tool_calls") or []):
        fn = tc["function"]
        calls.append({
            "id": tc.get("id") or f"call_{i}",
            "type": "function",
            "function": {"name": fn["name"], "arguments": fn.get("arguments") or "{}"},
        })
    if calls:
        out["tool_calls"] = calls
    return out


async def _call_llm(messages: list) -> Optional[dict]:
    """
    Groq קודם. בכל כישלון (מגבלת קצב, עומס, תקלה) — מיד ל-Gemini ב-Vertex,
    בלי להמתין: עדיף תשובה ממודל אחר מאשר "עומס" בפני המשתמש.
    """
    common = dict(messages=messages, tool_choice="auto", max_tokens=4096)
    for attempt in (1, 2):
        try:
            return await acompletion(model=MODEL, tools=_groq_tools(), **common)
        except Exception as exc:
            msg = str(exc)
            logger.warning("המודל הראשי נכשל (%s): %s", type(exc).__name__,
                           msg[:200])
            # המודל "הזה" שם כלי לא קיים / ארגומנט לא תקין — לרוב חולף בניסיון שני
            if attempt == 1 and "Tool call validation failed" in msg:
                continue
            break

    fallback = _fallback_kwargs()
    if fallback is None:
        return None
    try:
        logger.info("עובר למודל גיבוי: %s", FALLBACK_MODEL)
        return await acompletion(**fallback, tools=TOOLS, **common)
    except Exception as exc:
        logger.error("גם מודל הגיבוי נכשל: %s", str(exc)[:300])
        return None


async def complete_text(prompt: str, max_tokens: int = 2000,
                        long: bool = False) -> Optional[str]:
    """
    טקסט חופשי בלי כלים (סיכומים, חילוץ). long=True: Gemini קודם —
    הקשר ארוך, ובלי תקרת 8K הטוקנים לדקה של Groq החינמי.
    """
    messages = [{"role": "user", "content": prompt}]
    fallback = _fallback_kwargs()
    primary = {"model": MODEL}
    order = ([fallback, primary] if long and fallback
             else [primary] + ([fallback] if fallback else []))
    for kw in order:
        try:
            resp = await acompletion(**kw, messages=messages,
                                     max_tokens=max(max_tokens, 1024))
            text = (resp["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text
        except Exception as exc:
            logger.warning("complete_text נכשל ב-%s: %s", kw.get("model"),
                           str(exc)[:200])
    return None


async def handle_message(chat_id: int, text: str) -> Optional[str]:
    """
    מחזיר טקסט לשליחה, או None כשכבר נשלחה הודעת אישור עם כפתורים.
    """
    db.add_message(chat_id, "user", text)

    history = db.get_history(chat_id, limit=6)
    messages = [{"role": "system", "content": _system_prompt(chat_id)}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]

    proposals: list[dict] = []   # נאספים במהלך התור, נשלחים כמקשה אחת בסוף

    # תמונה עם רשימה: vision כותב "סה"כ N אירועים". אם המודל עצר לפני N,
    # מחזירים אותו להשלים את החסרים — במקום לשלוח אישור חלקי בשקט.
    m = re.search(r'סה"כ\s*(\d+)', text)
    expected = int(m.group(1)) if m else 0
    nudges = 0

    for _ in range(MAX_ROUNDS):
        response = await _call_llm(messages)
        if response is None:
            return ("יש עומס רגעי על המודל. חכה חצי דקה ותנסה שוב — "
                    "שום דבר לא אבד.")

        choice = response["choices"][0]["message"]
        tool_calls = choice.get("tool_calls") or []

        if not tool_calls:
            reply = (choice.get("content") or "").strip()
            created = sum(_occurrences(p["payload"]) for p in proposals
                          if p["action_type"] == "create_event")
            if expected and created < expected and nudges < 2:
                nudges += 1
                logger.info("נוצרו %s מתוך %s אירועים — מבקש השלמה", created, expected)
                messages.append({"role": "assistant", "content": reply or ""})
                messages.append({"role": "user", "content": (
                    f"נוספו {created} מתוך {expected} האירועים שברשימה. עבור שוב "
                    "על הרשימה הממוספרת, מצא את האירועים שעוד לא נוספו, וקרא "
                    "ל-create_event רק עבורם. אל תוסיף שוב אירוע שכבר נוסף "
                    "(אירוע חוזר עם COUNT כבר מכסה את כל המפגשים שלו)."
                )})
                continue
            if proposals:
                await confirm.propose_batch(chat_id, _dedupe(proposals))
                # טקסט חופשי מהמודל מיותר לצד הודעת האישור
                return None
            if reply:
                db.add_message(chat_id, "assistant", reply)
                return reply
            return "לא הבנתי. תוכל לנסח אחרת?"

        messages.append(_clean_assistant(choice))

        for idx, call in enumerate(tool_calls):
            name = call["function"]["name"]
            call_id = call.get("id") or f"call_{idx}"   # זהה ל-_clean_assistant
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if isinstance(args, dict):
                # null בפרמטר רשות = "לא נמסר"
                args = {k: v for k, v in args.items() if v is not None}
            else:
                args = {}

            # --- שחזור: שולפים את הפעולה הקודמת ומציעים אותה שוב ---
            if name == RESTORE_TOOL:
                previous = db.get_last_action(chat_id, ("create_event",))
                if previous is None:
                    return "אין אירוע קודם שאני יכול להחזיר."
                args = previous["payload"]
                conflicts = await _conflicts_for(args)
                await confirm.propose(
                    chat_id=chat_id, action_type="create_event", payload=args,
                    summary="להחזיר: " + _summarize("create_event", args),
                    conflicts=conflicts,
                )
                return None

            # --- כלי כתיבה: מציעים, ו*ממשיכים* —
            # חיוני כשהמודל שולח פריטים בזה אחר זה ולא במקביל.
            # בלי ההמשך, "תקבע X וגם Y" היה נעצר אחרי X.
            if name in WRITE_TOOLS:
                conflicts = []
                summary = _summarize(name, args)
                if name == "create_event":
                    conflicts = await _conflicts_for(args)
                    summary += await _departure_line(args)
                proposals.append({
                    "action_type": name, "payload": args,
                    "summary": summary, "conflicts": conflicts,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": ("הפריט נוסף לרשימת האישור. אל תשאל שוב "
                                "עליו. אם נשארו פריטים נוספים בבקשה — "
                                "טפל בהם עכשיו. אם לא — סיים בלי טקסט."),
                })
                continue

            # --- כלי קריאה: מבצעים ומחזירים למודל ---
            executor = _read_executors.get(name)
            if executor is None:
                result = "הכלי אינו זמין."
            else:
                try:
                    result = await executor(**args) if args else await executor()
                except Exception as exc:
                    logger.error("כלי %s נכשל: %s", name, exc)
                    result = "הפעולה נכשלה."

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    if proposals:
        await confirm.propose_batch(chat_id, _dedupe(proposals))
        return None
    return "הסתבכתי עם הבקשה הזו. תוכל לפרק אותה לשני חלקים?"
