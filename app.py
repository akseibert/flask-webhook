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

# In‑memory session store
# session_data = { telegram_user_id: { "structured_data": {...}, "awaiting_correction": bool } }
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
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    r = requests.get(url)
    return f"https://api.telegram.org/file/bot{token}/{r.json()['result']['file_path']}"

def transcribe_from_telegram_voice(file_id):
    try:
        download_url = get_telegram_file_path(file_id)
        print("🔗 Downloading audio from:", download_url)
        audio_r = requests.get(download_url)
        print("📥 Audio fetch status:", audio_r.status_code)
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio_r.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        print("🗣 Whisper status:", whisper.status_code)
        result = whisper.json()
        print("🗣 Whisper JSON:", json.dumps(result, indent=2))
        return result.get("text", "")
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def summarize_data(d):
    lines = []
    if d.get("site_name"):   lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):     lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):    lines.append(f"🌿 Category: {d['category']}")
    if isinstance(d.get("company"), list):
        comps = ", ".join(c["name"] for c in d["company"] if c.get("name"))
        lines.append(f"🏣 Companies: {comps}")
    if isinstance(d.get("people"), list):
        ppl = ", ".join(f"{p['name']} ({p['role']})" for p in d["people"])
        lines.append(f"👷 People: {ppl}")
    if isinstance(d.get("service"), list):
        svcs = ", ".join(f"{s['task']} ({s['company']})" for s in d["service"])
        lines.append(f"🔧 Services: {svcs}")
    if isinstance(d.get("tools"), list):
        tls = ", ".join(f"{t['item']} ({t['company']})" for t in d["tools"])
        lines.append(f"🛠️ Tools: {tls}")
    if isinstance(d.get("activities"), list):
        lines.append(f"📋 Activities: {', '.join(d['activities'])}")
    if isinstance(d.get("issues"), list):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            desc = i.get("description","")
            by   = i.get("caused_by","unknown")
            photo = " 📸" if i.get("has_photo") else ""
            lines.append(f"• {desc} (by {by}){photo}")
    if d.get("time"):       lines.append(f"⏰ Time: {d['time']}")
    if d.get("weather"):    lines.append(f"🌦️ Weather: {d['weather']}")
    if d.get("impression"): lines.append(f"💬 Impression: {d['impression']}")
    if d.get("comments"):   lines.append(f"📝 Comments: {d['comments']}")
    if d.get("date"):       lines.append(f"🗓️ Date: {d['date']}")
    return "\n".join(lines)

def enrich_with_date(d):
    today = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today
    else:
        try:
            parsed = datetime.strptime(d["date"], "%d-%m-%Y")
            if parsed > datetime.now():
                d["date"] = today
        except:
            d["date"] = today
    return d

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role":"system", "content":"You ONLY extract explicitly mentioned fields. Do NOT guess or fill missing values."},
        {"role":"user",   "content":prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=msgs, temperature=0.2
        )
        raw = resp.choices[0].message.content
        print("🧠 GPT raw reply:", raw)
        return json.loads(raw)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

def apply_correction(original, correction_text):
    prompt = (
        f"Original JSON:\n{json.dumps(original,indent=2)}\n\n"
        f"User correction:\n\"\"\"{correction_text}\"\"\"\n\n"
        "Return only the fields that changed in valid JSON."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2
        )
        raw = resp.choices[0].message.content
        print("🧠 Correction raw reply:", raw)
        return json.loads(raw)
    except Exception as e:
        print("❌ Correction parsing failed:", e)
        return {}

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        print("📩 Telegram update:", json.dumps(update, indent=2))

        msg = update.get("message", {})
        chat_id = str(msg.get("chat",{}).get("id",""))
        text = msg.get("text","") or ""

        # 1) EXPLICIT RESET
        if text.strip().lower() in ("/new", "start over", "new report"):
            session_data.pop(chat_id, None)
            send_telegram_message(chat_id,
                "🔄 Starting a fresh report. Please describe today’s site work."
            )
            return "reset", 200

        # 2) VOICE → TEXT
        if not text and msg.get("voice"):
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id, "⚠️ Couldn't understand audio. Please send again.")
                return "no transcription", 200

        print(f"📩 From {chat_id}: {text}")

        # 3) DATE‐BASED RESET
        import re
        m = re.search(r"\b(\d{1,2}-\d{1,2}-\d{4})\b", text)
        if m:
            new_date = m.group(1)
            old_date = session_data.get(chat_id,{}).get("structured_data",{}).get("date")
            if old_date and new_date != old_date:
                session_data.pop(chat_id, None)
                send_telegram_message(chat_id,
                    f"📅 Detected new date {new_date}. Starting a new report session."
                )
                # fall through to treat message as first of new session

        # 4) INIT SESSION
        if chat_id not in session_data:
            session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}

        stored = session_data[chat_id]["structured_data"]

        # 5) CORRECTION BRANCH
        if session_data[chat_id]["awaiting_correction"]:
            delta = apply_correction(stored, text)
            # merge delta
            for k,v in delta.items():
                stored[k] = v
            session_data[chat_id]["structured_data"] = stored
            session_data[chat_id]["awaiting_correction"] = True

            full = summarize_data(stored)
            send_telegram_message(chat_id,
                f"✅ Got it! Here’s the **full** updated report:\n\n{full}\n\n✅ Anything else to correct?"
            )
            return "corrected", 200

        # 6) INITIAL EXTRACTION
        extracted = extract_site_report(text)
        if not extracted.get("site_name"):
            send_telegram_message(chat_id,
                "⚠️ Sorry, I couldn't detect site info. Please try again."
            )
            return "missing fields", 200

        enriched = enrich_with_date(extracted)
        session_data[chat_id] = {"structured_data": enriched, "awaiting_correction": True}
        summary = summarize_data(enriched)
        send_telegram_message(chat_id,
            f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections."
        )
        return "extracted", 200

    except Exception as e:
        print("❌ Error in webhook:", e)
        return "error", 500

# GPT extraction prompt
gpt_prompt_template = """
You are an AI assistant extracting a construction site report. Only pull out fields explicitly mentioned.
Do NOT guess, infer, or supply any missing fields.

Return JSON with any of these (omit unmentioned):
- site_name
- segment
- category
- company: [ {"name":"..."} ]
- people:  [ {"name":"...","role":"..."} ]
- tools:   [ {"item":"...","company":"..."} ]
- service: [ {"task":"...","company":"..."} ]
- activities: [ "...", ... ]
- issues: [ {"description":"...","caused_by":"...","has_photo":true/false} ]
- time
- weather
- impression
- comments
- date (dd‑mm‑yyyy)
"""
