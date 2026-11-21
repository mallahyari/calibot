"""
הודעות קוליות.

הזרימה: טלגרם שולח voice -> מורידים את הקובץ -> תמלול -> הטקסט נכנס
לאותו pipeline בדיוק כמו הודעת טקסט. אין מסלול נפרד.

התמלול רץ על Whisper של Groq (עברו מ-Gemini באוגוסט 2026, אחרי
שגוגל התחילה לחסום גיאוגרפית IP של דאטהסנטרים).
Whisper מקבל ogg ישירות, תומך בעברית, והמכסה החינמית נדיבה.
עדיין בלי Whisper מקומי — ל-droplet יש היסטוריית OOM.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_TOKEN = None  # מוזרק מ-config באתחול
TELEGRAM_BASE = "https://api.telegram.org"

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# הודעה ארוכה מזה כמעט תמיד שיחה שנשלחה בטעות, לא פנייה לסוכן
MAX_DURATION_SECONDS = 300
MAX_FILE_BYTES = 20 * 1024 * 1024

DOWNLOAD_TIMEOUT = 30.0
TRANSCRIBE_TIMEOUT = 60.0

# רמז ל-Whisper לאיות נכון של מונחים צפויים
TRANSCRIBE_PROMPT = "פגישה, תור, יומן, תזכורת, משימה"


async def download_voice(file_id: str) -> Optional[tuple[bytes, str]]:
    """מוריד קובץ קולי מטלגרם. מחזיר (bytes, mime) או None."""
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT) as client:
            meta = await client.get(
                f"{TELEGRAM_BASE}/bot{TELEGRAM_API_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            meta.raise_for_status()
            info = meta.json()

            if not info.get("ok"):
                logger.warning("getFile החזיר שגיאה: %s", info)
                return None

            result = info["result"]
            size = result.get("file_size", 0)
            if size > MAX_FILE_BYTES:
                logger.info("קובץ קולי גדול מדי: %s בייטים", size)
                return None

            path = result["file_path"]
            audio = await client.get(
                f"{TELEGRAM_BASE}/file/bot{TELEGRAM_API_TOKEN}/{path}"
            )
            audio.raise_for_status()

        mime = "audio/ogg" if path.endswith(".oga") or path.endswith(".ogg") \
            else "audio/mpeg"
        return audio.content, mime

    except Exception as exc:
        logger.error("הורדת קובץ קולי נכשלה: %s", exc)
        return None


async def transcribe(audio: bytes, mime: str = "audio/ogg") -> Optional[str]:
    """תמלול דרך Whisper של Groq. מחזיר טקסט או None בכישלון."""
    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY חסר — אין תמלול")
        return None

    ext = "ogg" if "ogg" in mime else "mp3"

    try:
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT) as client:
            resp = await client.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"voice.{ext}", audio, mime)},
                data={
                    "model": WHISPER_MODEL,
                    "language": "he",
                    "prompt": TRANSCRIBE_PROMPT,
                    "temperature": "0",
                    "response_format": "json",
                },
            )
            resp.raise_for_status()
            text = (resp.json().get("text") or "").strip()
            return text or None

    except Exception as exc:
        logger.error("תמלול נכשל: %s", exc)
        return None


def extract_voice(message: dict) -> Optional[dict]:
    """
    שולף voice או audio מהודעת טלגרם.
    מחזיר {'file_id', 'duration'} או None אם זו לא הודעה קולית.
    """
    media = message.get("voice") or message.get("audio")
    if not media:
        return None
    return {
        "file_id": media["file_id"],
        "duration": media.get("duration", 0),
    }


async def voice_to_text(message: dict) -> tuple[Optional[str], Optional[str]]:
    """
    נקודת הכניסה מ-routes.
    מחזיר (טקסט, שגיאה_להצגה). בדיוק אחד מהם יהיה None.
    """
    media = extract_voice(message)
    if not media:
        return None, None

    if media["duration"] > MAX_DURATION_SECONDS:
        return None, "ההקלטה ארוכה מדי. תוכל לשלוח משהו קצר יותר?"

    downloaded = await download_voice(media["file_id"])
    if downloaded is None:
        return None, "לא הצלחתי להוריד את ההקלטה. תנסה שוב?"

    audio, mime = downloaded
    text = await transcribe(audio, mime)

    if not text:
        return None, "לא הצלחתי לתמלל את ההקלטה. תוכל לכתוב במקום?"

    logger.info("תומלל (%s שניות): %s", media["duration"], text[:80])
    return text, None
