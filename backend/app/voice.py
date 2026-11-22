"""
הודעות קוליות.

הזרימה: טלגרם שולח voice -> מורידים את הקובץ -> תמלול -> הטקסט נכנס
לאותו pipeline בדיוק כמו הודעת טקסט. אין מסלול נפרד.

התמלול רץ על Whisper של Groq (עברו מ-Gemini באוגוסט 2026, אחרי
שגוגל התחילה לחסום גיאוגרפית IP של דאטהסנטרים).
Whisper מקבל ogg ישירות, תומך בעברית, והמכסה החינמית נדיבה.
עדיין בלי Whisper מקומי — ל-droplet יש היסטוריית OOM.
"""

import asyncio
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


FALLBACK_WHISPER = os.getenv("WHISPER_FALLBACK_MODEL", "whisper-large-v3")


async def _groq_whisper(audio: bytes, mime: str, model: str,
                        with_prompt: bool) -> tuple[Optional[str], str]:
    """(טקסט, שגיאה). השגיאה כוללת את גוף התשובה של Groq — 400 לבד לא אומר כלום."""
    ext = "ogg" if "ogg" in mime else "mp3"
    data = {"model": model, "language": "he", "temperature": "0",
            "response_format": "json"}
    if with_prompt:
        data["prompt"] = TRANSCRIBE_PROMPT
    try:
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT) as client:
            resp = await client.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"voice.{ext}", audio, mime)},
                data=data,
            )
        if resp.status_code >= 400:
            return None, f"{model}: {resp.status_code} {resp.text[:300]}"
        text = (resp.json().get("text") or "").strip()
        return (text or None), ("" if text else f"{model}: תמלול ריק")
    except Exception as exc:
        return None, f"{model}: {exc}"


def _to_mp3(audio: bytes) -> Optional[bytes]:
    """המרה עם ffmpeg — לקבצים ש-Groq לא מזהה את הפורמט שלהם."""
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-f", "mp3", "pipe:1"],
            input=audio, capture_output=True, timeout=120, check=True)
        return out.stdout or None
    except Exception as exc:
        logger.warning("המרת אודיו נכשלה: %s", exc)
        return None


async def _gemini_transcribe(audio: bytes) -> Optional[str]:
    """גיבוי אחרון: תמלול ב-Gemini (Vertex), אותו חשבון שירות של התמונות."""
    try:
        import base64
        from app import vision
        if vision._load_credentials() is None:
            return None
        text = await vision.vertex_call([
            {"text": "תמלל את ההקלטה מילה במילה, בשפה שבה מדברים (בדרך כלל "
                     "עברית). החזר רק את התמלול, בלי הקדמות."},
            {"inline_data": {"mime_type": "audio/mp3",
                             "data": base64.b64encode(audio).decode("ascii")}},
        ], max_tokens=4096, timeout=120)
        return (text or "").strip() or None
    except Exception as exc:
        logger.warning("תמלול Gemini נכשל: %s", exc)
        return None


async def transcribe(audio: bytes, mime: str = "audio/ogg") -> Optional[str]:
    """
    תמלול עם שרשרת גיבויים:
      1. Groq Whisper (המודל המוגדר, עם רמז מילים)
      2. אותו קובץ אחרי המרה ל-mp3, בלי רמז, במודל הגיבוי של Groq
      3. Gemini ב-Vertex
    כל כישלון נרשם עם הסיבה המלאה מהשרת.
    """
    errors = []
    if GROQ_API_KEY:
        text, err = await _groq_whisper(audio, mime, WHISPER_MODEL, True)
        if text:
            return text
        errors.append(err)
        mp3 = await asyncio.to_thread(_to_mp3, audio) or audio
        text, err = await _groq_whisper(mp3, "audio/mpeg", FALLBACK_WHISPER, False)
        if text:
            logger.warning("תמלול הצליח רק בגיבוי Groq. שגיאה ראשונה: %s", errors[0])
            return text
        errors.append(err)
    else:
        errors.append("GROQ_API_KEY חסר")
        mp3 = await asyncio.to_thread(_to_mp3, audio) or audio

    text = await _gemini_transcribe(mp3)
    if text:
        logger.warning("תמלול הצליח רק ב-Gemini. שגיאות Groq: %s", " | ".join(errors))
        return text
    logger.error("תמלול נכשל בכל המסלולים: %s", " | ".join(errors))
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
