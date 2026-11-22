"""
מחליף את backend/app/main.py.

כאן נסגרים כל החיבורים: DB, טוקנים, מבצעים, מתזמן, וובהוק.
"""

import logging
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI

from app import backup
from app import confirm
from app import db
from app import executors
from app import gmail_reader
from app import policy
from app import scheduler
from app import vision
from app import vision
from app import voice
from app.api.routes import router, calendar_service
from app.config import API_HOST, API_PORT, TELEGRAM_API_TOKEN
from app.services.telegram import send_telegram_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# הדומיין הציבורי של ה-droplet. מחליף את ה-ngrok המקודד קשיח.
PUBLIC_URL = os.getenv("PUBLIC_URL")

OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))


async def _send(chat_id: int, text: str) -> None:
    """עטיפה שמכבדת שעות שקט ושבת — המתזמן משתמש בה, לא בשליחה ישירה."""
    allowed, reason = await policy.may_send()
    if not allowed:
        logger.info("הודעה יזומה נחסמה (%s)", reason)
        return
    await send_telegram_message(chat_id, text)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- 1. אחסון ---
    db.init_db()
    from app import memfiles
    memfiles.ensure_dirs()

    # --- 2. הזרקת טוקנים ---
    confirm.TELEGRAM_API_TOKEN = TELEGRAM_API_TOKEN
    voice.TELEGRAM_API_TOKEN = TELEGRAM_API_TOKEN
    vision.TELEGRAM_API_TOKEN = TELEGRAM_API_TOKEN
    backup.TELEGRAM_API_TOKEN = TELEGRAM_API_TOKEN
    vision.TELEGRAM_API_TOKEN = TELEGRAM_API_TOKEN

    # --- 3. מבצעים + כלי קריאה ---
    executors.set_calendar_service(calendar_service, OWNER_CHAT_ID)
    gmail_reader.init(calendar_service)
    executors.set_prompt_job_registrar(scheduler.register_prompt_job)
    executors.set_prompt_job_remover(scheduler.remove_prompt_job)

    # --- 4. וובהוק ---
    if not PUBLIC_URL:
        logger.error("PUBLIC_URL לא הוגדר — הוובהוק לא נרשם")
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/setWebhook",
                params={
                    "url": f"{PUBLIC_URL}/webhook",
                    # בלי זה טלגרם לא שולח לחיצות על כפתורים
                    "allowed_updates": '["message","callback_query"]',
                },
            )
            if not resp.json().get("ok"):
                logger.error("רישום וובהוק נכשל: %s", resp.text)

    # --- 5. מתזמן ---
    if OWNER_CHAT_ID:
        scheduler.start_scheduler(scheduler.Deps(
            chat_id=OWNER_CHAT_ID,
            send=_send,
            fetch_events=executors.fetch_events,
        ))
    else:
        logger.error("OWNER_CHAT_ID לא הוגדר — אין הודעות יזומות")

    yield

    scheduler.stop_scheduler()
    async with httpx.AsyncClient() as client:
        await client.get(
            f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/deleteWebhook"
        )


app = FastAPI(title="עוזר אישי", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok", "jobs": len(scheduler._scheduler.get_jobs())
            if scheduler._scheduler else 0}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=API_HOST, port=API_PORT)
