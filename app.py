from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client (new SDK style)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# Health check route
@app.route("/", methods=["GET"])
def index():
    return "Running", 200

# In-memory session store
session_data = {}  # { telegram_user_id: {"structured_data": {...}, "awaiting_correction": True/False} }

def send_telegram_message(chat_id, text):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤 Sending to Telegram:", url)
    print("📤 Payload:", json.dumps(payload, indent=2))
    response = requests.post(url, json=payload)
    print("✅ Telegram message sent:", response.status_code, response.text)

def get_telegram_file_path(file_id):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_token}/getFile?file_id={file_id}"
    response = requests.get(url)
    file_path = response.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{telegram_token}/{file_path}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        audio_response = requests.get(audio_url)
        if audio_response.status_code != 200:
            print("❌ Failed to fetch audio from Telegram")
            return ""
        whisper_response = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio_response.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper_response.json().get("text", "")
    except Exception as e:
        print("❌ Transcription failed:", e)
        return ""

def summarize_data(data):
    lines = []
    if "site_name" in data:
        lines.append(f"📍 Site: {data['site_name']}")
    if "segment" in data:
        lines.append(f"📆 Segment: {data['segment']}")
    if "category" in data:
        lines.append(f"🌿 Category: {data['category']}")
    if "company" in data and isinstance(data["company"], list):
        lines.append("🏣 Companies: " + ", ".join(c["name"] for c in data["company"] if isinstance(c, dict)))
    if "people" in data and isinstance(data["people"], list):
        lines.append("👷 People: " + ", ".join(f"{p['name']} ({p['role']})" for p in data["people"] if isinstance(p, dict)))
    if "service" in data and isinstance(data["service"], list):
        lines.append("🔧 Services: " + ", ".join(f"{s['task']} ({s['company']})" for s in data["service"] if isinstance(s, dict)))
    if "tools" in data and isinstance(data["tools"], list):
        lines.append("🛠️ Tools: " + ", ".join(f"{t['item']} ({t['company']})" for t in data["tools"] if isinstance(t, dict)))
    if "activities" in data and isinstance(data["activities"], list):
        lines.append("📋 Activities: " + ", ".join(data["activities"]))
    if "issues" in data and isinstance(data["issues"], list):
        lines.append("⚠️ Issues:")
        for i in data["issues"]:
            if isinstance(i, dict):
                caused_by = i.get("caused_by", "unknown")
                has_photo = i.get("has_photo", False)
                lines.append(f"• {i['description']} (by {caused_by}){' 📸' if has_photo else ''}")
    if "time" in data:
        lines.append(f"⏰ Time: {data['time']}")
    if "weather" in data:
        lines.append(f"🌦️ Weather: {data['weather']}")
    if "impression" in data:
        lines.append(f"💬 Impression: {data['impression']}")
    if "comments" in data:
        lines.append(f"📝 Comments: {data['comments']}")
    if "date" in data:
        lines.append(f"🗓️ Date: {data['date']}")
    return "\n".join(lines)

def enrich_with_date(data):
    today = datetime.now()
    today_str = today.strftime("%d-%m-%Y")
    if "date" not in data or not data["date"]:
        data["date"] = today_str
    else:
        try:
            parsed_date = datetime.strptime(data["date"], "%d-%m-%Y")
            if parsed_date > today:
                data["date"] = today_str
        except Exception as e:
            print("❌ Date format invalid, defaulting to today.", e)
            data["date"] = today_str
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + f"\n{text}"
    messages = [
        {"role": "system", "content": "You are a strict assistant. You ONLY extract fields that are *explicitly* mentioned. Never guess, generalize, or fill in missing values."},
        {"role": "user", "content": prompt}
    ]
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.2
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

def apply_correction(original_data, correction_text):
    prompt = f"""
You are helping correct structured site data. This is the original structured JSON:
{json.dumps(original_data)}

The user said:
"{correction_text}"

Return the updated JSON with only the corrected fields changed.
"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print("❌ Correction GPT parsing failed:", e)
        return original_data

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print("📩 Telegram webhook received:", json.dumps(data, indent=2))

        if "message" not in data:
            return "No message found", 400

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text")

        # if voice, transcribe
        if not text and "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id, "⚠️ Sorry, I couldn't understand the audio message. Please try again.")
                return "No transcription", 200

        print(f"📩 Message from Telegram user {chat_id}: {text}")

        if chat_id not in session_data:
            session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}

        stored = session_data[chat_id]["structured_data"]

        # Correction branch
        if session_data[chat_id]["awaiting_correction"]:
            updated = apply_correction(stored, text)
            # **Consolidate**: update full data, keep correction mode on
            session_data[chat_id]["structured_data"] = updated
            session_data[chat_id]["awaiting_correction"] = True
            full_summary = summarize_data(updated)
            send_telegram_message(chat_id,
                f"✅ Got it! Here’s the **full** updated report:\n\n{full_summary}\n\n✅ Anything else to correct?"
            )
            return "Updated with correction", 200

        # Initial extraction
        extracted = extract_site_report(text)
        if not extracted or "site_name" not in extracted:
            send_telegram_message(chat_id, "⚠️ Sorry, I couldn't detect site info. Please try again.")
            return "Missing required fields", 200

        enriched = enrich_with_date(extracted)
        session_data[chat_id]["structured_data"] = enriched
        session_data[chat_id]["awaiting_correction"] = True
        summary = summarize_data(enriched)

        send_telegram_message(chat_id,
            f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections."
        )
        return "Summary sent", 200

    except Exception as e:
        print("❌ Error in Telegram webhook:", e)
        return "Error", 500

# GPT Prompt Template
gpt_prompt_template = """
You are an AI assistant helping extract a construction site report based on a spoken or written summary from a site manager.

⚠️ Only extract information that is **explicitly mentioned** in the input. Do NOT infer or guess missing information. Leave out any field that was not clearly stated.

Return the following fields as JSON (omit any not mentioned):
- site_name
- segment
- category
- company: list of {"name": "..."}
- people: list of {"name": "...", "role": "..."}
- tools: list of {"item": "...", "company": "..."}
- service: list of {"task": "...", "company": "..."}
- activities: list of strings
- issues: list of {"description": "...", "caused_by": "...", "has_photo": true/false}
- time
- weather
- impression
- comments
- date (format: dd-mm-yyyy)
"""
