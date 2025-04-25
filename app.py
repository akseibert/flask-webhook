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
    filename="/opt/render/project/src/app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
# Add console logging for Render debugging
logging.getLogger().addHandler(logging.StreamHandler())

# --- Initialize OpenAI client ---
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error(f"Failed to initialize OpenAI client: {e}")
    raise

app = Flask(__name__)

# --- Session data persistence ---
SESSION_FILE = "/opt/render/project/src/session_data.json"

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
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        raise ValueError("Telegram bot token missing")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        )
        response.raise_for_status()
        logger.info(f"Sent Telegram message to {chat_id}")
        return response
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        raise ValueError("Telegram bot token missing")
    try:
        response = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
        response.raise_for_status()
        file_path = response.json()["result"]["file_path"]
        return f"https://api.telegram.org/file/bot{token}/{file_path}"
    except Exception as e:
        logger.error(f"Failed to get Telegram file path: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        logger.info(f"Fetching audio from: {audio_url}")
        resp = requests.get(audio_url)
        resp.raise_for_status()
        audio_bytes = resp.content

        # call Whisper
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio_bytes, "audio/ogg")
        )

        # grab the .text attribute
        text = getattr(result, "text", "")
        text = (text or "").strip()

        if not text:
            logger.warning(f"Whisper returned empty text. Full response object: {result}")
            return ""

        logger.info(f"Whisper transcription: '{text}'")
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
        ", ".join(
            c.get("name", "") if isinstance(c, dict) else str(c)
            for c in d.get("company", [])
        )
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
You are an AI assistant extracting a construction site report from text. Parse the input text and return a JSON object with only explicitly mentioned fields. Possible fields:
- site_name: string
- segment: string
- category: string
- company: list of objects with "name"
- people: list of objects with "name" and "role"
- tools: list of objects with "item" and "company"
- service: list of objects with "task" and "company"
- activities: list of strings
- issues: list of objects with "description" (required), "caused_by" (optional), and "has_photo" (optional, default false)
- time: string
- weather: string
- impression: string
- comments: string
- date: string (dd-mm-yyyy)
Rules:
- Extract only explicitly mentioned fields. Do not infer or assume.
- Omit unclear or empty fields. Return {} if nothing.
Input text: {text}
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    if not text.strip():
        logger.warning("Empty input text received")
        return {}
    messages = [
        {"role": "system", "content": "Extract only explicitly stated fields; never guess."},
        {"role": "user", "content": gpt_prompt.replace("{text}", text)}
    ]
    try:
        logger.info(f"Processing input text: '{text}'")
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        raw = response.choices[0].message.content
        logger.info(f"Raw GPT response: {raw}")
        return json.loads(raw)
    except Exception as e:
        logger.error(f"GPT extract error for input '{text}': {e}")
        return {}

def merge_structured_data(existing, new):
    merged = existing.copy()
    for key, value in new.items():
        if key in ["company", "people", "tools", "service", "activities", "issues"]:
            existing_list = merged.get(key, [])
            new_items = value if isinstance(value, list) else []
            for item in new_items:
                if key == "issues":
                    if not isinstance(item, dict) or "description" not in item:
                        continue
                    if not any(existing_item.get("description") == item["description"]
                               for existing_item in existing_list
                               if isinstance(existing_item, dict)):
                        existing_list.append(item)
                elif item not in existing_list:
                    existing_list.append(item)
            merged[key] = existing_list
        else:
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
    updated = structured_data.copy()
    t = target.strip().lower()
    # scalar fields
    for field in ["site_name","segment","category","time","weather","impression","comments","date"]:
        if t == field or t.startswith(field+" "):
            updated[field] = ""
            return updated
    # list fields
    mapping = {
        "company":"name","people":"name","tools":"item",
        "service":"task","activities":None,"issues":"description"
    }
    for field, key in mapping.items():
        if t.startswith(field+" ") or (key and t.startswith(key+" ")):
            value = t.split(" ",1)[1].strip()
            lst = updated.get(field,[])
            if key:
                lst = [i for i in lst if i.get(key,"").lower()!=value]
            else:
                lst = [i for i in lst if i.lower()!=value]
            updated[field] = lst
            return updated
    return updated

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if "message" not in data:
            logger.info("No message in webhook data")
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        logger.info(f"Received webhook message: chat_id={chat_id}, text='{text}'")

        # Initialize session
        if chat_id not in session_data:
            session_data[chat_id] = {"structured_data": blank_report(), "awaiting_correction": False}
        sess = session_data[chat_id]

        # Voice handling
        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id,
                    "Couldn't understand the audio. Please speak clearly and try again.")
                return "ok", 200
            logger.info(f"Transcribed voice to text: '{text}'")

        # Reset
        if text.lower() in ("new","new report","reset","/new"):
            sess["structured_data"] = blank_report()
            sess["awaiting_correction"] = False
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "**Starting a fresh report**\n\n" + tpl +
                "\n\nTell me any details about your report.")
            return "ok", 200

        # Delete
        if text.lower().startswith("delete "):
            target = text[7:].strip()
            if not target:
                send_telegram_message(chat_id,
                    "Please specify what to delete (e.g., 'delete site_name').")
                return "ok", 200
            sess["structured_data"] = delete_from_report(sess["structured_data"], target)
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Deleted '{target}'. Updated report:\n\n" + tpl +
                "\n\nAnything else?")
            return "ok", 200

        # Extract or correct
        extracted = extract_site_report(text)
        logger.info(f"Extracted data: {extracted}")

        if not extracted and not sess["awaiting_correction"]:
            send_telegram_message(chat_id,
                "I couldn't extract any information. Please try again with details like 'Issue: Delayed delivery'.")
            return "ok", 200

        if sess["awaiting_correction"]:
            updated = apply_correction(sess["structured_data"], text)
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(updated)
            )
            sess["awaiting_correction"] = False
        else:
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(extracted)
            )
            sess["awaiting_correction"] = True

        save_session_data(session_data)
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            "Here's what I understood:\n\n" + tpl +
            "\n\nIs this correct? Reply with corrections or more details.")
        return "ok", 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 5000)), debug=True)
