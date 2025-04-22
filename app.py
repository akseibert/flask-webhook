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
    if "site_name" in data:      lines.append(f"📍 Site: {data['site_name']}")
    if "segment" in data:        lines.append(f"📆 Segment: {data['segment']}")
    if "category" in data:       lines.append(f"🌿 Category: {data['category']}")
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
                caused_by = i.get("caused_by","unknown")
                has_photo = i.get("has_photo",False)
                lines.append(f"• {i['description']} (by {caused_by}){' 📸' if has_photo else ''}")
    if "time" in data:           lines.append(f"⏰ Time: {data['time']}")
    if "weather" in data:        lines.append(f"🌦️ Weather: {data['weather']}")
    if "impression" in data:     lines.append(f"💬 Impression: {data['impression']}")
    if "comments" in data:       lines.append(f"📝 Comments: {data['comments']}")
    if "date" in data:           lines.append(f"🗓️ Date: {data['date']}")
    return "\n".join(lines)

def enrich_with_date(data):
    today = datetime.now().strftime("%d-%m-%Y")
    if not data.get("date"):
        data["date"] = today
    else:
        try:
            parsed = datetime.strptime(data["date"], "%d-%m-%Y")
            if parsed > datetime.now():
                data["date"] = today
        except:
            data["date"] = today
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + f"\n{text}"
    messages = [
        {"role":"system","content":"You ONLY extract explicitly mentioned fields; never infer or guess."},
        {"role":"user","content":prompt}
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

def apply_correction(original, correction_text):
    prompt = f"""
Original JSON:
{json.dumps(original)}

User said correction:
\"\"\"{correction_text}\"\"\"

Return ONLY the fields that change, in valid JSON."""
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ Correction GPT parsing failed:", e)
        return {}

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print("📩 Telegram webhook received:", json.dumps(data, indent=2))
        if "message" not in data:
            return "No message", 400

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text") or ""
        if not text and msg.get("voice"):
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id, "⚠️ Couldn't understand the audio. Please retry.")
                return "No transcription", 200

        # init session
        if chat_id not in session_data:
            session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}

        stored = session_data[chat_id]["structured_data"]

        # **Correction branch**
        if session_data[chat_id]["awaiting_correction"]:
            delta = apply_correction(stored, text)
            # **Merge** only changed fields back into the full data
            for k,v in delta.items():
                stored[k] = v
            session_data[chat_id]["structured_data"] = stored
            # still allow further corrections
            session_data[chat_id]["awaiting_correction"] = True

            full = summarize_data(stored)
            send_telegram_message(chat_id,
                f"✅ Got it! Here’s the **full** updated report:\n\n{full}\n\n✅ Anything else to correct?"
            )
            return "Corrected", 200

        # **Initial extraction branch**
        extracted = extract_site_report(text)
        if not extracted or "site_name" not in extracted:
            send_telegram_message(chat_id, "⚠️ Sorry, I couldn't detect site info. Please try again.")
            return "Missing fields", 200

        enriched = enrich_with_date(extracted)
        session_data[chat_id] = {"structured_data": enriched, "awaiting_correction": True}
        summary = summarize_data(enriched)
        send_telegram_message(chat_id,
            f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections."
        )
        return "Extracted", 200

    except Exception as e:
        print("❌ Error in Telegram webhook:", e)
        return "Error", 500

# Prompt template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report from the user’s input.
⚠️ Only extract fields the user **explicitly** mentions. Never infer or guess missing data.

Return JSON with any of these, omitting unmentioned fields:
- site_name
- segment
- category
- company: [{"name": "..."}]
- people: [{"name": "...","role":"..."}]
- tools: [{"item":"...","company":"..."}]
- service: [{"task":"...","company":"..."}]
- activities: [ "...", ... ]
- issues: [{"description":"...","caused_by":"...","has_photo":true/false}]
- time
- weather
- impression
- comments
- date (dd-mm-yyyy)
"""
