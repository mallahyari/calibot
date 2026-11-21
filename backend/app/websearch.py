"""
חיפוש ברשת — דרך DuckDuckGo (חבילת ddgs), בלי מפתח ובלי הרשמה.
הספרייה סינכרונית, אז רצים ב-thread כדי לא לחסום את לולאת האירועים.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

MAX_RESULTS = 5


def _search_sync(query: str) -> list[dict]:
    from ddgs import DDGS
    with DDGS() as ddgs:
        return [
            {"title": r.get("title", ""),
             "snippet": r.get("body", "")[:300],
             "url": r.get("href", "")}
            for r in ddgs.text(query, region="il-he", max_results=MAX_RESULTS)
        ]


async def search(query: str) -> list[dict]:
    try:
        return await asyncio.to_thread(_search_sync, query)
    except ImportError:
        logger.error("ddgs לא מותקן — אין חיפוש ברשת")
        return [{"title": "שגיאה", "snippet": "חיפוש ברשת לא מותקן בשרת.", "url": ""}]
    except Exception as exc:
        logger.error("חיפוש ברשת נכשל: %s", exc)
        return []
