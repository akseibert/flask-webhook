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
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    resp = requests.post(url, json=payload)
    print("✅ Telegram response:", resp.status_code, resp.text)

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
            return ""
        whisper_resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio_resp.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper_resp.json().get("text", "")
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def summarize_data(d):
    lines = []
    lines.append(f"📍 Site: {d.get('site_name','')}")
    lines.append(f"📆 Segment: {d.get('segment','')}")
    lines.append(f"🌿 Category: {d.get('category','')}")
    companies = ", ".join(c.get("name","") for c in d.get("company",[]) if isinstance(c,dict))
    lines.append(f"🏣 Companies: {companies}")
    people = ", ".join(f\"{p.get('name','')} ({p.get('role','')})\" for p in d.get("people",[]) if isinstance(p,dict))
    lines.append(f"👷 People: {people}")
    services = ", ".join(f\"{s.get('task','')} ({s.get('company','')})\" for s in d.get("service",[]) if isinstance(s,dict))
    lines.append(f"🔧 Services: {services}")
    tools = ", ".join(f\"{t.get('item','')} ({t.get('company','')})\" for t in d.get("tools",[]) if isinstance(t,dict))
    lines.append(f"🛠️ Tools: {tools}")
    lines.append(f"📋 Activities: {', '.join(d.get('activities',[]))}")
    lines.append("⚠️ Issues:")
    for i in d.get("issues",[]):
        if isinstance(i,dict):
            lines.append(f"• {i.get('description','')} (by {i.get('caused_by','')}){' 📸' if i.get('has_photo') else ''}")
    lines.append(f"⏰ Time: {d.get('time','')}")
    lines.append(f"🌦️ Weather: {d.get('weather','')}")
    lines.append(f"💬 Impression: {d.get('impression','')}")
    lines.append(f"📝 Comments: {d.get('comments','')}")
    lines.append(f"🗓️ Date: {d.get('date','')}")
    return "\n".join(lines)

def enrich_with_date(d):
    today_str = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today_str
    else:
        try:
            parsed = datetime.strptime(d["date"],"%d-%m-%Y")
            if parsed > datetime.now():
                d["date"] = today_str
        except:
            d["date"] = today_str
    return d

def extract_site_report(text):
    prompt = gpt_prompt_template + f"\n{text}"
    msgs = [
        {"role":"system","content":"You are strict. Only extract fields explicitly mentioned."},
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
        print("❌ GPT parse failed:",e)
        return {}

def apply_correction(orig, corr_text):
    prompt = (
        f"You are correcting JSON. Original:\n{json.dumps(orig)}\n\n"
        f"Correction request:\n{corr_text}\n\n"
        "Return full updated JSON."
    )
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(res.choices[0].message.content.strip())
    except Exception as e:
        print("❌ Correction failed:",e)
        return orig

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    msg = data.get("message",{})
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text","")
    if not text and "voice" in msg:
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])

    print(f"📩 From {chat_id}: {text}")

    # New session trigger
    if text.lower().strip() in ["new","reset","new report","start over"]:
        session_data[chat_id] = {"structured_data":{},"awaiting_correction":False}
        blank = summarize_data({})
        send_telegram_message(chat_id,f"🔄 Starting new report:\n\n{blank}\n\n✅ Go ahead.")
        return "reset",200

    # initialize if needed
    if chat_id not in session_data:
        session_data[chat_id] = {"structured_data":{},"awaiting_correction":False}

    if session_data[chat_id]["awaiting_correction"]:
        updated = apply_correction(session_data[chat_id]["structured_data"],text)
        session_data[chat_id]["structured_data"] = updated
        # keep awaiting for further corrections
        full = summarize_data(updated)
        send_telegram_message(chat_id,f"✅ Got it! Here’s the **full** updated report:\n\n{full}\n\n✅ Anything else?")
        return "corrected",200

    # first-time extract
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id,"⚠️ I couldn't detect site info. Please try again.")
        return "missing",200

    enriched = enrich_with_date(extracted)
    session_data[chat_id] = {"structured_data":enriched,"awaiting_correction":True}
    full = summarize_data(enriched)
    send_telegram_message(chat_id,f"Here’s what I understood:\n\n{full}\n\n✅ Is this correct? You can correct anytime.")
    return "done",200

# GPT prompt template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report. 
Only extract fields explicitly mentioned. Omit any not stated.

Return JSON with:
- site_name
- segment
- category
- company: list of {{'name':...}}
- people: list of {{'name':...,'role':...}}
- tools: list of {{'item':...,'company':...}}
- service: list of {{'task':...,'company':...}}
- activities: list of strings
- issues: list of {{'description':...,'caused_by':...,'has_photo':true/false}}
- time
- weather
- impression
- comments
- date (dd-mm-yyyy)
"""
