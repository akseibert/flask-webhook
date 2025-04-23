from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client (new SDK style)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# Health check
@app.route("/", methods=["GET"])
def index():
    return "Running", 200

# In-memory session store
# { telegram_user_id: {"structured_data": {...}, "awaiting_correction": True/False} }
session_data = {}

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤 Sending to Telegram:", url)
    print("📤 Payload:", json.dumps(payload, indent=2))
    r = requests.post(url, json=payload)
    print("✅ Telegram response:", r.status_code, r.text)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        ar = requests.get(audio_url)
        if ar.status_code != 200:
            print("❌ Failed to fetch audio")
            return ""
        wr = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", ar.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return wr.json().get("text", "")
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def summarize_data(data):
    lines = []
    if data.get("site_name"):
        lines.append(f"📍 Site: {data['site_name']}")
    if data.get("segment"):
        lines.append(f"📆 Segment: {data['segment']}")
    if data.get("category"):
        lines.append(f"🌿 Category: {data['category']}")
    if isinstance(data.get("company"), list):
        names = [c.get("name","") for c in data["company"] if isinstance(c, dict)]
        if names: lines.append("🏣 Companies: " + ", ".join(names))
    if isinstance(data.get("people"), list):
        ppl = [f"{p.get('name','')} ({p.get('role','')})" for p in data["people"] if isinstance(p, dict)]
        if ppl: lines.append("👷 People: " + ", ".join(ppl))
    if isinstance(data.get("service"), list):
        svcs = []
        for s in data["service"]:
            task = s.get("task","")
            comp = s.get("company")
            if comp:
                svcs.append(f"{task} ({comp})")
            else:
                svcs.append(task)
        if svcs: lines.append("🔧 Services: " + ", ".join(svcs))
    if isinstance(data.get("tools"), list):
        tls = []
        for t in data["tools"]:
            item = t.get("item","")
            comp = t.get("company")
            if comp:
                tls.append(f"{item} ({comp})")
            else:
                tls.append(item)
        if tls: lines.append("🛠️ Tools: " + ", ".join(tls))
    if isinstance(data.get("activities"), list):
        if data["activities"]:
            lines.append("📋 Activities: " + ", ".join(data["activities"]))
    if isinstance(data.get("issues"), list):
        if data["issues"]:
            lines.append("⚠️ Issues:")
            for i in data["issues"]:
                desc = i.get("description","")
                by   = i.get("caused_by","unknown")
                photo = " 📸" if i.get("has_photo") else ""
                lines.append(f"• {desc} (by {by}){photo}")
    if data.get("time"):
        lines.append(f"⏰ Time: {data['time']}")
    if data.get("weather"):
        lines.append(f"🌦️ Weather: {data['weather']}")
    if data.get("impression"):
        lines.append(f"💬 Impression: {data['impression']}")
    if data.get("comments"):
        lines.append(f"📝 Comments: {data['comments']}")
    if data.get("date"):
        lines.append(f"🗓️ Date: {data['date']}")
    return "\n".join(lines)

def enrich_with_date(data):
    today = datetime.now()
    ds = today.strftime("%d-%m-%Y")
    if not data.get("date"):
        data["date"] = ds
    else:
        try:
            pd = datetime.strptime(data["date"], "%d-%m-%Y")
            if pd > today:
                data["date"] = ds
        except:
            data["date"] = ds
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    messages = [
        {"role":"system","content":"Only extract fields explicitly mentioned. Never guess or fill missing values."},
        {"role":"user","content":prompt}
    ]
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

def apply_correction(original, correction_text):
    prompt = (
        "Correct only the fields mentioned. Original JSON:\n"
        f"{json.dumps(original)}\nUser said:\n\"{correction_text}\""
    )
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print("❌ Correction failed:", e)
        return original

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print("📩 Telegram webhook:", json.dumps(data, indent=2))

        if "message" not in data:
            return "No message", 400

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text")

        # handle voice → text
        if not text and "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id, "⚠️ Couldn't understand audio. Please try again.")
                return "No transcription", 200

        print(f"📩 From {chat_id}: {text}")

        # init session
        if chat_id not in session_data:
            session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}

        sd = session_data[chat_id]["structured_data"]
        awaiting = session_data[chat_id]["awaiting_correction"]

        # correction flow
        if awaiting:
            updated = apply_correction(sd, text)
            session_data[chat_id]["structured_data"] = updated
            # keep awaiting True so user can iterate indefinitely
            summary = summarize_data(updated)
            send_telegram_message(chat_id,
                f"✅ Got it! Here’s the **full** updated report:\n\n{summary}\n\n✅ Anything else to correct?"
            )
            return "Corrected", 200

        # new extraction
        extracted = extract_site_report(text)
        if not extracted.get("site_name"):
            send_telegram_message(chat_id,
                "⚠️ Sorry, I couldn't detect site info. Please try again."
            )
            return "Missing fields", 200

        enriched = enrich_with_date(extracted)
        session_data[chat_id] = {"structured_data": enriched, "awaiting_correction": True}
        summary = summarize_data(enriched)

        send_telegram_message(chat_id,
            f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections."
        )
        return "Extracted", 200

    except Exception as e:
        print("❌ Error in webhook:", e)
        return "Error", 500

# GPT Prompt Template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report from a spoken or written summary.
Only pull out data explicitly mentioned—do NOT guess or fill in missing details.

Return JSON (omit any not mentioned) with these fields:
- site_name
- segment
- category
- company: [ {"name": "..."} ]
- people: [ {"name": "...", "role": "..."} ]
- tools: [ {"item": "...", "company": "..."} ]
- service: [ {"task": "...", "company": "..."} ]
- activities: [ "..." ]
- issues: [ {"description":"...", "caused_by":"...", "has_photo":true/false} ]
- time
- weather
- impression
- comments
- date (dd-mm-yyyy)
"""
