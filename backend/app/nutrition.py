"""
יומן תזונה — "אכלתי שניצל ואורז" בצ'אט, המודל מעריך קלוריות וחלבון,
וניקול רושמת ומסכמת. הערכות ולא מעבדה — וזה בסדר: המטרה מודעות, לא דיוק.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app import db

TZ = ZoneInfo("Asia/Jerusalem")


def log(chat_id: int, description: str, kcal: int = None,
        protein_g: int = None) -> str:
    now = datetime.now(TZ)
    db.add_meal(chat_id, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
                description.strip(), kcal, protein_g)
    total_k, total_p = totals(chat_id, now.strftime("%Y-%m-%d"))
    est = f" (~{kcal} קק\"ל)" if kcal else ""
    return f"נרשם: {description.strip()}{est}. סה\"כ היום: ~{total_k} קק\"ל, ~{total_p}g חלבון."


def totals(chat_id: int, meal_date: str) -> tuple[int, int]:
    meals = db.meals_for_date(chat_id, meal_date)
    kcal = sum(m["kcal"] or 0 for m in meals)
    protein = sum(m["protein_g"] or 0 for m in meals)
    return kcal, protein


def day_summary(chat_id: int, meal_date: str = None) -> str:
    """סיכום יום לתשובה לשאלה או לסיכום הערב. ריק אם לא נרשם כלום."""
    d = meal_date or datetime.now(TZ).strftime("%Y-%m-%d")
    meals = db.meals_for_date(chat_id, d)
    if not meals:
        return ""
    kcal, protein = totals(chat_id, d)
    lines = [f"🍽 היום: ~{kcal} קק\"ל, ~{protein}g חלבון"]
    for m in meals:
        est = f" — {m['kcal']} קק\"ל" if m["kcal"] else ""
        lines.append(f"  {m['meal_time']}  {m['description']}{est}")
    return "\n".join(lines)
