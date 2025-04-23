from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In-memory session store
session_data = {}  # { telegram_user_id: {"structured_data": {...}, "awaiting_correction": True/False} }

@app.route("/", methods=["GET"])
def index():
    return "Running", 200

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    resp = requests.post(url, json=payload)
    print("✅ Telegram response:", resp.status_code, resp.text)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    resp = requests.get(url).json()
    path = resp["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        audio = requests.get(audio_url).content
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"}
        ).json()
        return whisper.get("text", "")
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def summarize_data(d):
    parts = []
    parts.append(f"📍 Site: {d.get('site_name','')}")
    parts.append(f"📆 Segment: {d.get('segment','')}")
    parts.append(f"🌿 Category: {d.get('category','')}")
    comps = ", ".join(c.get("name","") for c in d.get("company",[]) if isinstance(c, dict))
    parts.append(f"🏣 Companies: {comps}")
    ppl = ", ".join(f'{p.get("name","")} ({p.get("role","")})' for p in d.get("people",[]) if isinstance(p, dict))
    parts.append(f"👷 People: {ppl}")
    svc = ", ".join(f'{s.get("task","")} ({s.get("company","")})' for s in d.get("service",[]) if isinstance(s, dict))
    parts.append(f"🔧 Services: {svc}")
    tls = ", ".join(f'{t.get("item","")} ({t.get("company","")})' for t in d.get("tools",[]) if isinstance(t, dict))
    parts.append(f"🛠️ Tools: {tls}")
    acts = ", ".join(d.get("activities",[]))
    parts.append(f"📋 Activities: {acts}")
    parts.append("⚠️ Issues:")
    for issue in d.get("issues",[]):
        if isinstance(issue, dict):
            cb = issue.get("caused_by","")
            photo = " 📸" if issue.get("has_photo") else ""
            parts.append(f"• {issue.get('description','')} (by {cb}){photo}")
    parts.append(f"⏰ Time: {d.get('time','')}")
    parts.append(f"🌦️ Weather: {d.get('weather','')}")
    parts.append(f"💬 Impression: {d.get('impression','')}")
    parts.append(f"📝 Comments: {d.get('comments','')}")
    parts.append(f"🗓️ Date: {d.get('date','')}")
    return "\n".join(parts)

def enrich_with_date(d):
    today = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today
    else:
        try:
            pd = datetime.strptime(d["date"], "%d-%m-%Y")
            if pd > datetime.now():
                d["date"] = today
        except:
            d["date"] = today
    return d

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role":"system","content":"Extract only explicitly mentioned fields."},
        {"role":"user","content":prompt}
    ]
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=msgs,
            temperature=0.2
        )
        return json.loads(res.choices[0].message.content.strip())
    except Exception as e:
        print("❌ GPT parse failed:", e)
        return {}

def apply_correction(orig, corr):
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nCorrection:\n" + corr +
        "\n\nReturn the full updated JSON."
    )
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(res.choices[0].message.content.strip())
    except Exception as e:
        print("❌ Correction parse failed:", e)
        return orig

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    msg = data.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "") or ""
    if not text and "voice" in msg:
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])

    # New/reset command
    if text.lower().strip() in ["new","reset","new report","start over"]:
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}
        blank = summarize_data({})
        send_telegram_message(chat_id, f"🔄 Starting new report:\n\n{blank}\n\n✅ Go ahead.")
        return "", 200

    # Initialize session
    if chat_id not in session_data:
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}

    # If awaiting further corrections
    if session_data[chat_id]["awaiting_correction"]:
        updated = apply_correction(session_data[chat_id]["structured_data"], text)
        session_data[chat_id]["structured_data"] = updated
        full = summarize_data(updated)
        send_telegram_message(chat_id, f"✅ Full updated report:\n\n{full}\n\n✅ Anything else?")
        return "", 200

    # First extraction
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id, "⚠️ I couldn't detect site info. Please try again.")
        return "", 200

    enriched = enrich_with_date(extracted)
    session_data[chat_id] = {"structured_data": enriched, "awaiting_correction": True}
    full = summarize_data(enriched)
    send_telegram_message(chat_id, f"Here’s what I understood:\n\n{full}\n\n✅ You can correct anytime.")
    return "", 200

# GPT prompt template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report.
Only extract fields explicitly mentioned; omit any not stated.

Return JSON with these keys (omit missing ones):
- site_name
- segment
- category
- company: [ {"name":...} ]
- people: [ {"name":...,"role":...} ]
- tools: [ {"item":...,"company":...} ]
- service: [ {"task":...,"company":...} ]
- activities: [ ... ]
- issues: [ {"description":...,"caused_by":...,"has_photo":true/false} ]
- time
- weather
- impression
- comments
- date (dd-mm-yyyy)
"""
