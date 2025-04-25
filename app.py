from flask import Flask, request
import requests
import os
import json
import logging
from datetime import datetime
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

# --- Initialize logging ---
logging.basicConfig(
    filename="/opt/render/project/src/app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())  # also print to console

# --- Initialize OpenAI client ---
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    logger.info("OpenAI client initialized")
except Exception as e:
    logger.error(f"OpenAI init failed: {e}")
    raise

app = Flask(__name__)

# --- Session persistence ---
SESSION_FILE = "/opt/render/project/src/session_data.json"
def load_session_data():
    try:
        if os.path.exists(SESSION_FILE):
            return json.load(open(SESSION_FILE))
    except Exception as e:
        logger.error(f"load_session_data error: {e}")
    return {}
def save_session_data(data):
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        json.dump(data, open(SESSION_FILE, "w"))
    except Exception as e:
        logger.error(f"save_session_data error: {e}")

session_data = load_session_data()

def blank_report():
    today = datetime.now().strftime("%d-%m-%Y")
    return {
        "site_name": "", "segment": "", "category": "",
        "company": [], "people": [], "tools": [], "service": [],
        "activities": [], "issues": [],
        "time": "", "weather": "", "impression": "",
        "comments": "", "date": today
    }

# --- Telegram helpers ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    })
    resp.raise_for_status()
    logger.info(f"→ Telegram {chat_id}: {text[:50]}…")
    return resp

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    resp = requests.get(url)
    resp.raise_for_status()
    fp = resp.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{fp}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        logger.info(f"Fetching audio: {audio_url}")
        r = requests.get(audio_url); r.raise_for_status()
        audio_bytes = r.content

        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio_bytes, "audio/ogg")
        )
        text = getattr(result, "text", "").strip()
        if not text:
            logger.warning(f"Empty transcription, full result: {result}")
            return ""
        logger.info(f"Transcribed: {text}")
        return text

    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return ""

# --- Data-extraction & formatting (unchanged) ---
def enrich_with_date(d):
    today = datetime.now().strftime("%d-%m-%Y")
    # … your existing enrich logic …
    if not d.get("date"):
        d["date"] = today
    return d

def summarize_data(d):
    # … your existing summary logic …
    lines = [
        f"🏗️ **Site**: {d.get('site_name','')}",
        # …
        f"📆 **Date**: {d.get('date','')}",
    ]
    return "\n".join(lines)

# (extract_site_report, merge_structured_data, apply_correction,
#  delete_from_report—all as you already have them, unchanged)

# --- HTTP routes ---
@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        msg  = data.get("message", {})
        chat_id = str(msg.get("chat",{}).get("id", ""))

        # pull either text, voice or audio-file
        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        elif "audio" in msg:
            text = transcribe_from_telegram_voice(msg["audio"]["file_id"])
        else:
            text = (msg.get("text") or "").strip()

        logger.info(f"Incoming [{chat_id}]: {text[:50]}…")

        # … then the rest of your handler (reset,
