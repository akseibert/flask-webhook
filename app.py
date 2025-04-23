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
session_data = {}  # { chat_id: { "structured_data": {...}, "awaiting_correction": bool } }

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    return f"https://api.telegram.org/file/bot{token}/{r.json()['result']['file_path']}"

def transcribe_from_telegram_voice(file_id):
    url = get_telegram_file_path(file_id)
    audio = requests.get(url).content
    resp = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
        files={"file": ("voice.ogg", audio, "audio/ogg")},
        data={"model": "whisper-1"}
    )
    return resp.json().get("text", "")

def enrich_with_date(data):
    today = datetime.now().strftime("%d-%m-%Y")
    d = data.get("date", "")
    try:
        if not d or datetime.strptime(d, "%d-%m-%Y") > datetime.now():
            data["date"] = today
    except:
        data["date"] = today
    return data

def flatten_lists(data):
    # Turn list fields into comma-joined strings
    for fld in ("time", "weather", "activities", "comments", "impression"):
        if fld in data and isinstance(data[fld], list):
            data[fld] = ", ".join(data[fld])
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role": "system", "content": "Only extract explicitly mentioned fields; never guess."},
        {"role": "user",   "content": prompt}
    ]
    try:
        r = client.chat.completions.create(model="gpt-3.5-turbo", messages=msgs, temperature=0.2)
        out = json.loads(r.choices[0].message.content)
        return flatten_lists(out)
    except:
        return {}

def apply_correction(original, correction_text):
    prompt = (
        "You are correcting structured site data. Original JSON:\n"
        f"{json.dumps(original)}\n\n"
        f"User says: {correction_text}\n\n"
        "Return ONLY the fields that changed."
    )
    try:
        r = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        out = json.loads(r.choices[0].message.content)
        return flatten_lists(out)
    except:
        return {}

def summarize_data(d: dict) -> str:
    lines = []
    if d.get("site_name"):   lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):     lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):    lines.append(f"🌿 Category: {d['category']}")
    if d.get("company"):
        comps = [c["name"] for c in d["company"] if isinstance(c, dict) and c.get("name")]
        if comps: lines.append(f"🏣 Companies: {', '.join(comps)}")
    if d.get("people"):
        ppl = []
        for p in d["people"]:
            if p.get("name"):
                role = p.get("role","")
                ppl.append(f"{p['name']} ({role})" if role else p["name"])
        if ppl: lines.append(f"👷 People: {', '.join(ppl)}")
    if d.get("service"):
        svcs = []
        for s in d["service"]:
            t = s.get("task","")
            c = s.get("company","")
            svcs.append(f"{t} ({c})" if c else t)
        if svcs: lines.append(f"🔧 Services: {', '.join(svcs)}")
    if d.get("tools"):
        tls = []
        for t in d["tools"]:
            i = t.get("item","")
            c = t.get("company","")
            tls.append(f"{i} ({c})" if c else i)
        if tls: lines.append(f"🛠️ Tools: {', '.join(tls)}")
    if d.get("activities"): lines.append(f"📋 Activities: {d['activities']}")
    if d.get("issues"):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            desc = i.get("description","")
            cb   = i.get("caused_by","")
            ph   = " 📸" if i.get("has_photo") else ""
            lines.append(f"• {desc}" + (f" (by {cb})" if cb else "") + ph)
    if d.get("time"):       lines.append(f"⏰ Time: {d['time']}")
    if d.get("weather"):    lines.append(f"🌦️ Weather: {d['weather']}")
    if d.get("impression"): lines.append(f"💬 Impression: {d['impression']}")
    if d.get("comments"):   lines.append(f"📝 Comments: {d['comments']}")
    if d.get("date"):       lines.append(f"🗓️ Date: {d['date']}")
    return "\n".join(lines)

def blank_template():
    today = datetime.now().strftime("%d-%m-%Y")
    return (
        "🆕 Starting a fresh report:\n\n"
        "📍 Site: \n"
        "📆 Segment: \n"
        "🌿 Category: \n"
        "🏣 Companies: \n"
        "👷 People: \n"
        "🔧 Services: \n"
        "🛠️ Tools: \n"
        "📋 Activities: \n"
        "⚠️ Issues: \n"
        "⏰ Time: \n"
        "🌦️ Weather: \n"
        "💬 Impression: \n"
        "📝 Comments: \n"
        f"🗓️ Date: {today}\n\n"
        "✅ Send your first field or voice message."
    )

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    msg  = data.get("message", {})
    chat = str(msg.get("chat",{}).get("id",""))
    txt  = (msg.get("text") or "").strip()

    # Reset / New
    if txt.lower() in ("new","/new","reset","new report"):
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}
        send_telegram_message(chat, blank_template())
        return "", 200

    # Voice → text
    if not txt and msg.get("voice"):
        txt = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not txt:
            send_telegram_message(chat, "❌ Could not transcribe audio. Please try again.")
            return "", 200

    # Ensure session exists
    if chat not in session_data:
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}
    sess = session_data[chat]

    # Correction flow
    if sess["awaiting_correction"]:
        original = sess["structured_data"]
        updated_fields = apply_correction(original, txt)
        # Merge corrected fields back in
        merged = {**original, **updated_fields}
        enriched = enrich_with_date(merged)
        sess["structured_data"] = enriched

        full = summarize_data(enriched)
        send_telegram_message(
            chat,
            f"✅ Got it! Here’s the **full** updated report:\n\n{full}\n\n✅ Anything else to correct?"
        )
        return "", 200

    # Initial extraction
    extracted = extract_site_report(txt)
    if not extracted.get("site_name"):
        send_telegram_message(chat, "❌ Sorry, I couldn't detect site info. Please try again.")
        return "", 200

    enriched = enrich_with_date(extracted)
    sess["structured_data"]    = enriched
    sess["awaiting_correction"] = True

    full = summarize_data(enriched)
    send_telegram_message(
        chat,
        f"Here’s what I understood:\n\n{full}\n\n✅ Is this correct? Send any corrections now."
    )
    return "", 200

# GPT prompt template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report from summary text.

⚠️ Only extract fields explicitly mentioned. Do NOT guess or fill missing info.

Return JSON with any of these keys (omit unmentioned):
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
- date (dd-mm-yyyy)
"""
