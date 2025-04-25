from flask import Flask, request
import requests
import os
import json
import re
import logging
from datetime import datetime
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

# --- Initialize logging ---
logging.basicConfig(
    filename="app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Initialize OpenAI client ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# --- Session data persistence ---
SESSION_FILE = "session_data.json"

def load_session_data():
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data):
    try:
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
    response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
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
    lines.append(f"🏗️ **Site**: {d.get('site_name', '')}")
    lines.append(f"🛠️ **Segment**: {d.get('segment', '')}")
    lines.append(f"📋 **Category**: {d.get('category', '')}")
    lines.append(
        "🏢 **Companies**: " +
        ", ".join(c.get("name", "") if isinstance(c, dict) else str(c)
                  for c in d.get("company", []))
    )
    lines.append(
        "👷 **People**: " +
        ", ".join(
            f"{p.get('name', '')} ({p.get('role', '')})" if isinstance(p, dict) else str(p)
            for p in d.get("people", [])
        )
    )
    lines.append(
        "🔧 **Services**: " +
        ", ".join(
            f"{s.get('task', '')} ({s.get('company', '')})" if isinstance(s, dict) else str(s)
            for s in d.get("service", [])
        )
    )
    lines.append(
        "🛠️ **Tools**: " +
        ", ".join(
            f"{t.get('item', '')} ({t.get('company', '')})" if isinstance(t, dict) else str(t)
            for t in d.get("tools", [])
        )
    )
    lines.append("📅 **Activities**: " + ", ".join(d.get("activities", [])))
    lines.append("⚠️ **Issues**:")
    valid_issues = [
        i for i in d.get("issues", [])
        if isinstance(i, dict) and i.get("description", "").strip()
    ]
    for i in valid_issues:
        desc = i["description"]
        by = i.get("caused_by", "")
        photo = " 📸" if i.get("has_photo") else ""
        extra = f" (by {by})" if by else ""
        lines.append(f"  • {desc}{extra}{photo}")
    lines.append(f"⏰ **Time**: {d.get('time', '')}")
    lines.append(f"🌦️ **Weather**: {d.get('weather', '')}")
    lines.append(f"😊 **Impression**: {d.get('impression', '')}")
    lines.append(f"💬 **Comments**: {d.get('comments', '')}")
    lines.append(f"📆 **Date**: {d.get('date', '')}")
    return "\n".join(lines)

gpt_prompt = """
You are an AI assistant extracting a construction site report. Only extract what’s explicitly mentioned.
Return JSON with any of these fields (omit if not present):
site_name, segment, category,
company:[{"name":...}], people:[{"name":...,"role":...}],
tools:[{"item":...,"company":...}], service:[{"task":...,"company":...}],
activities:[...], issues:[{"description":...,"caused_by":...,"has_photo":...}],
time, weather, impression, comments, date (dd-mm-yyyy)
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    messages = [
        {"role": "system", "content": "Only extract explicitly stated fields; never guess."},
        {"role": "user", "content": gpt_prompt + "\n" + text}
    ]
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        data = json.loads(response.choices[0].message.content)
        logger.info(f"Extracted report: {data}")
        return data
    except Exception as e:
        logger.error(f"GPT extract error: {e}")
        return {}

def merge_structured_data(existing, new):
    merged = existing.copy()
    for key, value in new.items():
        if key in ["company", "people", "tools", "service", "activities", "issues"]:
            # Append to lists, avoiding duplicates
            existing_list = merged.get(key, [])
            new_items = value if isinstance(value, list) else []
            for item in new_items:
                if item not in existing_list:
                    existing_list.append(item)
            merged[key] = existing_list
        else:
            # Update scalar fields if not empty
            if value:
                merged[key] = value
    return merged

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def apply_correction(orig, corr):
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nUser correction:\n\"" + corr + "\"\n\n"
        "Return JSON with only corrected fields. Do not modify fields not explicitly mentioned."
    )
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        partial = json.loads(response.choices[0].message.content)
        merged = orig.copy()
        merged.update(partial)
        logger.info(f"Applied correction: {corr}")
        return merged
    except Exception as e:
        logger.error(f"GPT correction error: {e}")
        return orig

def delete_from_report(structured_data, target):
    """Process a deletion request and return updated structured data."""
    updated_data = structured_data.copy()
    target = target.strip().lower()
    
    # Scalar fields
    scalar_fields = ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]
    for field in scalar_fields:
        if target == field or target.startswith(f"{field} "):
            updated_data[field] = ""
            logger.info(f"Deleted scalar field: {field}")
            return updated_data

    # List fields
    list_fields = {
        "company": "name",
        "people": "name",
        "tools": "item",
        "service": "task",
        "activities": None,  # Direct string comparison
        "issues": "description"
    }
    
    for field, key in list_fields.items():
        if target.startswith(f"{field} ") or (key and target.startswith(f"{key} ")):
            value = target[len(f"{field} "):].strip() if target.startswith(f"{field} ") else target[len(f"{key} "):].strip()
            if not value:
                continue
            updated_list = updated_data.get(field, [])
            if key:
                updated_list = [item for item in updated_list if item.get(key, "").lower() != value.lower()]
            else:
                # For activities (simple strings)
                updated_list = [item for item in updated_list if item.lower() != value.lower()]
            updated_data[field] = updated_list
            logger.info(f"Deleted from {field}: {value}")
            return updated_data
    
    return updated_data

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if "message" not in data:
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()

        # Initialize session
        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False
            }
        sess = session_data[chat_id]

        # Handle voice message
        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id,
                    "  Couldn't understand the audio. Please try again.")
                return "ok", 200

        # Handle reset commands
        if text.lower() in ("new", "new report", "reset", "/new"):
            sess["structured_data"] = blank_report()
            sess["awaiting_correction"] = False
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "  **Starting a fresh report**\n\n" + tpl +
                "\n\n  Speak or type your first field (site name required)."
            )
            return "ok", 200

        # Handle deletion command
        if text.lower().startswith("delete "):
            target = text[7:].strip()  # Remove "delete " prefix
            if not target:
                send_telegram_message(chat_id,
                    "  Please specify what to delete (e.g., 'delete site_name' or 'delete issue Delayed delivery').")
                return "ok", 200
            sess["structured_data"] = delete_from_report(sess["structured_data"], target)
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"  Deleted '{target}'. Updated report:\n\n" + tpl +
                "\n\n  Anything else to add, correct, or delete?"
            )
            return "ok", 200

        # Handle first extraction
        if not sess["awaiting_correction"]:
            extracted = extract_site_report(text)
            if not extracted.get("site_name"):
                send_telegram_message(chat_id,
                    "  Please provide a site name to start the report.")
                return "ok", 200
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(extracted)
            )
            sess["awaiting_correction"] = True
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "Here’s what I understood:\n\n" + tpl +
                "\n\n  Is this correct? Reply with corrections or more details."
            )
            return "ok", 200

        # Handle correction or addition
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = merge_structured_data(
            sess["structured_data"], enrich_with_date(updated)
        )
        save_session_data(session_data)
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            "  Got it! Here’s the **full** updated report:\n\n" + tpl +
            "\n\n  Anything else to add or correct?"
        )
        return "ok", 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 5000)), debug=True)
