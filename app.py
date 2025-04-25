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
logger.addHandler(logging.StreamHandler())  # Also print to console

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
        r = requests.get(audio_url)
        r.raise_for_status()
        audio_bytes = r.content
        logger.info(f"Audio file size: {len(audio_bytes)} bytes")
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio_bytes, "audio/ogg")
        )
        logger.info(f"Transcription response type: {type(result)}")
        logger.info(f"Transcription response attributes: {dir(result)}")
        text = getattr(result, "text", "").strip()
        if not text or len(text.split()) < 2:
            logger.warning(f"Transcription invalid or too short: '{text}'")
            return ""
        logger.info(f"Transcribed: {text}")
        return text
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return ""

# --- Data-extraction & formatting ---
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
You are an AI assistant extracting a construction site report from text. Parse the input text and return a JSON object with only explicitly mentioned fields. Possible fields:
- site_name: string
- segment: string
- category: string
- company: list of objects with "name" (e.g., [{"name": "Acme Corp"}])
- people: list of objects with "name" and "role" (e.g., [{"name": "John Doe", "role": "Foreman"}])
- tools: list of objects with "item" and "company" (e.g., [{"item": "Crane", "company": "Acme Corp"}])
- service: list of objects with "task" and "company" (e.g., [{"task": "Excavation", "company": "Acme Corp"}])
- activities: list of strings (e.g., ["Concrete pouring"])
- issues: list of objects with "description" (required), "caused_by" (optional), and "has_photo" (optional, default false) (e.g., [{"description": "Delayed delivery", "caused_by": "Supplier", "has_photo": true}])
- time: string
- weather: string
- impression: string
- comments: string
- date: string (format dd-mm-yyyy)

Rules:
- Extract only explicitly mentioned fields. Do not infer or assume.
- For issues:
  - "description" is mandatory.
  - "caused_by" and "has_photo" are optional. Set "has_photo" to true only if "with photo" or "has photo" is mentioned.
  - Recognize keywords: "Issue", "Issues", "Problem", "Problems", or natural language (e.g., "The issue is...", "There’s a delay").
  - Handle multiple issues as separate objects.
- Omit unclear or empty fields.
- Return {} if no valid fields are extracted.
- Be forgiving for short or ambiguous inputs, treating unrecognized text as a potential issue description if it seems like a problem.

Examples:
1. Input: "Issue: Delayed delivery caused by Supplier with photo"
   Output: {"issues": [{"description": "Delayed delivery", "caused_by": "Supplier", "has_photo": true}]}
2. Input: "Issues: Broken equipment, Missing tools caused by Beta Inc"
   Output: {"issues": [{"description": "Broken equipment"}, {"description": "Missing tools", "caused_by": "Beta Inc"}]}
3. Input: "The issue is a late delivery due to the supplier"
   Output: {"issues": [{"description": "Late delivery", "caused_by": "Supplier"}]}
4. Input: "Problem: Faulty wiring"
   Output: {"issues": [{"description": "Faulty wiring"}]}
5. Input: "Site: Downtown Project, There’s a delay in delivery"
   Output: {"site_name": "Downtown Project", "issues": [{"description": "Delay in delivery"}]}
6. Input: "Side down turn"
   Output: {"issues": [{"description": "Side down turn"}]}

Input text: {text}
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    if not text.strip():
        logger.warning("Empty input text received")
        return {}
    # Normalize input for keyword matching
    text_lower = text.lower()
    messages = [
        {"role": "system", "content": "Extract only explicitly stated fields; never guess."},
        {"role": "user", "content": gpt_prompt.replace("{text}", text)}
    ]
    try:
        logger.info(f"Processing input text: '{text}'")
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        raw_response = response.choices[0].message.content
        logger.info(f"Raw GPT response: {raw_response}")
        try:
            data = json.loads(raw_response)
        except json.JSONDecodeError:
            logger.error(f"JSON parsing failed, attempting to fix response: {raw_response}")
            data = {}
        # Fallback: extract issues from text if GPT returns empty or invalid
        if not data and any(kw in text_lower for kw in ["issue", "issues", "problem", "problems"]) or not any(
            kw in text_lower for kw in ["site", "company", "people", "tools", "service", "activities", "time", "weather", "impression", "comments"]
        ):
            issues = []
            lines = text.split(",")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                desc = line
                caused_by = ""
                has_photo = False
                if "caused by" in line.lower():
                    desc, caused_by = line.split("caused by", 1)
                    desc = desc.strip()
                    caused_by = caused_by.strip()
                if "with photo" in line.lower() or "has photo" in line.lower():
                    has_photo = True
                    desc = desc.replace("with photo", "").replace("has photo", "").strip()
                if desc and any(kw in desc.lower() for kw in ["issue", "problem"]):
                    desc = desc.split(":", 1)[-1].strip() if ":" in desc else desc.strip()
                if desc:
                    issue = {"description": desc}
                    if caused_by:
                        issue["caused_by"] = caused_by
                    if has_photo:
                        issue["has_photo"] = True
                    issues.append(issue)
                elif desc:  # Treat ambiguous text as an issue
                    issues.append({"description": desc})
            if issues:
                data["issues"] = issues
            logger.info(f"Fixed extracted report: {data}")
        logger.info(f"Extracted report: {data}")
        return data
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

# --- HTTP routes ---
@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        msg = data.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not chat_id:
            logger.info("No chat_id in webhook data")
            return "ok", 200

        # Pull either text, voice, or audio-file
        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        elif "audio" in msg:
            text = transcribe_from_telegram_voice(msg["audio"]["file_id"])
        else:
            text = (msg.get("text") or "").strip()

        logger.info(f"Incoming [{chat_id}]: {text[:50]}…")

        # Initialize session
        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False
            }
        sess = session_data[chat_id]

        # Handle voice message
        if ("voice" in msg or "audio" in msg) and not text:
            send_telegram_message(chat_id,
                f"Couldn't understand the audio. I heard: '{text}'. Please speak clearly (e.g., say 'Issue: Delayed delivery') and try again.")
            return "ok", 200

        # Handle reset commands
        if text.lower() in ("new", "new report", "reset", "/new"):
            sess["structured_data"] = blank_report()
            sess["awaiting_correction"] = False
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "**Starting a fresh report**\n\n" + tpl +
                "\n\nTell me any details about your report.")
            return "ok", 200

        # Handle deletion command
        if text.lower().startswith("delete "):
            target = text[7:].strip()
            if not target:
                send_telegram_message(chat_id,
                    "Please specify what to delete (e.g., 'delete site_name' or 'delete issue Delayed delivery').")
                return "ok", 200
            sess["structured_data"] = delete_from_report(sess["structured_data"], target)
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Deleted '{target}'. Updated report:\n\n" + tpl +
                "\n\nAnything else to add, correct, or delete?")
            return "ok", 200

        # Handle extraction or correction
        extracted = extract_site_report(text)
        logger.info(f"Extracted data: {extracted}")
        if not extracted and not sess["awaiting_correction"]:
            error_msg = f"I couldn't extract any information from '{text}'. Please try again with details like 'Issue: Delayed delivery', 'Site: Downtown', etc."
            if "voice" in msg or "audio" in msg:
                error_msg += "\nFor voice, speak clearly and use phrases like 'Issue: ...' or 'Site: ...'."
            send_telegram_message(chat_id, error_msg)
            return "ok", 200

        if sess["awaiting_correction"]:
            updated = apply_correction(sess["structured_data"], text)
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(updated)
            )
            logger.info(f"Applied correction, updated data: {sess['structured_data']}")
        else:
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(extracted)
            )
            sess["awaiting_correction"] = True
            logger.info(f"Initial extraction, updated data: {sess['structured_data']}")

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
    logger.info("Starting Flask app")
    app.run(port=int(os.getenv("PORT", 5000)), debug=True)
