import httpx
from app.config import TELEGRAM_API_TOKEN

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}"

def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2"""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

async def send_telegram_message(chat_id: int, text: str, parse_mode: str = None):
        """
        שליחת הודעה. ברירת המחדל היא טקסט רגיל ולא MarkdownV2 —
        טקסט עברי עם נקודות, מקפים וסוגריים נדחה על ידי טלגרם
        אם לא בורחים מכל תו מיוחד.
        """
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TELEGRAM_API_BASE}/sendMessage", json=payload
            )
            return response.json()


class TelegramBotService:
    def start(self):
        print("Telegram bot started...")  # For debugging

    def stop(self):
        print("Telegram bot stopped...")  # For debugging
        
    
    

        
        

        
        



async def send_telegram_voice(chat_id: int, audio: bytes, kind: str = "voice",
                              caption: str = None):
    """
    שליחת שמע: kind='voice' (ogg/opus, מוצג כהודעה קולית) או
    'audio' (mp3, מוצג כנגן). ה-caption מצרף את הטקסט המלא —
    כך אפשר גם להאזין וגם לקרוא.
    """
    method = "sendVoice" if kind == "voice" else "sendAudio"
    field = "voice" if kind == "voice" else "audio"
    ext = "ogg" if kind == "voice" else "mp3"

    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1024]

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{TELEGRAM_API_BASE}/{method}",
            data=data,
            files={field: (f"nikol.{ext}", audio,
                           "audio/ogg" if kind == "voice" else "audio/mpeg")},
        )
        return response.json()
