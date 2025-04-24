import re
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

# In‐memory session store
session_data = {}  # telegram_user_id → {"structured_data": {...}, "awaiting_correction": bool}

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
        audio_resp = requests.get(audio_url)
        if audio_resp.status_code != 200:
            print("❌ Failed to fetch audio")
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
    if "site_name" in data:    lines.append(f"📍 Site: {data['site_name']}")
    if "segment"   in data:    lines.append(f"📆 Segment: {data['segment']}")
    if "category"  in data:    lines.append(f"🌿 Category: {data['category']}")
    if "company"   in data and isinstance(data["company"], list):
        lines.append("🏣 Companies: " + ", ".join(c["name"] for c in data["company"] if isinstance(c, dict)))
    if "people" in data and isinstance(data["people"], list):
        people = ", ".join(f"{p.get('name','')} ({p.get('role','')})" for p in data["people"] if isinstance(p, dict))
        lines.append(f"👷 People: {people}")
    if "service" in data and isinstance(data["service"], list):
        svc = ", ".join(f"{s['task']} ({s['company']})" for s in data["service"] if isinstance(s, dict))
        lines.append(f"🔧 Services: {svc}")
    if "tools" in data and isinstance(data["tools"], list):
        tls = ", ".join(f"{t['item']} ({t.get('company','')})" for t in data["tools"] if isinstance(t, dict))
        lines.append(f"🛠️ Tools: {tls}")
    if "activities" in data and isinstance(data["activities"], list):
        lines.append("📋 Activities: " + ", ".join(data["activities"]))
    if "issues" in data and isinstance(data["issues"], list):
        lines.append("⚠️ Issues:")
        for i in data["issues"]:
            if isinstance(i, dict):
                cb = i.get("caused_by", "unknown")
                ph = " 📸" if i.get("has_photo") else ""
                lines.append(f"• {i['description']} (by {cb}){ph}")
    if "time"       in data:    lines.append(f"⏰ Time: {data['time']}")
    if "weather"    in data:    lines.append(f"🌦️ Weather: {data['weather']}")
    if "impression" in data:    lines.append(f"💬 Impression: {data['impression']}")
    if "comments"   in data:    lines.append(f"📝 Comments: {data['comments']}")
    if "date"       in data:    lines.append(f"🗓️ Date: {data['date']}")
    return "\n".join(lines)

def enrich_with_date(data):
    today_str = datetime.now().strftime("%d-%m-%Y")
    if not data.get("date"):
        data["date"] = today_str
    else:
        try:
            parsed = datetime.strptime(data["date"], "%d-%m-%Y")
            if parsed > datetime.now():
                data["date"] = today_str
        except:
            data["date"] = today_str
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    messages = [
        {"role":"system","content":"You ONLY extract fields explicitly mentioned."},
        {"role":"user",  "content":prompt}
    ]
    try:
        resp = client.chat.completions.create(model="gpt-3.5-turbo", messages=messages, temperature=0.2)
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

def apply_correction(original, correction_text):
    prompt = (
        "You are helping correct structured site data. Original JSON:\n"
        f"{json.dumps(original)}\nUser said: \"{correction_text}\"\n"
        "Return only the corrected JSON."
    )
    try:
        resp = client.chat.completions.create(model="gpt-3.5-turbo",
                                              messages=[{"role":"user","content":prompt}])
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ Correction GPT parsing failed:", e)
        return original

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("📩 Telegram webhook received:", json.dumps(data, indent=2))

    if "message" not in data:
        return "No message", 400

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text") or ""
    if not text and "voice" in msg:
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not text:
            send_telegram_message(chat_id, "⚠️ Couldn't understand audio.")
            return "", 200

    if chat_id not in session_data:
        session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}

    session = session_data[chat_id]

    # Corrections flow
    if session["awaiting_correction"]:
        updated = apply_correction(session["structured_data"], text)
        enriched = enrich_with_date(updated)
        session["structured_data"] = enriched
        session["awaiting_correction"] = True
        summary = summarize_data(enriched)
        send_telegram_message(chat_id, f"✅ Got it! Here’s the **full** updated report:\n\n{summary}\n\n✅ Anything else to correct?")
        return "", 200

    # Initial extraction flow
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id, "⚠️ Sorry, I couldn’t detect site info. Please try again.")
        return "", 200

    enriched = enrich_with_date(extracted)
    session["structured_data"] = enriched
    session["awaiting_correction"] = True
    summary = summarize_data(enriched)

    send_telegram_message(chat_id, f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections.")
    return "", 200

# GPT Prompt Template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report from free‐form input.
Only include fields explicitly mentioned; never guess missing data. Return JSON with:
site_name, segment, category, company, people, tools, service,
activities, issues, time, weather, impression, comments, date (dd-mm-yyyy).
"""

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 5000)))
