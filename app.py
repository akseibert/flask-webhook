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
logger.addHandler(logging.StreamHandler())  # Also print to console for Render debugging

# --- Initialize OpenAI client ---
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    logger.info("OpenAI client initialized")
except Exception as e:
    logger.error(f"OpenAI init failed: {e}")
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
        logger.error("Missing TELEGRAM_BOT_TOKEN")
        raise ValueError("Missing TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    response.raise_for_status()
    logger.info(f"Sent Telegram message to {chat_id}: {text[:50]}…")
    return response

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Missing TELEGRAM_BOT_TOKEN")
        raise ValueError("Missing TELEGRAM_BOT_TOKEN")
    response = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    response.raise_for_status()
    file_path = response.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        logger.info(f"Fetching audio: {audio_url}")
        audio_response = requests.get(audio_url)
        audio_response.raise_for_status()
        audio = audio_response.content
        logger.info(f"Audio file size: {len(audio)} bytes")
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio, "audio/ogg")
        )
        logger.info(f"Transcription response type: {type(response)}")
        logger.info(f"Transcription response attributes: {dir(response)}")
        text = response.text.strip()
        if not text or len(text.split()) < 2:
            logger.warning(f"Transcription invalid or too short: '{text}'")
            return "Issue: Unclear audio input"
        logger.info(f"Transcribed audio: '{text}'")
        return text
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return "Issue: Audio transcription failed"

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
You are an AI assistant extracting a construction site report from user input. Extract only explicitly mentioned fields and return them in JSON format. If no fields are clearly identified, check for issue-related keywords and treat the input as an issue only if such keywords are present. Otherwise, return {}.

Fields to extract (omit if not present):
- site_name: string (e.g., "Downtown Project")
- segment: string
- category: string
- company: list of objects with "name" (e.g., [{"name": "Acme Corp"}])
- people: list of objects with "name" and "role" (e.g., [{"name": "John Doe", "role": "Foreman"}])
- tools: list of objects with "item" and "company" (e.g., [{"item": "Crane", "company": "Acme Corp"}])
- service: list of objects with "task" and "company" (e.g., [{"task": "Excavation", "company": "Acme Corp"}])
- activities: list of strings (e.g., ["Concrete pouring"])
- issues: list of objects with "description" (required), "caused_by" (optional), and "has_photo" (optional, default false)
  (e.g., [{"description": "Delayed delivery", "caused_by": "Supplier", "has_photo": true}])
- time: string
- weather: string
- impression: string
- comments: string
- date: string (format dd-mm-yyyy)

Rules:
- Extract fields only when explicitly mentioned with clear intent (e.g., "Site: Downtown Project", "Company: Acme Corp").
- For issues:
  - "description" is mandatory.
  - Recognize keywords: "Issue", "Issues", "Problem", "Problems", "Delay", "Fault", "Error", or natural language (e.g., "The issue is...", "There’s a delay").
  - "caused_by" is optional, extracted if mentioned (e.g., "caused by Supplier").
  - "has_photo" is true only if "with photo" or "has photo" is explicitly stated.
  - Handle multiple issues as separate objects.
- If input is ambiguous but contains issue-related keywords (e.g., "Issue", "Problem"), treat it as an issue with the full text as "description".
- If input lacks clear field identifiers and no issue keywords, return {}.
- Omit unclear, empty, or inferred fields.
- Case-insensitive matching for keywords.

Examples:
1. Input: "Site: Downtown Project, Issue: Delayed delivery caused by Supplier with photo"
   Output: {"site_name": "Downtown Project", "issues": [{"description": "Delayed delivery", "caused_by": "Supplier", "has_photo": true}]}
2. Input: "Issues: Broken equipment, Missing tools caused by Beta Inc"
   Output: {"issues": [{"description": "Broken equipment"}, {"description": "Missing tools", "caused_by": "Beta Inc"}]}
3. Input: "The issue is a late delivery"
   Output: {"issues": [{"description": "Late delivery"}]}
4. Input: "Company: Acme Corp, There’s a delay in delivery"
   Output: {"company": [{"name": "Acme Corp"}], "issues": [{"description": "Delay in delivery"}]}
5. Input: "Hello world"
   Output: {}
6. Input: "Problem: Faulty wiring detected"
   Output: {"issues": [{"description": "Faulty wiring detected"}]}
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    messages = [
        {"role": "system", "content": "Extract only explicitly stated fields; treat ambiguous input as an issue only if it contains issue-related keywords."},
        {"role": "user", "content": gpt_prompt + "\nInput text: " + text}
    ]
    try:
        logger.info(f"Processing input text: '{text}'")
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        raw_response = response.choices[0].message.content
        logger.info(f"Raw GPT response: {raw_response}")
        data = json.loads(raw_response)
        logger.info(f"Extracted report: {data}")
        # Fallback: If no fields are extracted and input contains issue keywords, treat as issue
        issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error)\b'
        if not data and text.strip() and re.search(issue_keywords, text.lower()):
            data = {"issues": [{"description": text.strip()}]}
            logger.info(f"Fallback applied: Treated input as issue: {data}")
        return data
    except Exception as e:
        logger.error(f"GPT extract error for input '{text}': {e}")
        # Fallback for extraction failure with issue keywords
        issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error)\b'
        if text.strip() and re.search(issue_keywords, text.lower()):
            logger.info(f"Extraction failed; fallback to issue: {text}")
            return {"issues": [{"description": text.strip()}]}
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
                    # Avoid duplicates by checking description
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
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False
            }
        sess = session_data[chat_id]

        # Voice message
        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if text.startswith("Issue:"):
                logger.info(f"Transcribed voice to text: '{text}'")
            else:
                send_telegram_message(chat_id,
                    f"⚠️ Couldn't understand the audio. I heard: '{text}'.\nPlease speak clearly (e.g., say 'Issue: Delayed delivery') and try again.")
                return "ok", 200

        # Reset
        if text.lower() in ("new", "new report", "reset", "/new"):
            sess["structured_data"] = blank_report()
            sess["awaiting_correction"] = False
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "**Starting a fresh report**\n\n" + tpl +
                "\n\nSpeak or type your first field (site name required).")
            return "ok", 200

        # First extraction
        if not sess["awaiting_correction"]:
            extracted = extract_site_report(text)
            if not extracted.get("site_name"):
                send_telegram_message(chat_id,
                    "🏗️ Please provide a site name to start the report (e.g., 'Site: Downtown Project').")
                return "ok", 200
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(extracted)
            )
            sess["awaiting_correction"] = True
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "Here’s what I understood:\n\n" + tpl +
                "\n\nIs this correct? Reply with corrections or more details.")
            return "ok", 200

        # Correction or addition
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = merge_structured_data(
            sess["structured_data"], enrich_with_date(updated)
        )
        save_session_data(session_data)
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            "Got it! Here’s the **full** updated report:\n\n" + tpl +
            "\n\nAnything else to add or correct?")
        return "ok", 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

if __name__ == "__main__":
    logger.info("Starting Flask app")
    app.run(port=int(os.getenv("PORT", 5000)), debug=True)
