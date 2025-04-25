from flask import Flask, request
import requests
import os
import json
import re
import logging
from datetime import datetime
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from difflib import SequenceMatcher
from contextlib import contextmanager
from typing import Dict, List, Any

# --- Initialize logging ---
logging.basicConfig(
    filename="/opt/render/project/src/app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())

# --- Initialize OpenAI client ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = Flask(__name__)

# --- Session data persistence ---
SESSION_FILE = "/opt/render/project/src/session_data.json"

def load_session_data() -> Dict[str, Any]:
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
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
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id: str, text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    )
    response.raise_for_status()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_voice(file_id: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    response = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    file_path = response.json()["result"]["file_path"]
    audio_response = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
    audio_response.raise_for_status()
    response = client.audio.transcriptions.create(
        model="whisper-1",
        file=("voice.ogg", audio_response.content, "audio/ogg")
    )
    text = response.text.strip()
    return text if text and len(text.split()) >= 2 else ""

# --- Data processing ---
def blank_report() -> Dict[str, Any]:
    return {
        "site_name": "", "segment": "", "category": "", "company": [], "people": [],
        "tools": [], "service": [], "activities": [], "issues": [], "time": "",
        "weather": "", "impression": "", "comments": "", "date": datetime.now().strftime("%d-%m-%Y")
    }

def string_similarity(a: str, b: str) -> float:
    similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    logger.info(f"String similarity between '{a}' and '{b}': {similarity}")
    return similarity

def is_similar(item: Dict, existing_item: Dict, field: str, threshold: float) -> bool:
    config = FIELD_CONFIG[field]
    key = config["key"]
    if not key:
        return False
    if field == "people":
        return (string_similarity(item.get(key, ""), existing_item.get(key, "")) > threshold and
                item.get("role") == existing_item.get("role"))
    if field in ("tools", "service"):
        return (string_similarity(item.get(key, ""), existing_item.get(key, "")) > threshold and
                string_similarity(item.get("company", "") or "", existing_item.get("company", "") or "") > threshold)
    return string_similarity(item.get(key, ""), existing_item.get(key, "")) > threshold

def merge_list_field(existing: List[Dict], new_items: List[Dict], key: str, similarity_threshold: float = 0.6) -> List[Dict]:
    """Merge or update items in a list field, deduplicating based on similarity."""
    existing_list = existing.copy()
    for item in new_items:
        if not isinstance(item, dict) or not item.get(FIELD_CONFIG[key]["key"]):
            continue
        matched = False
        for i, existing_item in enumerate(existing_list):
            if is_similar(item, existing_item, key, similarity_threshold):
                existing_list[i] = item
                logger.info(f"Replaced {key}: {existing_item.get(FIELD_CONFIG[key]['key'])} with {item.get(FIELD_CONFIG[key]['key'])}")
                matched = True
                break
        if not matched and item not in existing_list:
            existing_list.append(item)
            logger.info(f"Added new {key}: {item.get(FIELD_CONFIG[key]['key'])}")
    return existing_list

def merge_structured_data(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Merge new data into existing session data, handling lists and scalars."""
    merged = existing.copy()
    for key, value in new.items():
        if key in FIELD_CONFIG and not FIELD_CONFIG[key].get("scalar"):
            merged[key] = merge_list_field(merged.get(key, []), value if isinstance(value, list) else [], key)
        elif value:
            merged[key] = value
    return merged

def summarize_data(data: Dict[str, Any]) -> str:
    """Generate a formatted summary of the report data."""
    logger.info(f"Summarizing data: {json.dumps(data, indent=2)}")
    lines = []
    for field, config in FIELD_CONFIG.items():
        if field == "issues":
            lines.append(f"{config['icon']} **Issues**:")
            issues = [i for i in data.get("issues", []) if isinstance(i, dict) and i.get("description", "").strip()]
            lines.extend(config["format"](i) for i in issues) if issues else lines.append("  None")
        elif config.get("scalar"):
            value = data.get(field, "") or "None"
            lines.append(f"{config['icon']} **{field.title().replace('_', ' ')}**: {value}")
        else:
            items = data.get(field, [])
            value = ", ".join(config["format"](item) for item in items if isinstance(item, dict)) or "None"
            lines.append(f"{config.get('icon', '📅')} **{field.title()}**: {value}")
    return "\n".join(lines)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text: str) -> Dict[str, Any]:
    """Extract report fields from text using GPT or regex fallback."""
    # Try regex for "add [name] as [role]"
    person_match = re.match(r'^(?:add\s+)?(\w+\s*\w*)\s+as\s+(\w+\s*\w*)$', text, re.IGNORECASE)
    if person_match:
        name, role = person_match.groups()
        return {"people": [{"name": name.strip(), "role": role.strip()}]}

    prompt = """
    Extract construction site report fields from input text into JSON. Only include explicitly mentioned fields. Use keywords like "Site:", "Company:", or natural language intent. For segment/category, extract value only (e.g., "Category: 3" -> "category": "3"). For issues, recognize "Issue", "Problem", etc., with optional "caused_by" and "has_photo". For activities, detect "Work", "Task", etc. For people, recognize "add [name] as [role]" or "Person: [name], role: [role]". Fallback to comments for unclear inputs. Case-insensitive.

    Fields:
    - site_name: string
    - segment: string (no "Segment" prefix)
    - category: string (no "Category" prefix)
    - company: list of {"name": string}
    - people: list of {"name": string, "role": string}
    - tools: list of {"item": string, "company": string}
    - service: list of {"task": string, "company": string}
    - activities: list of strings
    - issues: list of {"description": string, "caused_by": string (optional), "has_photo": bool (default false)}
    - time: string
    - weather: string
    - impression: string
    - comments: string
    - date: string (dd-mm-yyyy)

    Examples:
    - "Site: Downtown, Issue: Delay with photo" -> {"site_name": "Downtown", "issues": [{"description": "Delay", "has_photo": true}]}
    - "Category: 3, Segment: 5" -> {"category": "3", "segment": "5"}
    - "Work at ABC" -> {"site_name": "ABC", "activities": ["Work"]}
    - "add Anna as Supervisor" -> {"people": [{"name": "Anna", "role": "Supervisor"}]}
    - "Person: John, role: Foreman" -> {"people": [{"name": "John", "role": "Foreman"}]}
    - "Hello" -> {}

    Input: {}
    """.format(text)
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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def apply_correction(orig: Dict[str, Any], corr: str) -> Dict[str, Any]:
    """Apply corrections to session data using GPT."""
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nCorrection:\n" + corr +
        "\n\nReturn JSON with only corrected fields. For lists (company, people, etc.), replace existing entries (e.g., 'Correct company Elektra Meyer to Elektro-Meier' updates the company name). Do not add duplicates."
    )
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}]
    )
    partial = json.loads(response.choices[0].message.content)
    logger.info(f"Correction response: {partial}")
    return merge_structured_data(orig, partial)

# --- Webhook ---
COMMAND_PATTERN = re.compile(
    r'^(?P<action>correct|update|delete|remove)\s+'
    r'(?P<field>site|segment|category|company|person|tool|service|activity|issue|time|weather|impression|comments)'
    r'(?:\s*:\s*(?P<value>.+))?(?:\s+to\s+(?P<new_value>.+))?$',
    re.IGNORECASE
)
RESET_COMMANDS = {"new", "new report", "reset", "/new"}

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if "message" not in data:
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        logger.info(f"Received message: chat_id={chat_id}, text='{text}'")

        with session_manager() as session_data:
            sess = session_data.setdefault(chat_id, {"structured_data": blank_report(), "awaiting_correction": False})
            data = sess["structured_data"]

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
                sess["structured_data"] = blank_report()
                sess["awaiting_correction"] = False
                send_telegram_message(chat_id, "**Fresh report**\n\n" + summarize_data(data) +
                                     "\n\nEnter first field (site name required).")
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
        logger.error(f"Webhook error: {e}")
        return "error", 500

if __name__ == "__main__":
    logger.info("Starting Flask app")
    app.run(port=int(os.getenv("PORT", 5000)), debug=True)
