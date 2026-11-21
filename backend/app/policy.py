"""
מדיניות: מי מורשה, מתי מותר לדבר, והאם הסוכן בכלל חי.

שלושת אלה לא פיצ'רים — הם התנאים לכך שהסוכן יישאר דלוק:
  allowlist   — בלי זה כל אחד שימצא את הבוט מגיע ליומן שלך
  quiet hours — בריף בוקר בשבת ב-7:30 הוא הסיבה שתשתיק אותו
  heartbeat   — נדנוד שקט כשאין משימות נראה בדיוק כמו מתזמן מת
"""

import logging
import os
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")

# ------------------------------------------------------------ הרשאות

def _parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


ALLOWED_CHAT_IDS = _parse_ids(os.getenv("ALLOWED_CHAT_IDS", ""))


def is_allowed(chat_id: int) -> bool:
    """
    ברירת המחדל היא סגירה: רשימה ריקה חוסמת את כולם.
    עדיף בוט שלא עונה לך עד שתגדיר אותו, מאשר בוט שעונה לכולם.
    """
    if not ALLOWED_CHAT_IDS:
        logger.error("ALLOWED_CHAT_IDS לא הוגדר — חוסם הכל")
        return False
    return chat_id in ALLOWED_CHAT_IDS


# ------------------------------------------------------- שעות שקט

QUIET_START = time(int(os.getenv("QUIET_START_HOUR", 23)), 0)
QUIET_END = time(int(os.getenv("QUIET_END_HOUR", 7)), 0)

# קרית אתא. מזהה GeoNames — משנים דרך משתנה סביבה במעבר דירה.
GEONAME_ID = os.getenv("HEBCAL_GEONAMEID", "295721")
HEBCAL_ZMANIM = "https://www.hebcal.com/zmanim"

_melacha_cache: dict[str, tuple[bool, datetime]] = {}
_MELACHA_TTL = timedelta(minutes=30)


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    t = now.time()
    if QUIET_START < QUIET_END:
        return QUIET_START <= t < QUIET_END
    return t >= QUIET_START or t < QUIET_END   # חוצה חצות


async def is_assur_melacha(now: Optional[datetime] = None) -> bool:
    """
    שבת או חג, לפי Hebcal. ממוטמח ל-30 דקות.
    כישלון רשת מחזיר False — עדיף הודעה מיותרת מאשר סוכן אילם.
    """
    now = (now or datetime.now(TZ)).astimezone(TZ)
    key = now.strftime("%Y-%m-%dT%H")

    cached = _melacha_cache.get(key)
    if cached and datetime.now(TZ) - cached[1] < _MELACHA_TTL:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(HEBCAL_ZMANIM, params={
                "cfg": "json",
                "im": "1",
                "geonameid": GEONAME_ID,
                "dt": now.isoformat(),
            })
            resp.raise_for_status()
            data = resp.json()

        result = bool(data.get("isAssurBemlacha", data.get("assurBemlacha", False)))
        _melacha_cache[key] = (result, datetime.now(TZ))
        return result

    except Exception as exc:
        logger.warning("בדיקת שבת/חג נכשלה: %s", exc)
        return False


async def may_send(now: Optional[datetime] = None,
                   urgent: bool = False) -> tuple[bool, str]:
    """
    האם מותר לשלוח הודעה יזומה כרגע.
    urgent=True עוקף את שעות השקט אך לא את שבת.
    מחזיר (מותר, סיבה).
    """
    now = (now or datetime.now(TZ)).astimezone(TZ)

    if await is_assur_melacha(now):
        return False, "שבת או חג"

    if in_quiet_hours(now) and not urgent:
        return False, "שעות שקט"

    return True, ""


def next_workday_note(now: Optional[datetime] = None) -> Optional[str]:
    """הערה לסיכום הערב כשמחר לא יום עבודה רגיל."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    tomorrow = now + timedelta(days=1)
    weekday = tomorrow.weekday()          # 4=שישי, 5=שבת
    if weekday == 4:
        return "מחר יום שישי — יום קצר."
    if weekday == 5:
        return "מחר שבת."
    return None


def hebrew_date(now: Optional[datetime] = None) -> str:
    """
    התאריך העברי כמחרוזת ("ח׳ אלול תשפ״ו"), או ריק בכישלון.
    pyluach מחשב לוקלית — בלי תלות ברשת בחמש בבוקר.
    """
    now = (now or datetime.now(TZ)).astimezone(TZ)
    try:
        from pyluach import dates
        return dates.HebrewDate.from_pydate(now.date()).hebrew_date_string()
    except Exception as exc:
        logger.warning("תאריך עברי נכשל: %s", exc)
        return ""


# --------------------------------------------------------- דופק

HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL")


async def ping_healthcheck(job_name: str = "") -> None:
    """
    נקרא בסוף כל ג'וב מתוזמן. אם ההרצות מפסיקות להגיע,
    השירות החיצוני מתריע — במקום שתגלה לבד שהמתזמן מת.
    """
    if not HEALTHCHECK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(HEALTHCHECK_URL)
        logger.debug("דופק נשלח (%s)", job_name)
    except Exception as exc:
        logger.warning("שליחת דופק נכשלה: %s", exc)
