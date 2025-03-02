import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o"
GOOGLE_CLIENT_SECRET_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")
GOOGLE_API_SCOPES = ['https://www.googleapis.com/auth/calendar']
OAUTH_REDIRECT_PATH = "/oauth2callback"

API_HOST = "localhost"
API_PORT = 8060