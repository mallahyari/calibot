"""
תשובות קוליות — ניקול עונה בקול כשמדברים אליה בקול.

edge-tts משתמש בקולות הנוירליים של מיקרוסופט: חינמי, בלי מפתח,
ועם קול עברי טבעי. הסינתזה מייצרת mp3; טלגרם דורש OGG/Opus כדי
שההודעה תוצג כהודעה קולית (עיגול עם גל), אז ממירים עם ffmpeg.
אם ffmpeg חסר — שולחים כקובץ שמע רגיל. אם הסינתזה נכשלת —
נשלח טקסט בלבד, כרגיל. הקול לעולם לא חוסם תשובה.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# קול עברי נשי — מתאים לניקול. אפשר להחליף ב-.env
VOICE = os.getenv("TTS_VOICE", "he-IL-HilaNeural")

# תקרה: תשובות ארוכות נשארות טקסט. אף אחד לא רוצה נאום של דקה.
MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "400"))

_FFMPEG = shutil.which("ffmpeg")


async def synthesize(text: str) -> tuple[Optional[bytes], str]:
    """
    (בייטים, סוג) — סוג הוא 'voice' (ogg/opus) או 'audio' (mp3).
    (None, '') בכל כישלון.
    """
    if not text or len(text) > MAX_CHARS:
        return None, ""

    try:
        import edge_tts
    except ImportError:
        logger.warning("edge-tts לא מותקן — אין תשובות קוליות")
        return None, ""

    tmp = Path(tempfile.mkdtemp())
    mp3 = tmp / "reply.mp3"
    try:
        await edge_tts.Communicate(text, VOICE).save(str(mp3))

        if _FFMPEG:
            ogg = tmp / "reply.ogg"
            proc = await asyncio.create_subprocess_exec(
                _FFMPEG, "-y", "-i", str(mp3),
                "-c:a", "libopus", "-b:a", "32k", str(ogg),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0 and ogg.exists():
                return ogg.read_bytes(), "voice"

        return mp3.read_bytes(), "audio"

    except Exception as exc:
        logger.error("סינתזת קול נכשלה: %s", exc)
        return None, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
