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
session_data = {}  # { telegram_user_id: {"structured_data": {...}, "awaiting_correction": True/False} }

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
        audio_url = get_telegram_file_path(file_id)
        audio_r = requests.get(audio_url)
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio_r.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper.json().get("text", "")
    except Exception:
        return ""

def summarize_data(d):
    lines = []
    if d.get("site_name"):
        lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):
        lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):
        lines.append(f"🌿 Category: {d['category']}")
    if isinstance(d.get("company"), list):
        lines.append("🏣 Companies: " + ", ".join(c["name"] for c in d["company"]))
    if isinstance(d.get("people"), list):
        lines.append("👷 People: " + ", ".join(f"{p['name']} ({p['role']})" for p in d["people"]))
    if isinstance(d.get("service"), list):
        lines.append("🔧 Services: " + ", ".join(f"{s['task']} ({s['company']})" for s in d["service"]))
    if isinstance(d.get("tools"), list):
        lines.append("🛠️ Tools: " + ", ".join(f"{t['item']} ({t['company']})" for t in d["tools"]))
    if isinstance(d.get("activities"), list):
        lines.append("📋 Activities: " + ", ".join(d["activities"]))
    if isinstance(d.get("issues"), list):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            desc = i.get("description","")
            by   = i.get("caused_by")
            photo = " 📸" if i.get("has_photo") else ""
            if by:
                lines.append(f"• {desc} (by {by}){photo}")
            else:
                lines.append(f"• {desc}{photo}")
    if d.get("time"):
        lines.append(f"⏰ Time: {d['time']}")
    if d.get("weather"):
        lines.append(f"🌦️ Weather: {d['weather']}")
    if d.get("impression"):
        lines.append(f"💬 Impression: {d['impression']}")
    if d.get("comments"):
        lines.append(f"📝 Comments: {d['comments']}")
    if d.get("date"):
        lines.append(f"🗓️ Date: {d['date']}")
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

def strip_code_fences(raw: str) -> str:
    return raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role":"system","content":"ONLY extract fields explicitly mentioned—do NOT guess or fill missing."},
        {"role":"user","content":prompt}
    ]
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=msgs,
        temperature=0.2
    )
    raw = resp.choices[0].message.content or ""
    clean = strip_code_fences(raw)
    try:
        return json.loads(clean)
    except:
        return {}

def apply_correction(original, correction_text):
    prompt = (
        "You are updating the JSON below per the user’s instruction. "
        "If they reference a field not present (e.g. time), ADD it. "
        "If they say delete or remove, do so.\n\n"
        f"Original JSON:\n{json.dumps(original,indent=2)}\n\n"
        f"Instruction:\n\"{correction_text}\"\n\n"
        "Return the **full** updated JSON only."
    )
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}],
        temperature=0.2
    )
    raw = resp.choices[0].message.content or ""
    clean = strip_code_fences(raw)
    try:
        return json.loads(clean)
    except:
        return original

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()
    msg = update.get("message", {})
    chat_id = str(msg.get("chat",{}).get("id",""))
    text = msg.get("text","") or ""

    # Voice → text
    if not text and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not text:
            send_telegram_message(chat_id, "⚠️ Couldn't understand audio.")
            return "no audio",200

    # Reset
    if text.lower() in ("/new","start over"):
        session_data.pop(chat_id,None)
        send_telegram_message(chat_id,"🔄 Starting fresh. Describe today’s site work.")
        return "reset",200

    if chat_id not in session_data:
        session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}

    stored = session_data[chat_id]["structured_data"]

    # Correction round
    if session_data[chat_id]["awaiting_correction"]:
        updated = apply_correction(stored, text)
        session_data[chat_id]["structured_data"] = updated
        session_data[chat_id]["awaiting_correction"] = True
        full = summarize_data(updated)
        send_telegram_message(chat_id,
            f"✅ Got it! Here’s the **full** updated report:\n\n{full}\n\n✅ Anything else to correct?"
        )
        return "corrected",200

    # First extraction
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id,"⚠️ Sorry, I couldn't detect site info. Try again.")
        return "missing",200

    enriched = enrich_with_date(extracted)
    session_data[chat_id] = {"structured_data":enriched, "awaiting_correction":True}
    summary = summarize_data(enriched)
    send_telegram_message(chat_id,
        f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections."
    )
    return "extracted",200

# Extraction prompt
gpt_prompt_template = """
Extract a construction site report as JSON from this user text.
⚠️ ONLY include fields explicitly mentioned. Do NOT guess or infer missing data.

Fields to return (omit unmentioned):
site_name, segment, category,
company:[{"name":""}], people:[{"name":"","role":""}],
tools:[{"item":"","company":""}], service:[{"task":"","company":""}],
activities:[...], issues:[{"description":"","caused_by":"","has_photo":true/false}],
time, weather, impression, comments, date (dd‑mm‑yyyy)
"""
