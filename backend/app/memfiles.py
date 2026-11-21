"""
זיכרון בקבצי Markdown — "המוח" של ניקול, בהשראת ארכיטקטורת ה-Personal OS.

מבנה על הדיסק (MEMORY_DIR, ברירת מחדל backend/memory; בשרת /opt/assistant/memory):
    journal/2026-08.md   יומן — קובץ לחודש, ערך לכל יום
    notes/<topic>.md     פתקים לפי נושא ("רכב", "פרויקט-כספת"...)

הקבצים הם טקסט פשוט בכוונה: אפשר לקרוא ולערוך אותם גם בלי ניקול,
לגבות אותם עם git, ולתת לכל כלי AI אחר לעבוד עליהם.
"""

import os
import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Jerusalem")

_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory")
MEMORY_DIR = os.getenv("MEMORY_DIR", _DEFAULT)
JOURNAL_DIR = os.path.join(MEMORY_DIR, "journal")
NOTES_DIR = os.path.join(MEMORY_DIR, "notes")

MAX_NOTE_CHARS = 20_000


def ensure_dirs() -> None:
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    os.makedirs(NOTES_DIR, exist_ok=True)


def _slug(topic: str) -> str:
    """שם קובץ בטוח מתוך נושא חופשי (כולל עברית)."""
    clean = re.sub(r"[^\w\u0590-\u05FF-]+", "-", topic.strip()).strip("-")
    return clean[:60] or "כללי"


# ------------------------------------------------------------- יומן

def append_journal(text: str, when: datetime = None) -> None:
    """מוסיף ערך ליומן של היום. ערכים מאותו יום מתאחדים תחת כותרת אחת."""
    ensure_dirs()
    now = when or datetime.now(TZ)
    path = os.path.join(JOURNAL_DIR, now.strftime("%Y-%m") + ".md")
    day_header = f"## {now.strftime('%d/%m/%Y')}"

    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()

    entry = f"- {now.strftime('%H:%M')} — {text.strip()}\n"
    if day_header in existing:
        content = existing.rstrip("\n") + "\n" + entry
    else:
        content = (existing + f"\n{day_header}\n{entry}") if existing else f"{day_header}\n{entry}"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def recent_journal(days: int = 7) -> str:
    """הערכים מהימים האחרונים — לקריאה של הסוכן."""
    ensure_dirs()
    cutoff = datetime.now(TZ) - timedelta(days=days)
    months = {cutoff.strftime("%Y-%m"), datetime.now(TZ).strftime("%Y-%m")}
    chunks = []
    for m in sorted(months):
        path = os.path.join(JOURNAL_DIR, f"{m}.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                chunks.append(f.read())
    text = "\n".join(chunks).strip()
    if not text:
        return ""

    # מסננים רק ימים בטווח
    keep, current_ok = [], False
    for line in text.splitlines():
        m = re.match(r"## (\d{2})/(\d{2})/(\d{4})", line)
        if m:
            d = datetime(int(m[3]), int(m[2]), int(m[1]), tzinfo=TZ)
            current_ok = d >= cutoff.replace(hour=0, minute=0)
        if current_ok:
            keep.append(line)
    return "\n".join(keep)[-6000:]


# ------------------------------------------------------------- פתקים

def write_note(topic: str, content: str, append: bool = True) -> str:
    """כותב/מוסיף לפתק נושא. מחזיר את שם הקובץ."""
    ensure_dirs()
    name = _slug(topic)
    path = os.path.join(NOTES_DIR, name + ".md")
    stamp = datetime.now(TZ).strftime("%d/%m/%Y")

    if append and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
        content = existing.rstrip("\n") + f"\n\n_{stamp}_\n{content.strip()}\n"
    else:
        content = f"# {topic.strip()}\n\n_{stamp}_\n{content.strip()}\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content[-MAX_NOTE_CHARS:])
    return name


def read_note(topic: str) -> str:
    ensure_dirs()
    path = os.path.join(NOTES_DIR, _slug(topic) + ".md")
    if not os.path.exists(path):
        # חיפוש רך: אולי הנושא הוא חלק משם קובץ קיים
        target = _slug(topic)
        for f in os.listdir(NOTES_DIR):
            if target in f or f.replace(".md", "") in target:
                path = os.path.join(NOTES_DIR, f)
                break
        else:
            return ""
    with open(path, encoding="utf-8") as f:
        return f.read()[-6000:]


def list_notes() -> list[str]:
    ensure_dirs()
    return sorted(f[:-3] for f in os.listdir(NOTES_DIR) if f.endswith(".md"))
