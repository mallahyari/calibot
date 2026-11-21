"""
גיבוי בסיס הנתונים.

שתי החלטות שחשובות יותר משנראה:

1. **לא מעתיקים את הקובץ.** cp על sqlite שרץ יכול לתפוס אותו באמצע
   טרנזקציה ולייצר גיבוי פגום — שתגלה בדיוק ביום שתצטרך אותו.
   כאן משתמשים ב-Online Backup API של sqlite, שמייצר עותק עקבי
   גם כשהבוט כותב באותו רגע.

2. **הגיבוי יוצא מהשרת.** גיבוי שיושב על אותו droplet שנפל הוא לא
   גיבוי. שולחים אותו לטלגרם — הקובץ קטן, אין צורך בהרשאות חדשות,
   ואין scope נוסף ב-OAuth של גוגל.

למה לא Google Drive: ה-OAuth הקיים מבקש scope של יומן בלבד. הוספת
Drive מחייבת אישור מחדש של החשבון. אפשר, אבל זה מחיר גבוה לפיצ'ר
שטלגרם פותר בחינם.
"""

import gzip
import io
import json
import tarfile
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from app import db

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", Path(__file__).parent / "backups"))
KEEP_LOCAL = int(os.getenv("BACKUP_KEEP", "14"))

TELEGRAM_API_TOKEN = None      # מוזרק מ-main
BACKUP_CHAT_ID = os.getenv("BACKUP_CHAT_ID") or os.getenv("OWNER_CHAT_ID")

# מעל זה טלגרם דוחה. גם התרעה שמשהו לא בסדר בגודל ה-DB.
MAX_UPLOAD_BYTES = 45 * 1024 * 1024


def create_snapshot() -> Optional[Path]:
    """יוצר עותק עקבי ודחוס. מחזיר את הנתיב, או None בכישלון."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    raw = BACKUP_DIR / f"assistant-{stamp}.db"
    packed = raw.with_suffix(".db.gz")

    try:
        source = sqlite3.connect(db.DB_PATH)
        target = sqlite3.connect(raw)
        with target:
            source.backup(target)       # ה-API שמבטיח עותק עקבי
        target.close()
        source.close()

        with open(raw, "rb") as f_in, gzip.open(packed, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        raw.unlink()

        logger.info("גיבוי נוצר: %s (%s בייטים)", packed.name,
                    packed.stat().st_size)
        return packed

    except Exception as exc:
        logger.error("יצירת גיבוי נכשלה: %s", exc)
        raw.unlink(missing_ok=True)
        return None


def rotate() -> int:
    """משאיר את KEEP_LOCAL האחרונים. מחזיר כמה נמחקו."""
    if not BACKUP_DIR.exists():
        return 0
    removed = 0
    for pattern in ("assistant-*.db.gz", "memory-*.tar.gz"):
        files = sorted(BACKUP_DIR.glob(pattern),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        removed += _rotate_files(files)
    return removed


def _rotate_files(files) -> int:
    removed = 0
    for old in files[KEEP_LOCAL:]:
        try:
            old.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("מחיקת גיבוי ישן נכשלה: %s", exc)
    return removed


async def upload(path: Path) -> bool:
    """שולח את הגיבוי לטלגרם — עותק מחוץ לשרת."""
    if not TELEGRAM_API_TOKEN or not BACKUP_CHAT_ID:
        logger.warning("אין טוקן או chat_id לגיבוי — נשמר מקומית בלבד")
        return False

    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        logger.error("הגיבוי גדול מדי לשליחה: %s בייטים", size)
        return False

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(path, "rb") as fh:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/sendDocument",
                    data={
                        "chat_id": str(BACKUP_CHAT_ID),
                        "caption": ("גיבוי זיכרון " if "memory" in path.name
                                    else "גיבוי ") + f"{datetime.now(TZ):%d/%m/%Y}",
                        "disable_notification": "true",
                    },
                    files={"document": (path.name, fh, "application/gzip")},
                )
        data = resp.json()
        ok = data.get("ok", False)
        if not ok:
            logger.error("שליחת גיבוי נכשלה: %s", resp.text)
        else:
            _remember_upload(path.name, data["result"]["document"]["file_id"])
        return ok

    except Exception as exc:
        logger.error("שליחת גיבוי נכשלה: %s", exc)
        return False


# --------------------------------------------- בדיקת שחזור
# גיבוי שלא נבדק הוא תקווה. פעם בחודש מורידים את מה שבאמת יושב בטלגרם
# (לא את העותק המקומי) ומוודאים שהוא נפתח: sqlite תקין, טבלאות עם נתונים,
# ארכיון זיכרון שנקרא. התוצאה נשלחת לבעלים — גם כשהכל תקין.

UPLOADS_FILE = BACKUP_DIR / "last_uploads.json"


def _remember_upload(name: str, file_id: str) -> None:
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        data = json.loads(UPLOADS_FILE.read_text()) if UPLOADS_FILE.exists() else {}
        kind = "memory" if name.startswith("memory-") else "db"
        data[kind] = {"name": name, "file_id": file_id,
                      "at": datetime.now(TZ).isoformat(timespec="minutes")}
        UPLOADS_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        logger.warning("שמירת מזהה הגיבוי נכשלה: %s", exc)


async def _download(client: httpx.AsyncClient, file_id: str) -> bytes:
    base = f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}"
    meta = (await client.get(f"{base}/getFile", params={"file_id": file_id})).json()
    path = meta["result"]["file_path"]
    resp = await client.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_API_TOKEN}/{path}")
    resp.raise_for_status()
    return resp.content


def _check_db(blob: bytes) -> str:
    raw = gzip.decompress(blob)
    tmp = BACKUP_DIR / "verify.db"
    tmp.write_bytes(raw)
    try:
        con = sqlite3.connect(tmp)
        status = con.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise ValueError(f"integrity_check: {status}")
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        rows = 0
        for t in tables:
            try:
                rows += con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                pass
        con.close()
        return f"מסד נתונים: {len(tables)} טבלאות, {rows:,} רשומות, תקין"
    finally:
        tmp.unlink(missing_ok=True)


def _check_memory(blob: bytes) -> str:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        md = [m for m in tar.getmembers() if m.name.endswith(".md")]
        for m in md:
            tar.extractfile(m).read()
    return f"זיכרון: {len(md)} קבצים, נקראו בהצלחה"


async def verify_restore() -> str:
    """
    ג'וב חודשי. מחזיר את הדוח (גם לשימוש ידני):
        python -c "import asyncio; from app import backup; print(asyncio.run(backup.verify_restore()))"
    """
    lines, failed = [], False
    try:
        data = json.loads(UPLOADS_FILE.read_text()) if UPLOADS_FILE.exists() else {}
    except Exception:
        data = {}
    if not data.get("db"):
        lines.append("❌ אין גיבוי מסד נתונים רשום שנשלח לטלגרם")
        failed = True

    async with httpx.AsyncClient(timeout=120.0) as client:
        for kind, check in (("db", _check_db), ("memory", _check_memory)):
            entry = data.get(kind)
            if not entry:
                continue
            try:
                blob = await _download(client, entry["file_id"])
                lines.append(f"✅ {check(blob)} ({entry['name']})")
            except Exception as exc:
                failed = True
                lines.append(f"❌ {entry['name']}: לא נפתח — {exc}")

    report = ("⚠️ בדיקת שחזור גיבוי נכשלה" if failed
              else "🗄️ בדיקת שחזור חודשית: הגיבוי בטלגרם נפתח תקין") + \
        "\n" + "\n".join(lines)
    logger.info(report)
    owner = os.getenv("OWNER_CHAT_ID")
    if TELEGRAM_API_TOKEN and owner:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/sendMessage",
                    data={"chat_id": owner, "text": report})
        except Exception as exc:
            logger.error("שליחת דוח בדיקת הגיבוי נכשלה: %s", exc)
    return report


def create_memory_snapshot() -> Optional[Path]:
    """אורז את תיקיית הזיכרון (יומן ופתקים) ל-tar.gz. None אם ריקה/חסרה."""
    from app import memfiles
    mem_dir = Path(memfiles.MEMORY_DIR)
    if not mem_dir.exists() or not any(mem_dir.rglob("*.md")):
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    packed = BACKUP_DIR / f"memory-{stamp}.tar.gz"
    try:
        with tarfile.open(packed, "w:gz") as tar:
            tar.add(mem_dir, arcname="memory")
        logger.info("גיבוי זיכרון נוצר: %s (%s בייטים)", packed.name,
                    packed.stat().st_size)
        return packed
    except Exception as exc:
        logger.error("גיבוי הזיכרון נכשל: %s", exc)
        packed.unlink(missing_ok=True)
        return None


async def daily_backup() -> None:
    """ג'וב מתוזמן. שקט בהצלחה, רועש בכישלון."""
    snapshot = create_snapshot()
    if snapshot is not None:
        await upload(snapshot)

    mem_snapshot = create_memory_snapshot()
    if mem_snapshot is not None:
        await upload(mem_snapshot)

    removed = rotate()
    if removed:
        logger.info("נמחקו %s גיבויים ישנים", removed)


def latest() -> Optional[Path]:
    """הגיבוי האחרון — לשחזור."""
    if not BACKUP_DIR.exists():
        return None
    files = sorted(BACKUP_DIR.glob("assistant-*.db.gz"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def restore(archive: Path, target: Path = None) -> bool:
    """
    שחזור. עוצרים את השירות, מריצים, מפעילים מחדש:
        python -c "import backup,pathlib; backup.restore(pathlib.Path('backups/assistant-....db.gz'))"
    """
    target = target or Path(db.DB_PATH)
    try:
        if target.exists():
            target.rename(target.with_suffix(".db.before-restore"))
        with gzip.open(archive, "rb") as f_in, open(target, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        logger.info("שוחזר מ-%s", archive.name)
        return True
    except Exception as exc:
        logger.error("שחזור נכשל: %s", exc)
        return False
