"""
קריאת מיילים — לבריף הבוקר ולשאלות "יש לי מיילים חדשים?".

קריאה בלבד (gmail.readonly) בכוונה: ניקול מסכמת ומפנה תשומת לב,
היא לא שולחת ולא מוחקת. משתמשים באותם credentials של היומן —
אותו token.pickle, בלי OAuth נוסף (רק אימות מחדש חד-פעמי בגלל
ה-scope שנוסף).
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_calendar = None   # שירות היומן של CaliBOT — מחזיק את ה-credentials


def init(calendar_service) -> None:
    global _calendar
    _calendar = calendar_service


def _service():
    if _calendar is None or not getattr(_calendar, "credentials", None):
        return None
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_calendar.credentials,
                 cache_discovery=False)


# סימנים מובהקים של דיוור שיווקי — בעברית ובאנגלית.
# קבלות ואישורי רכישה נשארים; ניוזלטרים ומבצעים לא.
PROMO_MARKERS = (
    "פרסומת", "מבצע", "הנחה", "דיוור", "ניוזלטר", "הטבה", "קופון",
    "unsubscribe", "newsletter", "promotion", "sale", "% off", "deal",
    "מציאות", "בלעדי לחברי",
)


def _looks_promotional(email: dict) -> bool:
    text = f"{email['from']} {email['subject']} {email['snippet']}".lower()
    return any(marker in text for marker in PROMO_MARKERS)


def _fetch_sync(query: str, limit: int) -> list[dict]:
    service = _service()
    if service is None:
        return []

    listing = service.users().messages().list(
        userId="me", q=query, maxResults=limit,
    ).execute()

    emails = []
    for ref in listing.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        # "דוד לוי <david@x.com>" -> "דוד לוי"
        if "<" in sender:
            sender = sender.split("<")[0].strip().strip('"')
        emails.append({
            "id": ref["id"],
            "from": sender,
            "from_addr": headers.get("From", ""),
            "subject": headers.get("Subject", "(ללא נושא)"),
            "snippet": (msg.get("snippet") or "")[:120],
        })
    return emails


def _read_full_sync(msg_id: str) -> Optional[dict]:
    """מייל בודד עם גוף וכותרות — לניסוח תשובה."""
    import base64
    service = _service()
    if service is None:
        return None
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"]
               for h in msg.get("payload", {}).get("headers", [])}

    def _walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            found = _walk(sub)
            if found:
                return found
        return ""

    body = _walk(msg.get("payload", {})) or msg.get("snippet", "")
    return {
        "id": msg_id,
        "thread_id": msg.get("threadId"),
        "from_addr": headers.get("From", ""),
        "reply_to": headers.get("Reply-To") or headers.get("From", ""),
        "subject": headers.get("Subject", "(ללא נושא)"),
        "message_id_header": headers.get("Message-ID", ""),
        "body": body[:4000],
    }


async def read_full(msg_id: str) -> Optional[dict]:
    try:
        return await asyncio.to_thread(_read_full_sync, msg_id)
    except Exception as exc:
        logger.warning("קריאת מייל מלא נכשלה: %s", exc)
        return None


async def unread_recent(limit: int = 5) -> list[dict]:
    """מיילים שלא נקראו מהיממה האחרונה, מהתיבה הראשית."""
    try:
        emails = await asyncio.to_thread(
            _fetch_sync,
            # שלילת קטגוריות השיווק במקור — עובד גם כשאין טאבים בתיבה
            "in:inbox is:unread newer_than:1d "
            "-category:promotions -category:social",
            limit * 2,     # מביאים מרווח, כי הסינון המקומי עוד יצמצם
        )
        return [m for m in emails if not _looks_promotional(m)][:limit]
    except Exception as exc:
        logger.warning("קריאת מייל נכשלה: %s", exc)
        return []
