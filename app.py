from flask import Flask, request
import requests
import os
import json
import logging
import threading
import httpx
from datetime import datetime
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

# Initialize logging
logging.basicConfig(
    filename="app.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
try:
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        http_client=httpx.Client(proxies=None)  # Disable proxies
    )
    logger.info("OpenAI client initialized successfully")
except Exception as e:
    logger.error(f"OpenAI client initialization failed: {e}")
    raise

app = Flask(__name__)

# Session data persistence
SESSION_FILE = "/opt/render/project/src/session_data.json"
session_lock = threading.Lock()

def load_session_data():
    with session_lock:
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, "r") as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Failed to load session data: {e}")
            return {}

def save_session_data(data):
    with session_lock:
        try:
            os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
            with open(SESSION_FILE, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.error(f"Failed to save session data: {e}")

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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": text})
    response.raise_for_status()
    logger.info(f"Sent Telegram message to {chat_id}")
    return response

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    response = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    response.raise_for_status()
    file_path = response.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        audio = requests.get(audio_url).content
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio, "audio/ogg")
        )
        text = response.text
        logger.info(f"Transcribed audio: {text}")
        return text
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return ""

def enrich_with_date(d):
    today = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today
    else:
        try:
            input_date = datetime.strptime(d["date"], "%d-%m-%Y")
            if input_date > datetime.now():
                d["date"] = today
        except ValueError:
            d["date"] = today
    return d

def summarize_data(d):
    lines = []
    lines.append(f"  Site: {d.get('site_name', 'Not provided')}")
    lines.append(f"  Segment: {d.get('segment', '')}")
    lines.append(f"  Category: {d.get('category', '')}")
    lines.append(
        "  Companies: " +
        ", ".join(c.get("name", "") if isinstance(c, dict) else str(c)
                  for c in d.get("company", []))
    )
    lines.append(
        "  People: " +
        ", ".join(
            f"{p.get('name', '')} ({p.get('role', '')})" if isinstance(p, dict) else str(p)
            for p in d.get("people", [])
        )
    )
    lines.append(
        "  Services: " +
        ", ".join(
            f"{s.get('task', '')} ({s.get('company', '')})" if isinstance(s, dict) else str(s)
            for s in d.get("service", [])
        )
    )
    lines.append(
        "  Tools: " +
        ", ".join(
            f"{t.get('item', '')} ({t.get('company', '')})" if isinstance(t, dict) else str(t)
            for t in d.get("tools", [])
        )
    )
    lines.append("  Activities: " + ", ".join(d.get("activities", [])))
    lines.append("  Issues:")
    valid_issues = [
        i for i in d.get("issues", [])
        if isinstance(i, dict) and i.get("description", "").strip()
    ]
    if valid_issues:
        for i in valid_issues:
            desc = i["description"]
            by = i.get("caused_by", "")
            photo = " (with photo)" if i.get("has_photo") else ""
            extra = f" (by {by})" if by else ""
            lines.append(f"    • {desc}{extra}{photo}")
    else:
        lines.append("    None reported")
    lines.append(f"  Time: {d.get('time', '')}")
    lines.append(f"  Weather: {d.get('weather', '')}")
    lines.append(f"  Impression: {d.get('impression', '')}")
    lines.append(f"  Comments: {d.get('comments', '')}")
    lines.append(f"  Date: {d.get('date', '')}")
    return "\n".join(lines)

gpt_prompt = """
You are an AI assistant extracting a construction site report. Extract only what’s explicitly mentioned in the text. Return JSON with any of these fields (omit if not present):
- site_name: string
- segment: string
- category: string
- company: list of {"name": string}
- people: list of {"name": string, "role": string}
- tools: list of {"item": string, "company": string}
- service: list of {"task": string, "company": string}
- activities: list of strings
- issues: list of {"description": string, "caused_by": string (optional), "has_photo": boolean (optional, default false)}
- time: string
- weather: string
- impression: string
- comments: string
- date: string (dd-mm-yyyy)
For issues, always include the description; caused_by and has_photo are optional. If issues are mentioned, ensure they are formatted as a list of objects, even if only the description is provided.
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    messages = [
        {"role": "system", "content": "Extract only explicitly stated fields; never infer or guess."},
        {"role": "user", "content": gpt_prompt + "\n" + text}
    ]
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        data = json.loads(response.choices[0].message.content)
        logger.info(f"Extracted report: {data}")
        return data
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing error in GPT response: {e}")
        return {}
    except Exception as e:
        logger.error(f"GPT extract error: {e}")
        return {}

def merge_structured_data(existing, new):
    merged = existing.copy()
    for key, value in new.items():
        if key in ["company", "people", "tools", "service", "activities", "issues"]:
            existing_list = merged.get(key, [])
            new_items = value if isinstance(value, list) else []
            for item in new_items:
                if key == "issues":
                    if not any(existing_item.get("description") == item.get("description")
                              for existing_item in existing_list):
                        existing_list.append(item)
                elif item not in existing_list:
                    existing_list.append(item)
            merged[key] = existing_list
        else:
            if value:
                merged[key] = value
    return merged

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def handle_update(update):
    # your implementation here
    pass
