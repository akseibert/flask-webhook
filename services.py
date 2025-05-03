import os
import requests
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
import logging

logger = logging.getLogger(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
TELEGRAM_API = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id, text):
    """Send a message via Telegram."""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    response = requests.post(url, json=payload)
    response.raise_for_status()
    logger.info({"event": "telegram_message_sent", "chat_id": chat_id, "text": text})
    return response.json()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    """Get file path from Telegram file ID."""
    url = f"{TELEGRAM_API}/getFile?file_id={file_id}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()["result"]["file_path"]

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    """Transcribe voice message from Telegram."""
    try:
        file_path = get_telegram_file_path(file_id)
        file_url = f"https://api.telegram.org/file/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/{file_path}"
        response = requests.get(file_url)
        response.raise_for_status()
        
        with open("temp_audio.ogg", "wb") as f:
            f.write(response.content)
        
        with open("temp_audio.ogg", "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        
        os.remove("temp_audio.ogg")
        # Clean transcription
        command_words = ["add", "delete", "remove", "correct", "update", "as", "issue", "tool", "activity", "people", "company", "service", "weather", "time", "comments", "category"]
        text = transcription.strip()
        for word in command_words:
            if text.lower().startswith(word + " "):
                text = text[len(word) + 1:].strip()
                break
        text = re.sub(r'^s+\s+', '', text, flags=re.IGNORECASE).strip()
        logger.info({"event": "voice_transcribed", "text": text})
        return text
    except Exception as e:
        logger.error({"event": "transcription_error", "error": str(e)})
        return ""
