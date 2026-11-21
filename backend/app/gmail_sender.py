"""
שליחת מיילים — תמיד אחרי אישור בטלגרם, לעולם לא אוטומטית.

העיקרון: ניקול מנסחת, רון מאשר. גם בטריאז' האוטומטי, טיוטה שהיא
בטוחה בה מגיעה ככפתור ✅ — הלחיצה שלו היא השליחה.
scope: gmail.send (נוסף ל-config; מחייב אימות מחדש חד-פעמי).
"""

import asyncio
import base64
import logging
from email.mime.text import MIMEText
from typing import Optional

from app import gmail_reader

logger = logging.getLogger(__name__)


def _send_sync(to: str, subject: str, body: str,
               thread_id: Optional[str] = None,
               in_reply_to: Optional[str] = None) -> bool:
    service = gmail_reader._service()
    if service is None:
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}
    if thread_id:
        payload["threadId"] = thread_id

    service.users().messages().send(userId="me", body=payload).execute()
    return True


async def send(to: str, subject: str, body: str,
               thread_id: Optional[str] = None,
               in_reply_to: Optional[str] = None) -> bool:
    try:
        return await asyncio.to_thread(
            _send_sync, to, subject, body, thread_id, in_reply_to)
    except Exception as exc:
        logger.error("שליחת מייל נכשלה: %s", exc)
        return False


async def send_reply(original_msg_id: str, body: str) -> str:
    """תשובה למייל לפי המזהה שלו — משורשרת לאותו thread."""
    orig = await gmail_reader.read_full(original_msg_id)
    if orig is None:
        return "לא מצאתי את המייל המקורי — אולי נמחק?"
    subject = orig["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    ok = await send(orig["reply_to"], subject, body,
                    thread_id=orig["thread_id"],
                    in_reply_to=orig["message_id_header"])
    return f"נשלחה תשובה אל {orig['from_addr']}." if ok else "השליחה נכשלה — בדוק את הלוג."
