import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o"
GOOGLE_CLIENT_SECRET_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")
# הוספת scope מחייבת אימות מחדש (מחיקת token.pickle ו-/start)
GOOGLE_API_SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
]
OAUTH_REDIRECT_PATH = "/oauth2callback"

OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "0")

API_HOST = "localhost"
API_PORT = 8060