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
# { telegram_user_id: {"structured_data": {...}, "awaiting_correction": True/False} }
session_data = {}

def send_telegram_message(chat_id, text):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤 Sending to Telegram:", url)
    print("📤 Payload:", json.dumps(payload, indent=2))
    resp = requests.post(url, json=payload)
    print("✅ Telegram message sent:", resp.status_code, resp.text)

def get_telegram_file_path(file_id):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_token}/getFile?file_id={file_id}"
    resp = requests.get(url)
    file_path = resp.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{telegram_token}/{file_path}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        audio_resp = requests.get(audio_url)
        if audio_resp.status_code != 200:
            print("❌ Failed to fetch audio from Telegram")
            return ""
        whisper_resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio_resp.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper_resp.json().get("text", "")
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
        lines.append("🛠️ Tools: " + ", ".join(f"{t['item']} ({t.get('company','')})" for t in data["tools"] if isinstance(t, dict)))
    if "activities" in data and isinstance(data["activities"], list):
        lines.append("📋 Activities: " + ", ".join(data["activities"]))
    if "issues" in data and isinstance(data["issues"], list):
        lines.append("⚠️ Issues:")
        for i in data["issues"]:
            if isinstance(i, dict):
                caused = i.get("caused_by", "unknown")
                photo = " 📸" if i.get("has_photo", False) else ""
                lines.append(f"• {i['description']} (by {caused}){photo}")
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
    if not data.get("date"):
        data["date"] = today_str
    else:
        try:
            parsed = datetime.strptime(data["date"], "%d-%m-%Y")
            if parsed > today:
                data["date"] = today_str
        except:
            data["date"] = today_str
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    messages = [
        {"role": "system", "content": "You are a strict assistant. You ONLY extract fields that are explicitly mentioned. Never guess or fill missing values."},
        {"role": "user",   "content": prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.2
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

def apply_correction(original_data, correction_text):
    system_prompt = """
You are an assistant that holds a full JSON record of a construction‐site report.
When the user gives you a correction, output the complete updated JSON,
including all fields (with corrections applied).
Do NOT output only the changed pieces.
Only change what the user asked. Keep everything else exactly as before.
"""
    user_prompt = (
        "Current JSON:\n"
        + json.dumps(original_data, ensure_ascii=False, indent=2)
        + "\n\nUser says:\n"
        + correction_text
        + "\n\nReturn the full updated JSON only."
    )
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt}
            ],
            temperature=0.2,
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print("❌ Correction parse error:", e)
        return original_data

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("📩 Telegram webhook received:", json.dumps(data, indent=2))

    if "message" not in data:
        return "No message", 400

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text") or ""

    # If voice, transcribe
    if not text and "voice" in msg:
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not text:
            send_telegram_message(chat_id, "⚠️ Sorry, I couldn’t understand the audio. Please try again.")
            return "", 200

    sess = session_data.setdefault(chat_id, {"structured_data": {}, "awaiting_correction": False})

    # Correction branch
    if sess["awaiting_correction"]:
        full = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = full
        # still allow further corrections
        summary = summarize_data(full)
        send_telegram_message(
            chat_id,
            "✅ Got it! Here’s the **full** updated report:\n\n"
            + summary
            + "\n\n✅ Anything else to correct?"
        )
        return "", 200

    # Initial extraction
    extracted = extract_site_report(text)
    if not extracted or "site_name" not in extracted:
        send_telegram_message(chat_id, "⚠️ Sorry, I couldn't detect site info. Please try again.")
        return "", 200

    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True
    summary = summarize_data(enriched)
    send_telegram_message(
        chat_id,
        "Here’s what I understood:\n\n"
        + summary
        + "\n\n✅ Is this correct? You can still send corrections."
    )
    return "", 200

# GPT Prompt Template
gpt_prompt_template = """
You are an AI assistant helping extract a construction site report based on a spoken or written summary from a site manager.

⚠️ Only extract information that is explicitly mentioned. Do NOT guess, infer, or fill missing fields.
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
- date (dd-mm-YYYY)
"""
