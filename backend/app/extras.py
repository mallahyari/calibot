"""
שלושה ג'ובים פרואקטיביים שנוספו בנובמבר 2026:

  weekly_summary      שישי בצהריים — השבוע במבט אחד: יומן, משימות,
                      הרגלים, תזונה ותיק.
  portfolio_alerts    אחרי רענון IBKR — רק כשיש תזוזה חריגה.
  event_reminders     כל 5 דקות — תזכורת לפני אירוע. לאירוע עם כתובת:
                      לפני מועד היציאה, עם זמן נסיעה וקישור Waze.

הכל מוגדר מ-.env (ערכי ברירת מחדל בסוגריים):
  WEEKLY_DAY (fri) WEEKLY_HOUR (13) WEEKLY_MINUTE (0)
  ALERT_POSITION_PCT (5)  ALERT_NAV_PCT (2)  PORTFOLIO_FLOOR (ריק = כבוי)
  REMINDERS (1)  REMIND_MINUTES (15)
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import db, habits, ibkr, policy, travel

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Jerusalem")

WEEKLY_DAY = os.getenv("WEEKLY_DAY", "fri")
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", 13))
WEEKLY_MINUTE = int(os.getenv("WEEKLY_MINUTE", 0))

ALERT_POSITION_PCT = float(os.getenv("ALERT_POSITION_PCT", 5))
ALERT_NAV_PCT = float(os.getenv("ALERT_NAV_PCT", 2))
PORTFOLIO_FLOOR = float(os.getenv("PORTFOLIO_FLOOR") or 0)

REMINDERS_ON = os.getenv("REMINDERS", "1") != "0"
REMIND_MINUTES = int(os.getenv("REMIND_MINUTES", 15))

KCAL_GOAL = int(os.getenv("NUTRITION_KCAL_GOAL", 0) or 0)

HE_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

_deps = None   # מוזרק מ-scheduler.start_scheduler


def init(deps) -> None:
    global _deps
    _deps = deps


async def _send(text: str) -> None:
    if _deps is not None and text:
        await _deps.send(_deps.chat_id, text)


# ============================================================ סיכום שבועי

def _week_start(now: datetime) -> datetime:
    """תחילת השבוע הישראלי — יום ראשון 00:00."""
    days_since_sunday = (now.weekday() + 1) % 7
    return (now - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _is_all_day(e: dict) -> bool:
    start, end = e["start"], e["end"]
    return (start.hour == 0 and start.minute == 0
            and (end - start) >= timedelta(hours=23))


def _tasks_done_since(chat_id: int, since: datetime) -> list[str]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT title FROM tasks WHERE chat_id = ? AND status = 'done' "
            "AND completed_at >= ? ORDER BY completed_at",
            (chat_id, since.strftime("%Y-%m-%d"))).fetchall()
    return [r["title"] for r in rows]


def _habit_week_lines(chat_id: int, now: datetime) -> list[str]:
    days = {(now.date() - timedelta(days=i)).isoformat() for i in range(7)}
    lines = []
    for h in db.list_habits(chat_id):
        dates = db.habit_dates(h["id"], limit=30)
        done = len(days & set(dates))
        streak = habits._streak(dates, now.date())
        bar = "●" * done + "○" * (7 - done)
        fire = f" · רצף {streak} 🔥" if streak >= 3 else ""
        lines.append(f"  {h['name']}: {bar} {done}/7{fire}")
    return lines


def _nutrition_week_line(chat_id: int, now: datetime) -> str:
    totals = []
    for i in range(7):
        d = (now.date() - timedelta(days=i)).isoformat()
        meals = db.meals_for_date(chat_id, d)
        if meals:
            totals.append(sum(m["kcal"] or 0 for m in meals))
    if not totals:
        return ""
    avg = round(sum(totals) / len(totals))
    line = f"🍽 תזונה: ממוצע ~{avg:,} קק\"ל ביום ({len(totals)} ימים נרשמו)"
    if KCAL_GOAL:
        diff = avg - KCAL_GOAL
        line += f" · {'+' if diff >= 0 else ''}{diff:,} מהיעד"
    return line


def _portfolio_week_line(now: datetime) -> str:
    hist = db.portfolio_history(limit=30)
    if not hist:
        return ""
    latest = hist[0]
    cutoff = (now.date() - timedelta(days=7)).isoformat()
    older = next((h for h in hist[1:] if h["snap_date"] <= cutoff), None)
    line = f"📈 תיק: ${latest['nav']:,.0f}"
    if older and older["nav"]:
        chg = latest["nav"] - older["nav"]
        pct = chg / older["nav"] * 100
        arrow = "▲" if chg >= 0 else "▼"
        line += (f" · השבוע {arrow}{abs(pct):.1f}% "
                 f"({'+' if chg >= 0 else '-'}${abs(chg):,.0f})")
    return line


async def build_weekly_summary(now: Optional[datetime] = None) -> str:
    if _deps is None:
        return ""
    now = now or datetime.now(TZ)
    chat_id = _deps.chat_id
    week_start = _week_start(now)
    next_start = week_start + timedelta(days=7)

    lines = [f"🗓 סיכום שבועי · {week_start:%d/%m}–{now:%d/%m}"]

    try:
        past = [e for e in await _deps.fetch_events(week_start, now)
                if not _is_all_day(e)]
        upcoming = [e for e in await _deps.fetch_events(next_start,
                                                        next_start + timedelta(days=7))
                    if not _is_all_day(e)]
    except Exception as exc:
        logger.warning("סיכום שבועי: קריאת יומן נכשלה: %s", exc)
        past, upcoming = [], []

    if past:
        hours = sum((e["end"] - e["start"]).total_seconds() for e in past) / 3600
        lines.append(f"📅 השבוע: {len(past)} אירועים, {hours:.0f} שעות ביומן")
    if upcoming:
        by_day: dict[int, int] = {}
        for e in upcoming:
            wd = e["start"].astimezone(TZ).weekday()
            by_day[wd] = by_day.get(wd, 0) + 1
        busiest = max(by_day, key=by_day.get)
        lines.append(f"🔜 שבוע הבא: {len(upcoming)} אירועים · העמוס ביותר "
                     f"יום {HE_DAYS[busiest]} ({by_day[busiest]})")

    done = _tasks_done_since(chat_id, week_start)
    open_tasks = db.list_open_tasks(chat_id)
    if done or open_tasks:
        line = f"✅ משימות: {len(done)} הושלמו"
        if open_tasks:
            line += f", {len(open_tasks)} פתוחות"
        lines.append(line)

    h_lines = _habit_week_lines(chat_id, now)
    if h_lines:
        lines.append("🔁 הרגלים (7 ימים):")
        lines.extend(h_lines)

    food = _nutrition_week_line(chat_id, now)
    if food:
        lines.append(food)

    try:
        from app import features
        spent = features.expenses_week_line(chat_id, week_start)
        if spent:
            lines.append(spent)
    except Exception as exc:
        logger.warning("שורת הוצאות לסיכום נכשלה: %s", exc)

    port = _portfolio_week_line(now)
    if port:
        lines.append(port)

    if len(lines) == 1:
        lines.append("שבוע שקט — לא נרשם כלום. שבת שלום!")
    else:
        lines.append("\nשבת שלום 🕯")
    return "\n".join(lines)


async def weekly_summary() -> None:
    try:
        await _send(await build_weekly_summary())
    except Exception as exc:
        logger.error("סיכום שבועי נכשל: %s", exc)


# ============================================================ התראות תיק

def portfolio_alert_lines() -> list[str]:
    """משווה את הצילום האחרון לקודם. ריק כשאין משהו חריג."""
    s = ibkr.summary(max_positions=50)
    if not s:
        return []
    lines = []
    if s["change_pct"] is not None and abs(s["change_pct"]) >= ALERT_NAV_PCT:
        arrow = "▲" if s["change"] >= 0 else "▼"
        lines.append(f"התיק {arrow}{abs(s['change_pct'])}% "
                     f"({'+' if s['change'] >= 0 else '-'}${abs(s['change']):,.0f}) "
                     f"· שווי ${s['nav']:,.0f}")
    for p in s["positions"]:
        if p.get("chg") is not None and abs(p["chg"]) >= ALERT_POSITION_PCT:
            arrow = "▲" if p["chg"] >= 0 else "▼"
            lines.append(f"{p['symbol']} {arrow}{abs(p['chg'])}% · ${p['price']:,.2f}")
    if PORTFOLIO_FLOOR and s["nav"] < PORTFOLIO_FLOOR:
        lines.append(f"⚠️ שווי התיק ${s['nav']:,.0f} — מתחת לסף "
                     f"${PORTFOLIO_FLOOR:,.0f} שהגדרת")
    return lines


async def ibkr_refresh_and_alert() -> None:
    """מחליף את ג'וב הרענון: רענון, ואם יש תזוזה חריגה — הודעה."""
    data = await ibkr.refresh()
    if data is None:
        return
    lines = portfolio_alert_lines()
    if lines:
        await _send("📊 התראת תיק\n" + "\n".join(lines))


# ============================================================ תזכורות

_reminded: dict[str, datetime] = {}   # מפתח אירוע -> מתי נשלחה תזכורת
_leave_cache: dict[str, tuple] = {}    # מפתח אירוע -> (leave_at, origin, minutes)


def _key(e: dict) -> str:
    return f"{e.get('id') or e['summary']}@{e['start'].isoformat()}"


async def _remind_at(e: dict, same_day: list[dict]) -> tuple[datetime, str]:
    """
    מתי להזכיר ומה לכתוב. אירוע עם כתובת מחוץ לבית: REMIND_MINUTES
    לפני היציאה. כל השאר: REMIND_MINUTES לפני תחילת האירוע.
    """
    start = e["start"].astimezone(TZ)
    when = start.strftime("%H:%M")
    loc = (e.get("location") or "").strip()
    head = f"⏰ {e['summary']} ב-{when}"

    if loc and travel.classify(e) == "physical" and not travel.is_home(loc):
        key = _key(e)
        if key not in _leave_cache:
            try:
                _leave_cache[key] = await travel.departure_hint(start, loc, same_day)
            except Exception as exc:
                logger.warning("חישוב יציאה נכשל: %s", exc)
                _leave_cache[key] = (None, None, None)
        leave_at, origin, minutes = _leave_cache[key]
        if leave_at:
            src = "מהבית" if origin == travel.HOME_ADDRESS else f"מ{origin}"
            text = (f"{head}\n🚗 לצאת ב-{leave_at:%H:%M} — {minutes} דק' נסיעה {src}"
                    f"\n📍 {loc}\n{travel.waze_link(loc)}")
            return leave_at - timedelta(minutes=REMIND_MINUTES), text
        return start - timedelta(minutes=REMIND_MINUTES), f"{head}\n📍 {loc}"

    return start - timedelta(minutes=REMIND_MINUTES), f"{head} (בעוד {REMIND_MINUTES} דק')"


async def event_reminders(now: Optional[datetime] = None) -> int:
    """רץ כל 5 דקות. מחזיר כמה תזכורות נשלחו (לבדיקות)."""
    if _deps is None or not REMINDERS_ON:
        return 0
    now = now or datetime.now(TZ)
    if policy.in_quiet_hours(now):
        return 0

    # ניקוי זיכרון ישן
    for k, t in list(_reminded.items()):
        if now - t > timedelta(days=1):
            _reminded.pop(k, None)
            _leave_cache.pop(k, None)

    try:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = await _deps.fetch_events(day_start, day_start + timedelta(days=1))
    except Exception as exc:
        logger.warning("תזכורות: קריאת יומן נכשלה: %s", exc)
        return 0

    sent = 0
    for e in events:
        if _is_all_day(e) or e["start"] <= now:
            continue
        if e["start"] - now > timedelta(hours=4):
            continue
        key = _key(e)
        if key in _reminded:
            continue
        remind_at, text = await _remind_at(e, events)
        # חלון של 10 דק' — אם השרת עלה באמצע, לא נשלח תזכורת שכבר איחרה מדי
        if remind_at <= now < remind_at + timedelta(minutes=10):
            await _send(text)
            _reminded[key] = now
            sent += 1
    return sent
