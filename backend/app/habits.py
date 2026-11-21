"""
מעקב הרגלים — רצפים (streaks) וניקוד יומי, בהשראת מסך ה-Habits של Miles OS,
אבל בסגנון ניקול: מסמנים בצ'אט ("התאמנתי"), והרצף מופיע בבריף.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import db

TZ = ZoneInfo("Asia/Jerusalem")


def _streak(dates: list[str], today: date) -> int:
    """רצף ימים רצוף שמסתיים היום או אתמול (יום חסר אחד לא שובר עדיין)."""
    done = {date.fromisoformat(d) for d in dates}
    if not done:
        return 0
    day = today if today in done else today - timedelta(days=1)
    if day not in done:
        return 0
    streak = 0
    while day in done:
        streak += 1
        day -= timedelta(days=1)
    return streak


def status(chat_id: int, on: date = None) -> list[dict]:
    """לכל הרגל: האם סומן היום, ומה הרצף."""
    today = on or datetime.now(TZ).date()
    out = []
    for h in db.list_habits(chat_id):
        dates = db.habit_dates(h["id"])
        out.append({
            "name": h["name"],
            "done_today": today.isoformat() in dates,
            "streak": _streak(dates, today),
        })
    return out


def mark(chat_id: int, name: str) -> str:
    """סימון הרגל כבוצע היום. מחזיר משפט תשובה."""
    h = db.find_habit(chat_id, name)
    if not h:
        known = ", ".join(x["name"] for x in db.list_habits(chat_id)) or "אין עדיין"
        return f"לא מצאתי הרגל בשם '{name}'. ההרגלים שלך: {known}."
    today = datetime.now(TZ).date()
    db.log_habit(h["id"], today.isoformat())
    s = _streak(db.habit_dates(h["id"]), today)
    fire = " 🔥" if s >= 3 else ""
    return f"סימנתי '{h['name']}'. רצף: {s} ימים{fire}."


def brief_line(chat_id: int) -> str:
    """שורת ההרגלים לבריף הבוקר. ריק אם אין הרגלים."""
    rows = status(chat_id)
    if not rows:
        return ""
    parts = []
    for r in rows:
        fire = f" 🔥{r['streak']}" if r["streak"] >= 2 else ""
        parts.append(f"{r['name']}{fire}")
    return "🔁 הרגלים להיום: " + " · ".join(parts)


def evening_lines(chat_id: int) -> list[str]:
    """תזכורת ערב על מה שטרם סומן + חיזוק על מה שכן."""
    rows = status(chat_id)
    if not rows:
        return []
    missing = [r for r in rows if not r["done_today"]]
    done = [r for r in rows if r["done_today"]]
    lines = []
    if done:
        parts = [f"{r['name']} ({r['streak']})" for r in done]
        lines.append("✅ סומנו היום: " + ", ".join(parts))
    if missing:
        at_risk = [r["name"] for r in missing if r["streak"] >= 3]
        names = ", ".join(r["name"] for r in missing)
        lines.append(f"🔁 עוד לא סומנו: {names}.")
        if at_risk:
            lines.append(f"   שים לב — רצף של {', '.join(at_risk)} על הכף!")
    return lines
