"""
מחליף את backend/app/api/models.py.

השינוי היחיד שחשוב: השדה callback_query. בלי זה FastAPI דוחה כל
לחיצה על כפתור אישור, והשכבה שבנינו פשוט לא תעבוד.
"""

from typing import Any, Optional

from pydantic import BaseModel


class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[dict[str, Any]] = None
    edited_message: Optional[dict[str, Any]] = None
    callback_query: Optional[dict[str, Any]] = None

    class Config:
        extra = "allow"     # טלגרם מוסיף שדות מדי פעם; לא ניפול בגללם
