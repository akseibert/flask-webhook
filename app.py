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
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        response.raise_for_status()
        logger.info(f"Sent Telegram message to {chat_id}")
        return response
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
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
You are an AI assistant extracting a construction site report from text. Your task is to parse the input text and return a JSON object containing only the fields explicitly mentioned. The possible fields are:
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

Extraction Rules:
- Only extract fields explicitly mentioned in the text. Do not infer or assume any information.
- For lists (e.g., company, issues), include all mentioned items in the specified format.
- For issues:
  - The "description" field is mandatory. Every issue must have a description.
  - "caused_by" and "has_photo" are optional. Set "has_photo" to true only if explicitly mentioned (e.g., "with photo", "has photo").
  - Extract multiple issues as separate objects in the "issues" list.
  - Recognize issues from keywords like "Issue", "Issues", "Problem", "Problems", or natural language phrases indicating a problem (e.g., "The issue is...", "There’s a delay").
- If a field is mentioned but empty or unclear, omit it from the JSON.
- Return an empty JSON object ({}) if no valid fields are extracted.
- Handle natural language inputs and structured formats (e.g., "Issue: Delayed delivery").

Examples:
1. Input: "Issue: Delayed delivery caused by Supplier with photo"
   Output: {
     "issues": [{"description": "Delayed delivery", "caused_by": "Supplier", "has_photo": true}]
   }
2. Input: "Issues: Broken equipment, Missing tools caused by Beta Inc"
   Output: {
     "issues": [
       {"description": "Broken equipment"},
       {"description": "Missing tools", "caused_by": "Beta Inc"}
     ]
   }
3. Input: "The issue is a late delivery due to the supplier"
   Output: {
     "issues": [{"description": "Late delivery", "caused_by": "Supplier"}]
   }
4. Input: "Problem: Faulty wiring"
   Output: {
     "issues": [{"description": "Faulty wiring"}]
   }
5. Input: "Site: Downtown Project, There’s a delay in delivery"
   Output: {
     "site_name": "Downtown Project",
     "issues": [{"description": "Delay in delivery"}]
   }

Input text: {text}
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
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
            # Attempt to extract issues from malformed response
            data = {}
            if "issues" in raw_response.lower():
                issues = []
                for line in raw_response.split("\n"):
                    if any(kw in line.lower() for kw in ["issue", "problem"]):
                        desc = line.strip().split(":", 1)[-1].strip() if ":" in line else line.strip()
                        if desc:
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

@retry(stop=stop_after_attempt(3), wait=wait_exponential
