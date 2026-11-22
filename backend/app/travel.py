"""
חישוב זמני נסיעה ובדיקת התנגשויות.

ספק: OpenRouteService (מדרגה חינמית). מפתח ב-ORS_API_KEY.
ORS לא מביא פקקים בזמן אמת — הוא מחזיר זמן לפי מהירויות טיפוסיות.
זה מספיק כאן: אנחנו מחשבים חיץ לפגישה שתיקבע בעתיד, שבו הפקקים
ממילא לא ידועים. החיץ הקבוע של 15 דקות סופג את ההפרש.

עקרון מנחה: אם משהו נכשל — גיאוקודינג, רשת, מפתח — לא חוסמים את
המשתמש. מחזירים "לא ידוע" והסוכן פשוט לא מזכיר נסיעה.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app import db

logger = logging.getLogger(__name__)

ORS_API_KEY = os.getenv("ORS_API_KEY")
ORS_BASE = "https://api.openrouteservice.org"

# מיקום ברירת מחדל כשלאירוע הקודם אין מיקום רשום
HOME_ADDRESS = os.getenv("HOME_ADDRESS", "גיבורי הקריה 9, קרית אתא")

# עיגון ידני של הבית: "lat,lon" מגוגל מפות. עוקף את הגיאוקודר לגמרי
# עבור הבית — הכתובת הכי חשובה והכי רגישה לפענוח שגוי.
_home_env = os.getenv("HOME_COORDS", "")
HOME_COORDS = None
if _home_env:
    try:
        lat, lon = (float(x) for x in _home_env.split(","))
        HOME_COORDS = (lon, lat)          # ORS עובד ב-(lon, lat)
    except ValueError:
        logger.error("HOME_COORDS לא תקין: %r — מצפה ל'lat,lon'", _home_env)

# חיץ מעבר לזמן הנסיעה עצמו: חניה, כניסה, לא להגיע מזיע
BUFFER_MINUTES = 15

REQUEST_TIMEOUT = 10.0

# סימנים שהאירוע וירטואלי — אין אליו נסיעה, ואפשר לעשות אותו מהרכב
VIRTUAL_HINTS = (
    "meet.google", "zoom.us", "teams.microsoft", "whereby", "webex",
    "טלפוני", "טלפונית", "בטלפון", "שיחת טלפון", "זום", "מקוון",
    "online", "virtual", "phone call", "call with",
)


# מילים שפירושן "הבית" — אירוע ביתי לא מצריך נסיעה מהבית
HOME_WORDS = {"בית", "בבית", "הבית", "בית שלי", "אצלי", "אצלי בבית", "home"}


def is_home(location: Optional[str]) -> bool:
    """האם המיקום הוא הבית — לפי מילת מפתח או הכתובת עצמה."""
    if not location:
        return False
    text = location.strip().rstrip(".")
    return (text in HOME_WORDS
            or text == HOME_ADDRESS.strip()
            or text.replace(" ,", ",") == HOME_ADDRESS.strip())


def classify(event: dict) -> str:
    """
    'virtual' | 'physical' | 'unknown'

    פגישה טלפונית היא לא מיקום ריק — היא סוג אחר של אירוע.
    ההבדל קריטי: מיקום ריק גורר נפילה לכתובת הבית ולחישוב נסיעה
    מדומיינת, ואילו אירוע וירטואלי לא דורש נסיעה בכלל.
    """
    if event.get("virtual") is True:
        return "virtual"

    haystack = " ".join(filter(None, [
        event.get("location", ""),
        event.get("summary", ""),
        event.get("description", ""),
    ])).lower()

    if any(hint in haystack for hint in VIRTUAL_HINTS):
        return "virtual"

    return "physical" if (event.get("location") or "").strip() else "unknown"


def physical_location(event: dict) -> Optional[str]:
    """המיקום שממנו/אליו באמת נוסעים. None כשאין נסיעה."""
    kind = classify(event)
    if kind == "virtual":
        return None
    loc = (event.get("location") or "").strip()
    return loc or HOME_ADDRESS


@dataclass
class TravelEstimate:
    minutes: Optional[int]          # None = לא הצלחנו לחשב
    from_address: str
    to_address: str
    cached: bool = False

    @property
    def known(self) -> bool:
        return self.minutes is not None

    @property
    def total_needed(self) -> Optional[int]:
        """זמן נסיעה + חיץ."""
        return None if self.minutes is None else self.minutes + BUFFER_MINUTES


@dataclass
class Conflict:
    kind: str                       # overlap | travel
    message: str                    # ניסוח בעברית להצגה באישור
    other_event: str
    minutes_short: Optional[int] = None


async def geocode(address: str) -> Optional[tuple[float, float]]:
    """כתובת -> (lon, lat). קודם מהמטמון."""
    if not address or not address.strip():
        return None

    # הבית — על כל צורותיו ("בבית", "אצלי", הכתובת המלאה) — מעוגן
    # ידנית; לא שואלים את הגיאוקודר עליו בכלל. בלי זה, "בבית" נשלח
    # לגיאוקודר ופוענח למקום אקראי, וזה מייצר זמני נסיעה הזויים
    # לאירועים ביתיים.
    if is_home(address):
        if HOME_COORDS:
            return HOME_COORDS
        address = HOME_ADDRESS

    cached = db.get_geocode(address)
    if cached:
        return cached

    if not ORS_API_KEY:
        logger.warning("ORS_API_KEY חסר — מדלג על גיאוקודינג")
        return None

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{ORS_BASE}/geocode/search",
                params={
                    "api_key": ORS_API_KEY,
                    "text": address,
                    "boundary.country": "IL",
                    "size": 1,
                    # הטיה לתוצאות קרובות לבית — "חיפה" צריך להיות
                    # חיפה שליד קרית אתא, לא עיר אחרת עם שם דומה
                    **({"focus.point.lon": str(HOME_COORDS[0]),
                        "focus.point.lat": str(HOME_COORDS[1])}
                       if HOME_COORDS else {}),
                },
            )
            resp.raise_for_status()
            features = resp.json().get("features", [])

        if not features:
            logger.info("לא נמצאה כתובת: %s", address)
            return None

        lon, lat = features[0]["geometry"]["coordinates"]
        label = features[0].get("properties", {}).get("label")
        db.set_geocode(address, lon, lat, label)
        return (lon, lat)

    except Exception as exc:
        logger.warning("גיאוקודינג נכשל עבור %r: %s", address, exc)
        return None


async def travel_minutes(origin: str, destination: str) -> TravelEstimate:
    # שני הצדדים בבית — אין נסיעה. נבדק לפני מטמון ולפני רשת.
    if is_home(origin) and is_home(destination):
        return TravelEstimate(minutes=0, from_address=origin,
                              to_address=destination, cached=True)
    """זמן נסיעה ברכב בדקות. מחזיר minutes=None כשלא ניתן לחשב."""
    if not origin or not destination:
        return TravelEstimate(None, origin or "", destination or "")

    if origin.strip().lower() == destination.strip().lower():
        return TravelEstimate(0, origin, destination, cached=True)

    cached = db.get_travel_minutes(origin, destination)
    if cached is not None:
        return TravelEstimate(cached, origin, destination, cached=True)

    src = await geocode(origin)
    dst = await geocode(destination)
    if not src or not dst:
        return TravelEstimate(None, origin, destination)

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{ORS_BASE}/v2/directions/driving-car",
                headers={"Authorization": ORS_API_KEY},
                json={"coordinates": [list(src), list(dst)]},
            )
            resp.raise_for_status()
            data = resp.json()

        seconds = data["routes"][0]["summary"]["duration"]
        minutes = max(1, round(seconds / 60))
        db.set_travel_minutes(origin, destination, minutes)
        return TravelEstimate(minutes, origin, destination)

    except Exception as exc:
        logger.warning("חישוב מסלול נכשל %r -> %r: %s", origin, destination, exc)
        return TravelEstimate(None, origin, destination)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%H:%M")


async def check_conflicts(
    new_start: datetime,
    new_end: datetime,
    new_location: Optional[str],
    same_day_events: list[dict],
    new_virtual: bool = False,
) -> list[Conflict]:
    """
    same_day_events: [{'summary','start','end','location'}] — start/end כ-datetime.

    שתי בדיקות:
      1. חפיפה ישירה בזמן — חלה גם על אירועים וירטואליים
      2. זמן הגעה — רק בין אירועים פיזיים

    אירועים וירטואליים מדולגים לגמרי בחישוב הנסיעה, ולכן פגישה טלפונית
    בין שתי פגישות פיזיות לא שוברת את שרשרת הנסיעה — היא פשוט שקופה לה.
    """
    conflicts: list[Conflict] = []
    ordered = sorted(same_day_events, key=lambda e: e["start"])

    # --- חפיפה ישירה, על כל האירועים ---
    for ev in ordered:
        if new_start < ev["end"] and ev["start"] < new_end:
            conflicts.append(Conflict(
                kind="overlap",
                other_event=ev["summary"],
                message=f'חופף ל"{ev["summary"]}" ({_fmt(ev["start"])}–{_fmt(ev["end"])})',
            ))

    new_kind = "virtual" if new_virtual else classify({
        "location": new_location or "", "summary": "",
    })

    # אירוע וירטואלי לא דורש נסיעה — אפשר לעשות אותו גם מהרכב
    if new_kind == "virtual":
        return conflicts

    dest = (new_location or "").strip()
    if not dest:
        return conflicts

    physical = [e for e in ordered if classify(e) != "virtual"]

    # --- האירוע הפיזי האחרון שלפני ---
    before = [e for e in physical if e["end"] <= new_start]
    if before:
        prev = before[-1]
        est = await travel_minutes(physical_location(prev), dest)
        if est.known:
            gap = int((new_start - prev["end"]).total_seconds() // 60)
            if gap < est.total_needed:
                conflicts.append(Conflict(
                    kind="travel",
                    other_event=prev["summary"],
                    minutes_short=est.total_needed - gap,
                    message=(
                        f'אחרי "{prev["summary"]}" שנגמר ב-{_fmt(prev["end"])} '
                        f"נשארות {gap} דקות, וצריך {est.total_needed} "
                        f"({est.minutes} נסיעה + {BUFFER_MINUTES} חיץ)"
                    ),
                ))

    # --- האירוע הפיזי הראשון שאחרי ---
    after = [e for e in physical if e["start"] >= new_end]
    if after:
        nxt = after[0]
        est = await travel_minutes(dest, physical_location(nxt))
        if est.known:
            gap = int((nxt["start"] - new_end).total_seconds() // 60)
            if gap < est.total_needed:
                conflicts.append(Conflict(
                    kind="travel",
                    other_event=nxt["summary"],
                    minutes_short=est.total_needed - gap,
                    message=(
                        f'לא תספיק ל"{nxt["summary"]}" ב-{_fmt(nxt["start"])} — '
                        f"יש {gap} דקות וצריך {est.total_needed}"
                    ),
                ))

    return conflicts


def suggest_shift(conflicts: list[Conflict]) -> Optional[int]:
    """בכמה דקות להזיז את האירוע כדי לפתור את כל התנגשויות הנסיעה."""
    shortfalls = [c.minutes_short for c in conflicts
                  if c.kind == "travel" and c.minutes_short]
    return max(shortfalls) if shortfalls else None


def waze_link(address: str) -> str:
    """קישור ניווט חינמי — נכנס להודעת התזכורת."""
    from urllib.parse import quote
    return f"https://waze.com/ul?q={quote(address)}&navigate=yes"


async def departure_hint(new_start, new_location: Optional[str],
                        same_day_events: list[dict], new_virtual: bool = False):
    """
    מתי לצאת כדי להגיע בזמן, גם כשזו הפגישה הראשונה ביום.
    בדיקת ההתנגשויות משווה בין אירועים; זה משלים אותה במקרה שאין
    אירוע קודם בכלל — ואז נקודת המוצא היא הבית.

    מחזיר (שעת_יציאה, מוצא, דקות_נסיעה) או (None, None, None).
    """
    if new_virtual or not new_location or not new_location.strip():
        return None, None, None

    # אירוע בבית: אתה כבר שם. אין "צא ב-".
    if is_home(new_location):
        return None, None, None

    ordered = sorted(same_day_events, key=lambda e: e["start"])
    physical = [e for e in ordered
                if classify(e) != "virtual" and e["end"] <= new_start]

    origin = physical_location(physical[-1]) if physical else HOME_ADDRESS

    est = await travel_minutes(origin, new_location.strip())
    if not est.known:
        return None, None, None

    from datetime import timedelta
    leave_at = new_start - timedelta(minutes=est.total_needed)
    return leave_at, origin, est.minutes
