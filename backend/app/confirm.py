"""
שכבת האישור.

הזרימה: הסוכן מציע -> נשמר ב-pending_actions -> אתה לוחץ כפתור או עונה
בטקסט -> הפעולה מבוצעת.

שלוש נקודות שנשברות בקלות אם לא מטפלים בהן, ולכן טופלו כאן:
  1. שרת שנופל — ההצעה ב-DB, לא בזיכרון. אחרי restart הכפתורים עדיין עובדים.
  2. לחיצה על הצעה ישנה — ה-callback נושא את מזהה ההצעה, ומושווה למה
     שפתוח כרגע. לחיצה על הצעה שפגה או שהוחלפה מקבלת הודעה מסודרת.
  3. תשובה בטקסט במקום כפתור — "כן", "לא", "אשר" מזוהים לפני שהודעה
     נשלחת ל-LLM. חוסך קריאה ומונע פרשנות מוזרה.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

import httpx

from app import db
from app import travel

logger = logging.getLogger(__name__)

TELEGRAM_API_TOKEN = None  # מוזרק מ-config בזמן אתחול
TELEGRAM_BASE = "https://api.telegram.org"

# תשובות טקסט שנחשבות אישור או דחייה, לפני שמערבים את המודל
YES_WORDS = {"כן", "אשר", "אישור", "בסדר", "אוקיי", "אוקי", "סבבה", "yes", "ok", "y"}
NO_WORDS = {"לא", "בטל", "ביטול", "עזוב", "no", "cancel", "n"}


# ------------------------------------------------------ רישום מבצעים

# action_type -> פונקציה אסינכרונית שמקבלת payload ומחזירה טקסט תוצאה
_EXECUTORS: dict[str, Callable[[dict], Awaitable[str]]] = {}


def register_executor(action_type: str):
    """
    דקורטור. שומר על שכבת האישור מנותקת משירות היומן —
    מה שמאפשר לבדוק אותה בלי גוגל בכלל.

        @register_executor("create_event")
        async def _(payload): ...
    """
    def wrapper(fn):
        _EXECUTORS[action_type] = fn
        return fn
    return wrapper


# -------------------------------------------------------- טלגרם

async def _tg(method: str, payload: dict) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TELEGRAM_BASE}/bot{TELEGRAM_API_TOKEN}/{method}", json=payload
            )
            return resp.json()
    except Exception as exc:
        logger.error("קריאת טלגרם %s נכשלה: %s", method, exc)
        return None


def _keyboard(action_id: int, shift_minutes: Optional[int]) -> dict:
    buttons = [
        {"text": "✅ אשר", "callback_data": f"ok:{action_id}"},
        {"text": "❌ בטל", "callback_data": f"no:{action_id}"},
    ]
    row2 = []
    if shift_minutes:
        row2.append({
            "text": f"🕐 הזז ב-{shift_minutes} דק'",
            "callback_data": f"shift:{action_id}:{shift_minutes}",
        })
    rows = [buttons] + ([row2] if row2 else [])
    return {"inline_keyboard": rows}


# --------------------------------------------------------- הצעה

async def propose(
    chat_id: int,
    action_type: str,
    payload: dict,
    summary: str,
    conflicts: Optional[list] = None,
) -> int:
    """מציג הצעה ומחכה. מחזיר את מזהה ההצעה."""
    text = summary
    shift = None

    if conflicts:
        lines = "\n".join(f"⚠️ {c.message}" for c in conflicts)
        text = f"{summary}\n\n{lines}"
        shift = travel.suggest_shift(conflicts)

    action_id = db.create_pending(chat_id, action_type, payload, summary)

    await _tg("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": _keyboard(action_id, shift),
    })
    return action_id


async def propose_batch(chat_id: int, items: list[dict]) -> None:
    """
    כמה פריטים מהודעה אחת -> הודעת אישור אחת עם רשימה ממוספרת
    וכפתורי "אשר הכל" / "בטל הכל". פריט בודד עובר ל-propose הרגיל.

    כל פריט: {"action_type", "payload", "summary", "conflicts"}.
    הפריטים נשמרים כרגיל ב-pending_actions (שרידות ל-restart);
    הכפתור נושא את טווח המזהים.
    """
    if not items:
        return
    if len(items) == 1:
        it = items[0]
        await propose(chat_id, it["action_type"], it["payload"],
                      it["summary"], it.get("conflicts"))
        return

    ids = []
    lines = []
    for i, it in enumerate(items, start=1):
        action_id = db.create_pending(
            chat_id, it["action_type"], it["payload"], it["summary"])
        ids.append(action_id)
        line = f"{i}. {it['summary'].rstrip('?')}"
        for c in (it.get("conflicts") or []):
            line += f"\n   ⚠️ {c.message}"
        lines.append(line)

    id_range = f"{min(ids)}-{max(ids)}"
    await _tg("sendMessage", {
        "chat_id": chat_id,
        "text": f"מצאתי {len(items)} פריטים:\n\n" + "\n".join(lines)
                + "\n\nלאשר את כולם?",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ אשר הכל", "callback_data": f"okall:{id_range}"},
            {"text": "❌ בטל הכל", "callback_data": f"noall:{id_range}"},
        ]]},
    })


def _batch_ids(spec: str) -> list[int]:
    lo, hi = (int(x) for x in spec.split("-"))
    return list(range(lo, hi + 1))


def _shift_payload(payload: dict, minutes: int) -> dict:
    """מזיז start/end (ISO) קדימה. שאר השדות לא נוגעים."""
    shifted = dict(payload)
    for key in ("start", "end"):
        if payload.get(key):
            dt = datetime.fromisoformat(payload[key]) + timedelta(minutes=minutes)
            shifted[key] = dt.isoformat()
    return shifted


async def _execute(action: dict) -> str:
    executor = _EXECUTORS.get(action["action_type"])
    if executor is None:
        logger.error("אין מבצע רשום עבור %s", action["action_type"])
        return "משהו השתבש — לא ידעתי לבצע את הפעולה."
    try:
        return await executor(action["payload"])
    except Exception as exc:
        logger.error("ביצוע %s נכשל: %s", action["action_type"], exc)
        return "לא הצלחתי לבצע את זה. תרצה שננסה שוב?"


# ------------------------------------------------- טיפול בלחיצה

async def handle_callback(callback: dict) -> None:
    """callback_query נכנס מטלגרם."""
    data = callback.get("data", "")
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    callback_id = callback["id"]

    parts = data.split(":")
    verb = parts[0]

    # --- כפתורי קבוצה: אשר/בטל את כל הפריטים של ההודעה ---
    if verb in ("okall", "noall"):
        await _tg("answerCallbackQuery", {"callback_query_id": callback_id})
        results = []
        for aid in _batch_ids(parts[1]):
            pending = db.get_pending_by_id(aid)
            if pending is None:
                continue
            if verb == "okall":
                db.resolve_pending(aid, "confirmed")
                results.append(await _execute(pending))
            else:
                db.resolve_pending(aid, "rejected")
        summary_text = ("\n".join(results) if results
                        else ("בוטל." if verb == "noall" else "לא נשאר מה לבצע."))
        if verb == "noall" and not results:
            summary_text = "הכל בוטל."
        await _tg("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
        await _tg("sendMessage", {"chat_id": chat_id, "text": summary_text})
        db.add_message(chat_id, "assistant", summary_text)
        return

    action_id = int(parts[1]) if len(parts) > 1 else None

    # מאתרים לפי המזהה שבכפתור, לא לפי "ההצעה הפתוחה" —
    # ככה כמה הצעות יכולות להמתין במקביל בלי להתבלבל.
    pending = db.get_pending_by_id(action_id) if action_id else None

    if pending is None:
        await _tg("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "ההצעה כבר לא רלוונטית",
        })
        await _tg("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
        return

    await _tg("answerCallbackQuery", {"callback_query_id": callback_id})

    if verb == "no":
        db.resolve_pending(action_id, "rejected")
        result = "בוטל."
    elif verb == "shift":
        minutes = int(parts[2])
        pending["payload"] = _shift_payload(pending["payload"], minutes)
        db.resolve_pending(action_id, "confirmed")
        result = await _execute(pending)
    else:
        db.resolve_pending(action_id, "confirmed")
        result = await _execute(pending)

    # מסירים את הכפתורים כדי שלא ילחצו פעמיים
    await _tg("editMessageReplyMarkup", {
        "chat_id": chat_id, "message_id": message_id,
        "reply_markup": {"inline_keyboard": []},
    })
    await _tg("sendMessage", {"chat_id": chat_id, "text": result})
    db.add_message(chat_id, "assistant", result)


# ------------------------------------------ טיפול באישור בטקסט

async def try_text_confirmation(chat_id: int, text: str) -> bool:
    """
    נקרא בתחילת הטיפול בהודעה, לפני ה-LLM.
    מחזיר True אם ההודעה טופלה כאן ואין להמשיך הלאה.
    """
    open_actions = db.list_pending(chat_id)
    if not open_actions:
        return False

    word = text.strip().lower().rstrip("!.").strip()

    if word not in YES_WORDS and word not in NO_WORDS:
        # תשובה מורכבת כמו "כן אבל בשעה 4" — ההצעות נשארות פתוחות
        # וה-LLM יראה אותן בהקשר.
        return False

    if len(open_actions) > 1:
        # אחרי הודעת אישור מרוכזת, "כן" מתייחס לכל הרשימה —
        # בדיוק כמו הכפתור "אשר הכל".
        results = []
        for pending in open_actions:
            if word in YES_WORDS:
                db.resolve_pending(pending["id"], "confirmed")
                results.append(await _execute(pending))
            else:
                db.resolve_pending(pending["id"], "rejected")
        text_out = "\n".join(results) if results else "הכל בוטל."
        await _tg("sendMessage", {"chat_id": chat_id, "text": text_out})
        db.add_message(chat_id, "assistant", text_out)
        return True

    pending = open_actions[0]

    if word in YES_WORDS:
        db.resolve_pending(pending["id"], "confirmed")
        result = await _execute(pending)
    else:
        db.resolve_pending(pending["id"], "rejected")
        result = "בוטל."

    await _tg("sendMessage", {"chat_id": chat_id, "text": result})
    db.add_message(chat_id, "assistant", result)
    return True
