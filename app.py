from flask import Flask, request
import requests
import os
import json
import re
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from difflib import SequenceMatcher
from contextlib import contextmanager
from typing import Dict, List, Any, Callable, Sequence
from time import time

# --- Configuration ---
@dataclass(frozen=True)
class Settings:
    openai_key: str = os.getenv("OPENAI_API_KEY", "")
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    port: int = int(os.getenv("PORT", "5000"))
    session_file: str = "/opt/render/project/src/session_data.json"
    retry: Callable = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=10)
    )
    app_version: str = "8d7f9c3a-1b2c-4d5e-9f8a-3c6b0e1a2f4d"  # Version for logging
    rate_limit_seconds: float = 1.0  # Minimum time between processing same input

settings = Settings()
if not settings.openai_key or not settings.telegram_token:
    raise RuntimeError("OPENAI_API_KEY and TELEGRAM_BOT_TOKEN must be set")

# --- Initialize logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/render/project/src/app.log")
    ]
)
logger = logging.getLogger("site-bot")
logger.info(f"Starting application version: {settings.app_version}")

# --- Initialize OpenAI client ---
client = OpenAI(api_key=settings.openai_key)
app = Flask(__name__)

# --- Session data persistence ---
def load_session_data() -> Dict[str, Any]:
    try:
        if os.path.exists(settings.session_file):
            with open(settings.session_file) as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(settings.session_file), exist_ok=True)
        with open(settings.session_file, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed to save session data: {e}")

@contextmanager
def session_manager():
    data = load_session_data()
    yield data
    save_session_data(data)

# --- Field configuration ---
FIELD_CONFIG = {
    "site_name": {"scalar": True, "icon": "🏗️"},
    "segment": {"scalar": True, "icon": "🛠️"},
    "category": {"scalar": True, "icon": "📋"},
    "time": {"scalar": True, "icon": "⏰"},
    "weather": {"scalar": True, "icon": "🌦️"},
    "impression": {"scalar": True, "icon": "😊"},
    "comments": {"scalar": True, "icon": "💬"},
    "date": {"scalar": True, "icon": "📆"},
    "company": {"key": "name", "format": lambda x: x.get("name", ""), "icon": "🏢"},
    "people": {"key": "name", "format": lambda x: f"{x.get('name', '')} ({x.get('role', '')})", "icon": "👷"},
    "service": {"key": "task", "format": lambda x: f"{x.get('task', '')} ({x.get('company', '') or 'None'})", "icon": "🔧"},
    "tools": {"key": "item", "format": lambda x: f"{x.get('item', '')} ({x.get('company', '') or 'None'})", "icon": "🛠️"},
    "activities": {"key": None, "format": lambda x: x, "icon": "📅"},
    "issues": {
        "key": "description",
        "format": lambda x: f"  • {x.get('description', '')}{' (by ' + x.get('caused_by', '') + ')' if x.get('caused_by') else ''}{' 📸' if x.get('has_photo') else ''}",
        "icon": "⚠️"
    }
}

# --- Telegram API utilities ---
@settings.retry
def send_telegram_message(chat_id: str, text: str) -> None:
    """Send a message to Telegram with robust error handling."""
    try:
        if not chat_id or not isinstance(chat_id, str):
            raise ValueError(f"Invalid chat_id: {chat_id}")
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        response = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
            json=payload
        )
        response.raise_for_status()
        response_data = response.json()
        if not response_data.get("ok", False):
            error_desc = response_data.get("description", "Unknown error")
            raise RuntimeError(f"Telegram API error: {error_desc}")
        logger.info(f"Sent message to chat_id={chat_id}: {text[:50]}...")
    except Exception as e:
        logger.error(f"Failed to send message: {e}, payload: {json.dumps(payload)}, response: {response.text if 'response' in locals() else 'N/A'}")
        raise

@settings.retry
def transcribe_voice(file_id: str) -> str:
    """Transcribe voice message using OpenAI Whisper."""
    try:
        response = requests.get(f"https://api.telegram.org/bot{settings.telegram_token}/getFile?file_id={file_id}")
        response.raise_for_status()
        response_data = response.json()
        file_path = response_data.get("result", {}).get("file_path", "")
        if not file_path:
            raise ValueError(f"No file_path in response: {response_data}")
        audio_response = requests.get(f"https://api.telegram.org/file/bot{settings.telegram_token}/{file_path}")
        audio_response.raise_for_status()
        if not audio_response.content:
            raise ValueError("Empty audio content")
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio_response.content, "audio/ogg")
        )
        text = getattr(response, "text", "") or (response.get("text", "") if isinstance(response, dict) else "")
        logger.info(f"Transcription response: {text}")
        if not text.strip():
            logger.warning(f"Empty transcription result: {text}")
        return text.strip()
    except Exception as e:
        logger.error(f"Transcription failed: {e}\n{traceback.format_exc()}")
        return ""

# --- Data processing ---
def blank_report() -> Dict[str, Any]:
    """Create a blank report structure."""
    return {
        "site_name": "", "segment": "", "category": "", "company": [], "people": [],
        "tools": [], "service": [], "activities": [], "issues": [], "time": "",
        "weather": "", "impression": "", "comments": "", "date": datetime.now().strftime("%d-%m-%Y")
    }

def string_similarity(a: str, b: str) -> float:
    """Calculate string similarity using SequenceMatcher."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def merge_list_field(existing: List[Dict], new_items: List[Dict], key_fn: Callable[[Dict], str], threshold: float = 0.6) -> List[Dict]:
    """Merge or update items in a list field, deduplicating based on key function."""
    existing_list = existing.copy()
    idx = {key_fn(item): item for item in existing_list if key_fn(item)}
    for item in new_items:
        if not isinstance(item, dict) or not key_fn(item):
            continue
        k = key_fn(item)
        if k and any(string_similarity(k, existing_k) > threshold for existing_k in idx):
            idx[k] = item
            logger.info(f"Replaced item with key: {k}")
        elif item not in existing_list:
            existing_list.append(item)
            logger.info(f"Added new item: {k}")
    return list(idx.values())

def merge_structured_data(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Merge new data into existing session data."""
    merged = existing.copy()
    key_fns = {
        "company": lambda x: x.get("name", ""),
        "people": lambda x: f"{x.get('name', '')}{x.get('role', '')}",
        "tools": lambda x: f"{x.get('item', '')}{x.get('company', '')}",
        "service": lambda x: f"{x.get('task', '')}{x.get('company', '')}",
        "issues": lambda x: x.get("description", "")
    }
    for key, value in new.items():
        if key in key_fns and isinstance(value, list):
            merged[key] = merge_list_field(merged.get(key, []), value, key_fns[key])
        elif value:
            merged[key] = value
    return merged

def _comma(items: Sequence[str]) -> str:
    """Join items with commas, return 'None' if empty."""
    return ", ".join(items) if items else "None"

def summarize_data(data: Dict[str, Any]) -> str:
    """Generate a formatted summary of the report data."""
    try:
        if not isinstance(data, dict):
            raise ValueError(f"Invalid data type for summary: {type(data)}")
        logger.info(f"Summarizing data: {json.dumps(data, indent=2)}")
        lines = []
        for field, config in FIELD_CONFIG.items():
            if not isinstance(config, dict):
                logger.error(f"Invalid config for field {field}: {config}")
                continue
            if field == "issues":
                lines.append(f"{config.get('icon', '⚠️')} **Issues**:")
                issues = [i for i in data.get(field, []) if isinstance(i, dict) and i.get("description", "").strip()]
                lines.extend(config["format"](i) for i in issues if isinstance(i, dict)) if issues else lines.append("  None")
            elif config.get("scalar"):
                lines.append(f"{config.get('icon', '📅')} **{field.title().replace('_', ' ')}**: {data.get(field, '') or 'None'}")
            else:
                items = data.get(field, [])
                if not isinstance(items, list):
                    logger.warning(f"Invalid items for field {field}: {items}")
                    items = []
                value = _comma(config["format"](item) for item in items if isinstance(item, dict))
                lines.append(f"{config.get('icon', '📅')} **{field.title()}**: {value}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Summarize error: {e}\n{traceback.format_exc()}")
        return "Error generating summary"

@settings.retry
def extract_site_report(text: str) -> Dict[str, Any]:
    """Extract report fields from text using GPT or regex fallback."""
    logger.info(f"Processing input text for extraction: '{text}'")
    person_match = re.match(r'^(?:add\s+)?(\w+\s*\w*)\s+as\s+(\w+\s*\w*)$', text, re.IGNORECASE)
    if person_match:
        name, role = person_match.groups()
        return {"people": [{"name": name.strip(), "role": role.strip()}]}

    # Use f-string to safely format the prompt
    prompt = f"""
Extract construction site report fields from input text into JSON. Only include explicitly mentioned fields. Use keywords like "Site:", "Company:", or natural language intent. For segment/category, extract value only (e.g., "Category: 3" -> "category": "3"). For issues, recognize "Issue", "Problem", etc., with optional "caused_by" and "has_photo". For activities, detect "Work", "Task", etc. For people, recognize "add [name] as [role]" or "Person: [name], role: [role]". Fallback to comments for unclear inputs. Case-insensitive.

Fields:
- site_name: string
- segment: string (no "Segment" prefix)
- category: string (no "Category" prefix)
- company: list of {{"name": string}}
- people: list of {{"name": string, "role": string}}
- tools: list of {{"item": string, "company": string}}
- service: list of {{"task": string, "company": string}}
- activities: list of strings
- issues: list of {{"description": string, "caused_by": string (optional), "has_photo": bool (default false)}}
- time: string
- weather: string
- impression: string
- comments: string
- date: string (dd-mm-yyyy)

Examples:
- "Site: Downtown, Issue: Delay with photo" -> {{"site_name": "Downtown", "issues": [{{"description": "Delay", "has_photo": true}}]}}
- "Category: 3, Segment: 5" -> {{"category": "3", "segment": "5"}}
- "Work at ABC" -> {{"site_name": "ABC", "activities": ["Work"]}}
- "add Anna as Supervisor" -> {{"people": [{{"name": "Anna", "role": "Supervisor"}}]}}
- "Person: John, role: Foreman" -> {{"people": [{{"name": "John", "role": "Foreman"}}]}}
- "Hello" -> {{}}

Input: {text}
"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        data = json.loads(response.choices[0].message.content)
        for field in ["category", "segment"]:
            if field in data and isinstance(data[field], str):
                data[field] = re.sub(r'^(category|segment)\s*:?\s*', '', data[field], flags=re.IGNORECASE).strip()
        if not data and text.strip():
            if re.search(r'\b(issue|problem|delay|fault|error)\b', text.lower()):
                return {"issues": [{"description": text.strip()}]}
            if re.search(r'\b(work|activity|task|progress)\b', text.lower()) and re.search(r'\b(at|in|on)\b', text.lower()):
                location = re.split(r'\b(at|in|on)\b', text.lower())[-1].strip().title()
                activity = re.split(r'\b(at|in|on)\b', text, 1)[0].strip()
                return {"site_name": location, "activities": [activity]}
            return {"comments": text.strip()}
        logger.info(f"Extracted report: {data}")
        return data
    except Exception as e:
        logger.error(f"GPT extract error: {e}\n{traceback.format_exc()}")
        return {}

@settings.retry
def apply_correction(orig: Dict[str, Any], corr: str) -> Dict[str, Any]:
    """Apply corrections to session data using GPT."""
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nCorrection:\n" + corr +
        "\n\nReturn JSON with only corrected fields. For lists (company, people, etc.), replace existing entries (e.g., 'Correct company Elektra Meyer to Elektro-Meier' updates the company name). Do not add duplicates."
    )
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        partial = json.loads(response.choices[0].message.content)
        logger.info(f"Correction response: {partial}")
        return merge_structured_data(orig, partial)
    except Exception as e:
        logger.error(f"GPT correction error: {e}\n{traceback.format_exc()}")
        return orig

# --- Webhook ---
COMMAND_PATTERN = re.compile(
    r'^(?P<action>correct|update|delete|remove)\s+'
    r'(?P<field>site|segment|category|company|person|tool|service|activity|issue|time|weather|impression|comments)'
    r'(?:\s*:\s*(?P<value>.+))?(?:\s+to\s+(?P<new_value>.+))?$',
    re.IGNORECASE
)
RESET_COMMANDS = {"new", "new report", "reset", "/new"}

# Rate limiting: track last processed message per chat_id
last_processed = {}  # chat_id -> (text, timestamp)

@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram messages."""
    try:
        data = request.get_json(force=True)
        if "message" not in data:
            logger.info("No message in webhook data")
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not chat_id:
            logger.error("Missing chat_id in message")
            return "ok", 200  # Avoid retries
        text = (msg.get("text", "") or "").strip()
        logger.info(f"Received message: chat_id={chat_id}, text='{text}'")

        # Rate limiting: skip if same text processed recently
        current_time = time()
        last_text, last_time = last_processed.get(chat_id, ("", 0))
        if text and text == last_text and current_time - last_time < settings.rate_limit_seconds:
            logger.info(f"Skipping repeated message: chat_id={chat_id}, text='{text}'")
            return "ok", 200
        last_processed[chat_id] = (text, current_time)

        with session_manager() as session_data:
            sess = session_data.setdefault(chat_id, {"structured_data": blank_report(), "awaiting_correction": False})
            data = sess.get("structured_data", blank_report())
            logger.info(f"Session data for chat_id={chat_id}: {json.dumps(data, indent=2)}")

            # Handle voice input
            if "voice" in msg:
                text = transcribe_voice(msg["voice"]["file_id"])
                if not text:
                    send_telegram_message(chat_id, "⚠️ Couldn't understand audio. Please speak clearly (e.g., 'Work at ABC').")
                    return "ok", 200
                logger.info(f"Transcribed voice: '{text}'")

            # Handle reset
            if text.strip().lower() in RESET_COMMANDS:
                logger.info(f"Resetting report for chat_id={chat_id}")
                new_data = blank_report()
                sess["structured_data"] = new_data
                sess["awaiting_correction"] = False
                summary = summarize_data(new_data)
                if not isinstance(summary, str):
                    logger.error(f"Invalid summary: {summary}")
                    summary = "Error generating summary"
                message = f"**Fresh report**\n\n{summary}\n\nEnter first field (site name required)."
                send_telegram_message(chat_id, message)
                logger.info(f"Reset completed for chat_id={chat_id}")
                return "ok", 200

            # Handle commands
            match = COMMAND_PATTERN.match(text)
            if match:
                action, field, value, new_value = match.groups()
                field = "people" if field == "person" else field
                if action in ("delete", "remove"):
                    if FIELD_CONFIG[field].get("scalar"):
                        data[field] = ""
                    elif value:
                        data[field] = [item for item in data.get(field, [])
                                      if not (isinstance(item, dict) and
                                              item.get(FIELD_CONFIG[field]["key"], "").lower() == value.lower())]
                    send_telegram_message(chat_id, f"Removed {field}" + (f": {value}" if value else "") +
                                         "\n\nUpdated report:\n\n" + summarize_data(data) +
                                         "\n\nAnything else?")
                    return "ok", 200
                if action in ("correct", "update") and field in ("company", "people") and value and new_value:
                    target_list = data.get(field, [])
                    for i, item in enumerate(target_list):
                        if isinstance(item, dict) and item.get("name", "").lower() == value.lower():
                            target_list[i] = {"name": new_value, "role": item.get("role", "")} if field == "people" else {"name": new_value}
                            logger.info(f"Corrected {field}: {value} to {new_value}")
                            break
                    data[field] = target_list
                    send_telegram_message(chat_id, f"Corrected {field}: {value} to {new_value}" +
                                         "\n\nUpdated report:\n\n" + summarize_data(data) +
                                         "\n\nAnything else?")
                    return "ok", 200

            # Handle new data or corrections
            if not sess["awaiting_correction"]:
                extracted = extract_site_report(text)
                if not extracted.get("site_name"):
                    send_telegram_message(chat_id, "🏗️ Please provide a site name (e.g., 'Site: Downtown' or 'Work at ABC').")
                    return "ok", 200
                data.update(merge_structured_data(data, extracted))
                sess["awaiting_correction"] = True
                send_telegram_message(chat_id, "Understood:\n\n" + summarize_data(data) +
                                     "\n\nCorrect or add more details.")
            else:
                data.update(apply_correction(data, text))
                send_telegram_message(chat_id, "Updated report:\n\n" + summarize_data(data) +
                                     "\n\nAnything else?")

        return "ok", 200

    except Exception as e:
        logger.error(f"Webhook error: {e}\n{traceback.format_exc()}")
        return "ok", 200  # Prevent Telegram retries

@app.get("/")
def health():
    """Health check endpoint."""
    return "OK", 200

if __name__ == "__main__":
    logger.info("Starting Flask app")
    app.run(port=settings.port, debug=True)
