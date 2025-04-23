from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client (new SDK style)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In-memory session store
# { telegram_user_id: { "structured_data": {...}, "awaiting_correction": bool } }
session_data = {}

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        r = requests.get(audio_url)
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", r.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper.json().get("text", "")
    except:
        return ""

def enrich_with_date(data):
    today = datetime.now().strftime("%d-%m-%Y")
    # if no date or future date, set to today
    d = data.get("date", "")
    try:
        if not d or datetime.strptime(d, "%d-%m-%Y") > datetime.now():
            data["date"] = today
    except:
        data["date"] = today
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    messages = [
        {"role": "system", "content": "You ONLY extract fields explicitly mentioned; never guess."},
        {"role": "user", "content": prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        return json.loads(resp.choices[0].message.content)
    except:
        return {}

def apply_correction(original, correction_text):
    prompt = (
        "You are correcting structured site data. Original JSON:\n"
        f"{json.dumps(original)}\n\n"
        f"User correction: {correction_text}\n\n"
        "Return only the updated JSON with corrected fields."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(resp.choices[0].message.content)
    except:
        return original

def summarize_data(d: dict) -> str:
    lines = []
    if d.get("site_name"):
        lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):
        lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):
        lines.append(f"🌿 Category: {d['category']}")

    comps = [c["name"] for c in d.get("company", []) if isinstance(c, dict) and c.get("name")]
    if comps:
        lines.append(f"🏣 Companies: {', '.join(comps)}")

    ppl = []
    for p in d.get("people", []):
        if isinstance(p, dict) and p.get("name"):
            name = p["name"]
            role = p.get("role","")
            ppl.append(f"{name} ({role})" if role else name)
    if ppl:
        lines.append(f"👷 People: {', '.join(ppl)}")

    serv = []
    for s in d.get("service", []):
        if isinstance(s, dict) and s.get("task"):
            task = s["task"]
            comp = s.get("company","")
            serv.append(f"{task} ({comp})" if comp else task)
    if serv:
        lines.append(f"🔧 Services: {', '.join(serv)}")

    tools = []
    for t in d.get("tools", []):
        if isinstance(t, dict) and t.get("item"):
            item = t["item"]
            comp = t.get("company","")
            tools.append(f"{item} ({comp})" if comp else item)
    if tools:
        lines.append(f"🛠️ Tools: {', '.join(tools)}")

    if d.get("activities"):
        lines.append(f"📋 Activities: {', '.join(d['activities'])}")

    issues = d.get("issues", [])
    if issues:
        lines.append("⚠️ Issues:")
        for i in issues:
            if isinstance(i, dict) and i.get("description"):
                desc = i["description"]
                cb   = i.get("caused_by","")
                ph   = " 📸" if i.get("has_photo") else ""
                line = f"• {desc}" + (f" (by {cb})" if cb else "") + ph
                lines.append(line)

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

def blank_template():
    today = datetime.now().strftime("%d-%m-%Y")
    template = (
        "🆕 Starting a fresh report. Here’s the blank template:\n\n"
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
        "✅ You can now speak or type your first field."
    )
    return template

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    msg = data.get("message", {})
    chat_id = str(msg.get("chat",{}).get("id",""))
    text = msg.get("text","").strip().lower()

    # Reset command
    if text in ("new", "/new", "reset", "new report"):
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}
        send_telegram_message(chat_id, blank_template())
        return "reset", 200

    # Voice handling
    if not msg.get("text") and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not text:
            send_telegram_message(chat_id, "❌ Could not transcribe audio. Please try again.")
            return "no audio", 200

    # Ensure session exists
    if chat_id not in session_data:
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}

    session = session_data[chat_id]

    # Correction flow
    if session["awaiting_correction"]:
        updated = apply_correction(session["structured_data"], text)
        enriched = enrich_with_date(updated)
        session["structured_data"] = enriched
        # keep awaiting_correction = True to allow multiple corrections
        full = summarize_data(enriched)
        send_telegram_message(
            chat_id,
            f"✅ Got it! Here’s the **full** updated report:\n\n{full}\n\n✅ Anything else to correct?"
        )
        return "corrected", 200

    # Initial extract flow
    extracted = extract_site_report(text)
    if not extracted or "site_name" not in extracted:
        send_telegram_message(chat_id, "❌ Sorry, I couldn't detect site info. Please try again.")
        return "failed extract", 200

    enriched = enrich_with_date(extracted)
    session["structured_data"] = enriched
    session["awaiting_correction"] = True
    full = summarize_data(enriched)
    send_telegram_message(
        chat_id,
        f"Here’s what I understood:\n\n{full}\n\n✅ Is this correct? You can send corrections now."
    )
    return "extracted", 200

# GPT prompt template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report from a spoken or written summary.

⚠️ Only extract fields that are explicitly mentioned. Do NOT guess or infer missing information.
Return JSON with any of these keys (omit unmentioned):
- site_name
- segment
- category
- company (list of {name:…})
- people (list of {name:…, role:…})
- tools (list of {item:…, company:…})
- service (list of {task:…, company:…})
- activities (list of strings)
- issues (list of {description:…, caused_by:…, has_photo:true/false})
- time
- weather
- impression
- comments
- date (dd-mm-yyyy)
"""
