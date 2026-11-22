"""
טעינת .env מוקדם ככל האפשר.

הסיבה: מודולים כמו policy ו-travel קוראים os.getenv בזמן הייבוא עצמו.
אם dotenv נטען רק כשמגיעים ל-app.config, חלק מהמודולים כבר נטענו עם
ערכים ריקים. __init__.py של החבילה רץ לפני כל תת-מודול, ולכן זה המקום.
"""

from dotenv import load_dotenv

load_dotenv()
