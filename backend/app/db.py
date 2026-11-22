"""
שכבת אחסון לסוכן האישי.
SQLite — מחליף את ConversationState שבזיכרון, ומוסיף משימות ופעולות ממתינות.

הערות עיצוב:
- כל הזמנים נשמרים ב-UTC בפורמט ISO. ההמרה ל-Asia/Jerusalem היא לתצוגה בלבד.
- שימוש ב-sqlite3 סינכרוני בתוך FastAPI אסינכרוני: לעומס של משתמש יחיד
  הקריאות לוקחות מיקרו-שניות. אם תגדל — החלף ל-aiosqlite.
- pending_actions: רק פעולה אחת ממתינה לכל צ'אט בו-זמנית. הצעה חדשה
  מבטלת את הקודמת. זה מה שמונע את הבלבול של "כן" — למה בדיוק התכוונת.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).parent / "assistant.db"

# כמה זמן הצעה נשארת רלוונטית לפני שהיא פגה
PENDING_TTL_MINUTES = 180

# אחרי כמה ימים בלי טיפול משימה נחשבת "תקועה"
STUCK_AFTER_DAYS = 7

# מרווח מינימלי בין נדנוד לנדנוד על אותה משימה
NUDGE_COOLDOWN_DAYS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    title           TEXT    NOT NULL,
    notes           TEXT,
    status          TEXT    NOT NULL DEFAULT 'open',   -- open | done | dropped
    soft_due        TEXT,                              -- יעד רך, לא אירוע ביומן
    created_at      TEXT    NOT NULL,
    completed_at    TEXT,
    last_nudged_at  TEXT,
    nudge_count     INTEGER NOT NULL DEFAULT 0,
    linked_event    TEXT,                             -- כותרת האירוע שהמשימה מכינה אליו
    linked_date     TEXT                              -- YYYY-MM-DD של אותו אירוע
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(chat_id, status);

CREATE TABLE IF NOT EXISTS pending_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    action_type  TEXT    NOT NULL,   -- create_event | update_event | delete_event | add_task
    payload      TEXT    NOT NULL,   -- JSON: הארגומנטים המדויקים לביצוע
    summary      TEXT    NOT NULL,   -- מה שהוצג לך בעברית
    status       TEXT    NOT NULL DEFAULT 'pending',
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending ON pending_actions(chat_id, status);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    role        TEXT    NOT NULL,    -- user | assistant
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages ON messages(chat_id, id DESC);

CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    log_date    TEXT    NOT NULL,   -- YYYY-MM-DD מקומי
    content     TEXT    NOT NULL,   -- מה שדיווחת שעשית בפועל
    source      TEXT    NOT NULL DEFAULT 'evening_review',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity ON activity_log(chat_id, log_date DESC);

-- מטמון זמני נסיעה. בלעדיו כל בדיקת התנגשות היא קריאת API בתשלום.
-- החיים שלך מכילים כ-20 מקומות חוזרים, אז אחרי כמה שבועות רוב הבדיקות
-- ייענו מהמטמון בלי לצאת החוצה בכלל.
CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS habit_log (
    habit_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    UNIQUE(habit_id, log_date)
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    meal_date TEXT NOT NULL,
    meal_time TEXT NOT NULL,
    description TEXT NOT NULL,
    kcal INTEGER,
    protein_g INTEGER
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snap_date TEXT PRIMARY KEY,
    nav REAL,
    cash REAL,
    positions TEXT,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shopping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_seen (
    message_id TEXT PRIMARY KEY,
    seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduled_prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    day_of_week TEXT NOT NULL,      -- sun/mon/... או * לכל יום
    hour        INTEGER NOT NULL,
    minute      INTEGER NOT NULL,
    prompt      TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    chat_id     INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS travel_cache (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    origin         TEXT    NOT NULL,
    destination    TEXT    NOT NULL,
    minutes        INTEGER NOT NULL,
    mode           TEXT    NOT NULL DEFAULT 'driving',
    cached_at      TEXT    NOT NULL,
    UNIQUE(origin, destination, mode)
);

-- כתובת -> קואורדינטות. כתובת שתורגמה פעם אחת לא מתורגמת שוב.
CREATE TABLE IF NOT EXISTS geocode_cache (
    address     TEXT PRIMARY KEY,
    lon         REAL NOT NULL,
    lat         REAL NOT NULL,
    label       TEXT,
    cached_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """נקרא פעם אחת מתוך lifespan ב-main.py."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge USING fts5("
            "title, content, added_at UNINDEXED)"
        )

        # מיגרציה עדינה: בסיס נתונים שנוצר לפני שהעמודות האלה קיימות
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        for column in ("linked_event", "linked_date"):
            if column not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")


# ---------------------------------------------------------------- משימות

def add_task(chat_id: int, title: str, notes: str = None, soft_due: str = None,
             linked_event: str = None, linked_date: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (chat_id, title, notes, soft_due, created_at, "
            "linked_event, linked_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, title, notes, soft_due, _now(), linked_event, linked_date),
        )
        return cur.lastrowid


def tasks_for_date(chat_id: int, date_str: str) -> list[dict]:
    """משימות הכנה שקשורות לאירוע בתאריך מסוים. עולות בבריף של אותו בוקר."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'open' "
            "AND linked_date = ? ORDER BY id",
            (chat_id, date_str),
        ).fetchall()
        return [dict(r) for r in rows]


def list_open_tasks(chat_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'open' "
            "ORDER BY soft_due IS NULL, soft_due, created_at",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def complete_task(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? "
            "WHERE id = ? AND status = 'open'",
            (_now(), task_id),
        )
        return cur.rowcount > 0


def get_stuck_tasks(chat_id: int) -> list[dict]:
    """
    משימות פתוחות שראויות לנדנוד:
    ותיקות מ-STUCK_AFTER_DAYS, ולא נודנדו ב-NUDGE_COOLDOWN_DAYS האחרונים.
    זה מה שמונע נדנוד יומי על אותה משימה.
    """
    now = datetime.now(timezone.utc)
    stuck_before = (now - timedelta(days=STUCK_AFTER_DAYS)).isoformat()
    cooldown_before = (now - timedelta(days=NUDGE_COOLDOWN_DAYS)).isoformat()

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'open' "
            "AND created_at < ? "
            "AND (last_nudged_at IS NULL OR last_nudged_at < ?) "
            "ORDER BY created_at",
            (chat_id, stuck_before, cooldown_before),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_nudged(task_ids: list[int]) -> None:
    if not task_ids:
        return
    placeholders = ",".join("?" * len(task_ids))
    with get_conn() as conn:
        conn.execute(
            f"UPDATE tasks SET last_nudged_at = ?, nudge_count = nudge_count + 1 "
            f"WHERE id IN ({placeholders})",
            (_now(), *task_ids),
        )


# ------------------------------------------------------- פעולות ממתינות

def create_pending(chat_id: int, action_type: str, payload: dict, summary: str) -> int:
    """
    יוצר הצעה שממתינה לאישור.

    בעבר הצעה חדשה ביטלה את הקודמת, כדי ש"כן" בטקסט לא יהיה דו-משמעי.
    זה חסם בקשות כמו "תקבע גם רופא וגם ישיבה". עכשיו כמה הצעות יכולות
    להמתין במקביל — כל אחת עם הכפתורים שלה — ואישור בטקסט מטופל רק
    כשיש בדיוק אחת פתוחה.
    """
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO pending_actions "
            "(chat_id, action_type, payload, summary, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                action_type,
                json.dumps(payload, ensure_ascii=False),
                summary,
                now.isoformat(),
                (now + timedelta(minutes=PENDING_TTL_MINUTES)).isoformat(),
            ),
        )
        return cur.lastrowid


def get_pending(chat_id: int) -> Optional[dict]:
    """מחזיר את ההצעה הפתוחה, או None אם אין או שפגה."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE chat_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()

        if row is None:
            return None

        if row["expires_at"] < _now():
            conn.execute(
                "UPDATE pending_actions SET status = 'expired', resolved_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            return None

        action = dict(row)
        action["payload"] = json.loads(action["payload"])
        return action


def list_pending(chat_id: int) -> list[dict]:
    """כל ההצעות שעדיין ממתינות, מהישנה לחדשה. פגות מסומנות תוך כדי."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_actions WHERE chat_id = ? AND status = 'pending' "
            "ORDER BY id",
            (chat_id,),
        ).fetchall()

        live = []
        now = _now()
        for row in rows:
            if row["expires_at"] < now:
                conn.execute(
                    "UPDATE pending_actions SET status = 'expired', resolved_at = ? "
                    "WHERE id = ?", (now, row["id"]),
                )
                continue
            action = dict(row)
            action["payload"] = json.loads(action["payload"])
            live.append(action)
        return live


def get_pending_by_id(action_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = ? AND status = 'pending'",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < _now():
            conn.execute(
                "UPDATE pending_actions SET status = 'expired', resolved_at = ? "
                "WHERE id = ?", (_now(), row["id"]),
            )
            return None
        action = dict(row)
        action["payload"] = json.loads(action["payload"])
        return action


def resolve_pending(action_id: int, status: str) -> None:
    """status: confirmed | rejected"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ?",
            (status, _now(), action_id),
        )


# ------------------------------------------------------------ היסטוריה

def add_message(chat_id: int, role: str, content: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, _now()),
        )


def get_history(chat_id: int, limit: int = 10) -> list[dict]:
    """מחזיר בסדר כרונולוגי, מהישן לחדש — כפי שה-LLM מצפה."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ------------------------------------------------------ תיעוד מה שקרה

def log_activity(chat_id: int, log_date: str, content: str,
                 source: str = "evening_review") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO activity_log (chat_id, log_date, content, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, log_date, content, source, _now()),
        )
        return cur.lastrowid


def get_activity(chat_id: int, since_date: str) -> list[dict]:
    """since_date בפורמט YYYY-MM-DD. בסיס לסיכום שבועי."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log WHERE chat_id = ? AND log_date >= ? "
            "ORDER BY log_date",
            (chat_id, since_date),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------- מטמון זמני נסיעה

TRAVEL_CACHE_DAYS = 90


def get_travel_minutes(origin: str, destination: str,
                       mode: str = "driving") -> Optional[int]:
    """מחזיר None אם אין במטמון או שהערך ישן — אז צריך לקרוא ל-API."""
    stale_before = (datetime.now(timezone.utc)
                    - timedelta(days=TRAVEL_CACHE_DAYS)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT minutes FROM travel_cache WHERE origin = ? AND destination = ? "
            "AND mode = ? AND cached_at > ?",
            (origin.strip().lower(), destination.strip().lower(), mode, stale_before),
        ).fetchone()
        return row["minutes"] if row else None


def set_travel_minutes(origin: str, destination: str, minutes: int,
                       mode: str = "driving") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO travel_cache (origin, destination, minutes, mode, cached_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(origin, destination, mode) "
            "DO UPDATE SET minutes = excluded.minutes, cached_at = excluded.cached_at",
            (origin.strip().lower(), destination.strip().lower(), minutes, mode, _now()),
        )


def get_geocode(address: str) -> Optional[tuple[float, float]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT lon, lat FROM geocode_cache WHERE address = ?",
            (address.strip().lower(),),
        ).fetchone()
        return (row["lon"], row["lat"]) if row else None


def set_geocode(address: str, lon: float, lat: float, label: str = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO geocode_cache (address, lon, lat, label, cached_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "lon = excluded.lon, lat = excluded.lat, cached_at = excluded.cached_at",
            (address.strip().lower(), lon, lat, label, _now()),
        )


def get_last_action(chat_id: int, action_types: tuple = None) -> Optional[dict]:
    """
    הפעולה האחרונה שכבר טופלה — לשחזור אחרי ביטול.
    ה-payload נשמר בטבלה ממילא, אז אין צורך לשאול שוב לפרטים.
    """
    sql = ("SELECT * FROM pending_actions WHERE chat_id = ? "
           "AND status IN ('confirmed', 'rejected')")
    params = [chat_id]
    if action_types:
        sql += f" AND action_type IN ({','.join('?' * len(action_types))})"
        params.extend(action_types)
    sql += " ORDER BY id DESC LIMIT 1"

    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        action = dict(row)
        action["payload"] = json.loads(action["payload"])
        return action


def get_action_by_id(action_id: int) -> Optional[dict]:
    """שליפת הצעה לפי המזהה שבכפתור. מסמן פגות תוקף בדרך."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            return None
        action = dict(row)
        if action["status"] == "pending" and action["expires_at"] < _now():
            conn.execute(
                "UPDATE pending_actions SET status = 'expired', resolved_at = ? WHERE id = ?",
                (_now(), action_id),
            )
            action["status"] = "expired"
        action["payload"] = json.loads(action["payload"])
        return action


# ------------------------------------------------ העדפות קבועות

def set_preference(chat_id: int, key: str, value: str) -> None:
    """שמירת העדפה קבועה — "אימון תמיד שעה וחצי" וכדומה."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO preferences (chat_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (chat_id, key.strip(), value.strip(), _now()),
        )


def delete_preference(chat_id: int, key: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM preferences WHERE chat_id = ? AND key = ?",
            (chat_id, key.strip()),
        )
        return cur.rowcount > 0


def get_preferences(chat_id: int) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM preferences WHERE chat_id = ? ORDER BY key",
            (chat_id,),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}


# ------------------------------------------------ בסיס ידע (FTS5)

def add_document(title: str, content: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge (title, content, added_at) VALUES (?, ?, ?)",
            (title.strip(), content, _now()),
        )


def search_documents(query: str, limit: int = 3) -> list[dict]:
    """חיפוש FTS5. מחזיר קטעים רלוונטיים עם שם המסמך."""
    # OR בין המילים: שאלה טבעית ("מתי נגמר הביטוח") מכילה מילים שלא
    # מופיעות במסמך, וברירת המחדל של FTS5 היא AND — שמחזיר כלום.
    words = [w for w in query.replace('"', " ").split() if len(w) > 1]
    if not words:
        return []
    sanitized = " OR ".join(words)
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT title, snippet(knowledge, 1, '', '', '...', 40) AS part "
                "FROM knowledge WHERE knowledge MATCH ? ORDER BY rank LIMIT ?",
                (sanitized, limit),
            ).fetchall()
        except Exception:
            return []
        return [{"title": r["title"], "snippet": r["part"]} for r in rows]


def list_documents() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rowid AS id, title, added_at FROM knowledge ORDER BY rowid DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------- הוראות מתוזמנות

def add_scheduled_prompt(chat_id: int, day_of_week: str, hour: int,
                         minute: int, prompt: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_prompts "
            "(chat_id, day_of_week, hour, minute, prompt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, day_of_week, hour, minute, prompt, _now()),
        )
        return cur.lastrowid


def list_scheduled_prompts(chat_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_prompts WHERE chat_id = ? AND active = 1 "
            "ORDER BY id", (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def deactivate_scheduled_prompt(schedule_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE scheduled_prompts SET active = 0 WHERE id = ? AND active = 1",
            (schedule_id,),
        )
        return cur.rowcount > 0


# ------------------------------------------------------------- הרגלים

def add_habit(chat_id: int, name: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO habits (chat_id, name) VALUES (?, ?)", (chat_id, name))
        return cur.lastrowid


def remove_habit(chat_id: int, name: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE habits SET active = 0 WHERE chat_id = ? AND name = ? AND active = 1",
            (chat_id, name))
        return cur.rowcount > 0


def list_habits(chat_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM habits WHERE chat_id = ? AND active = 1 ORDER BY id",
            (chat_id,)).fetchall()
        return [dict(r) for r in rows]


def find_habit(chat_id: int, name: str) -> Optional[dict]:
    """התאמה רכה: 'אימון' יתפוס גם 'אימון כושר'."""
    name = name.strip()
    for h in list_habits(chat_id):
        if h["name"] == name or name in h["name"] or h["name"] in name:
            return h
    return None


def log_habit(habit_id: int, log_date: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO habit_log (habit_id, log_date) VALUES (?, ?)",
            (habit_id, log_date))


def habit_dates(habit_id: int, limit: int = 120) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT log_date FROM habit_log WHERE habit_id = ? "
            "ORDER BY log_date DESC LIMIT ?", (habit_id, limit)).fetchall()
        return [r["log_date"] for r in rows]


# ------------------------------------------------------------- תזונה

def add_meal(chat_id: int, meal_date: str, meal_time: str,
             description: str, kcal: int = None, protein_g: int = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meals (chat_id, meal_date, meal_time, description, kcal, protein_g) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, meal_date, meal_time, description, kcal, protein_g))


def meals_for_date(chat_id: int, meal_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE chat_id = ? AND meal_date = ? ORDER BY meal_time",
            (chat_id, meal_date)).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------- תיק השקעות

def save_portfolio_snapshot(snap_date: str, nav: float, cash: float,
                            positions_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO portfolio_snapshots (snap_date, nav, cash, positions) "
            "VALUES (?, ?, ?, ?)", (snap_date, nav, cash, positions_json))


def portfolio_history(limit: int = 30) -> list[dict]:
    """מהחדש לישן."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snap_date DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------- רשימת קניות

def shopping_add(chat_id: int, item: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO shopping (chat_id, item) VALUES (?, ?)",
                     (chat_id, item))


def shopping_remove(chat_id: int, item: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM shopping WHERE chat_id = ? AND id = ("
            "SELECT id FROM shopping WHERE chat_id = ? AND item LIKE ? LIMIT 1)",
            (chat_id, chat_id, f"%{item}%"))
        return cur.rowcount > 0


def shopping_list(chat_id: int) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item FROM shopping WHERE chat_id = ? ORDER BY id",
            (chat_id,)).fetchall()
        return [r["item"] for r in rows]


def shopping_clear(chat_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM shopping WHERE chat_id = ?", (chat_id,))
        return cur.rowcount


# ------------------------------------------------------------- טריאז' מייל

def email_mark_seen(message_id: str) -> bool:
    """True אם המייל חדש (טרם נסרק)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO email_seen (message_id) VALUES (?)",
            (message_id,))
        return cur.rowcount > 0
