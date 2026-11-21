"""
מזג אוויר לבריף הבוקר. Open-Meteo — חינמי, בלי מפתח.
לא קישוט: גשם משנה זמן נסיעה ומה לובשים לפגישה.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# קרית אתא
LAT = float(os.getenv("WEATHER_LAT", "32.81"))
LON = float(os.getenv("WEATHER_LON", "35.11"))

_CODES = {
    0: "בהיר", 1: "בהיר ברובו", 2: "מעונן חלקית", 3: "מעונן",
    45: "ערפילי", 48: "ערפילי",
    51: "טפטוף", 53: "טפטוף", 55: "טפטוף",
    61: "גשם קל", 63: "גשם", 65: "גשם כבד",
    80: "ממטרים", 81: "ממטרים", 82: "ממטרים עזים",
    95: "סופת רעמים", 96: "סופת רעמים", 99: "סופת רעמים",
}


async def today_line() -> str:
    """שורת מזג אוויר לבריף, או מחרוזת ריקה בכישלון — לא חוסמים בריף."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": LAT, "longitude": LON,
                    "daily": "temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max,weathercode",
                    "timezone": "Asia/Jerusalem", "forecast_days": 1,
                },
            )
            resp.raise_for_status()
            d = resp.json()["daily"]

        desc = _CODES.get(d["weathercode"][0], "")
        line = (f"🌡️ {round(d['temperature_2m_min'][0])}–"
                f"{round(d['temperature_2m_max'][0])}°")
        if desc:
            line += f", {desc}"
        rain = d.get("precipitation_probability_max", [0])[0]
        if rain and rain >= 30:
            line += f", {rain}% סיכוי לגשם — קח מטרייה"
        return line
    except Exception as exc:
        logger.warning("מזג אוויר נכשל: %s", exc)
        return ""
