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
# { chat_id: {"structured_data": {...}, "awaiting_correction": bool} }
session_data = {}

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤 Sending to Telegram:", url)
    print("📤 Payload:", json.dumps(payload, indent=2))
    resp = requests.post(url, json=payload)
    print("✅ Telegram response:", resp.status_code, resp.text)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    resp = requests.get(url)
    file_path = resp.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        r = requests.get(audio_url)
        if r.status_code != 200:
            print("❌ Failed to fetch audio from Telegram")
            return ""
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", r.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper.json().get("text", "")
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def summarize_data(d):
    lines = []
    if d.get("site_name"):
        lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):
        lines.append(f"📆 Segment: {d['segment']}")
    # category may be blank
    cat = d.get("category", "").strip()
    lines.append(f"🌿 Category: {cat}" if cat else "🌿 Category: ")
    # companies
    comps = [c.get("name","") for c in d.get("company",[]) if isinstance(c, dict)]
    lines.append("🏣 Companies: " + ", ".join(comps))
    # people
    ppl = []
    for p in d.get("people", []):
        if not isinstance(p, dict): continue
        name = p.get("name","")
        role = p.get("role","")
        ppl.append(f"{name} ({role})" if role else name)
    lines.append("👷 People: " + ", ".join(ppl))
    # tools
    tools = []
    for t in d.get("tools", []):
        if not isinstance(t, dict): continue
        item = t.get("item","")
        comp = t.get("company","")
        tools.append(f"{item} ({comp})" if comp else item)
    lines.append("🛠️ Tools: " + ", ".join(tools))
    # services
    svcs = []
    for s in d.get("service", []):
        if not isinstance(s, dict): continue
        task = s.get("task","")
        comp = s.get("company","")
        svcs.append(f"{task} ({comp})" if comp else task)
    lines.append("🔧 Services: " + ", ".join(svcs))
    # activities
    acts = d.get("activities", [])
    lines.append("📋 Activities: " + ", ".join(acts))
    # issues
    issues = d.get("issues", [])
    if issues:
        lines.append("⚠️ Issues:")
        for i in issues:
            if not isinstance(i, dict): continue
            desc = i.get("description","")
            cause = i.get("caused_by","")
            photo = " 📸" if i.get("has_photo") else ""
            lines.append(f"• {desc}" + (f" (by {cause})" if cause else "") + photo)
    else:
        lines.append("⚠️ Issues: ")
    # time, weather, impression, comments
    lines.append(f"⏰ Time: {d.get('time','')}")
    lines.append(f"🌦️ Weather: {d.get('weather','')}")
    lines.append(f"💬 Impression: {d.get('impression','')}")
    lines.append(f"📝 Comments: {d.get('comments','')}")
    # date
    lines.append(f"🗓️ Date: {d.get('date','')}")
    return "\n".join(lines)

def enrich_with_date(d):
    today = datetime.now().strftime("%d-%m-%Y")
    dt = d.get("date","").strip()
    if not dt:
        d["date"] = today
    else:
        try:
            parsed = datetime.strptime(dt, "%d-%m-%Y")
            if parsed > datetime.now():
                d["date"] = today
        except:
            d["date"] = today
    return d

def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role":"system","content":"You ONLY extract explicitly mentioned fields; never guess or fill defaults."},
        {"role":"user","content":prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=msgs,
            temperature=0.2
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

def apply_correction(orig, corr_text):
    prompt = (
        "You are helping correct a JSON site report. "
        "Original JSON:\n" + json.dumps(orig) +
        "\nUser said:\n" + corr_text +
        "\nReturn the updated JSON with only the changed fields."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ Correction GPT failed:", e)
        return orig

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("📩 Telegram update:", json.dumps(data))
    msg = data.get("message") or {}
    chat_id = str(msg.get("chat",{}).get("id",""))
    if not chat_id:
        return "no chat", 400

    # 1) get raw text from text or voice
    text = msg.get("text","") or ""
    if not text and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
    if not text:
        send_telegram_message(chat_id, "⚠️ I didn't see any text or couldn't transcribe the audio. Try again.")
        return "no content", 200

    # detect reset commands
    if text.strip().lower() in ("new report","new","reset","start again"):
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}
        blank = (
            "🔄 Starting a fresh report. Here's the blank template:\n\n"
            "📍 Site: \n"
            "📆 Segment: \n"
            "🌿 Category: \n"
            "🏣 Companies: \n"
            "👷 People: \n"
            "🛠️ Tools: \n"
            "🔧 Services: \n"
            "📋 Activities: \n"
            "⚠️ Issues: \n"
            "⏰ Time: \n"
            "🌦️ Weather: \n"
            "💬 Impression: \n"
            "📝 Comments: \n"
            f"🗓️ Date: {datetime.now().strftime('%d-%m-%Y')}\n\n"
            "✅ You can now speak or type your first field."
        )
        send_telegram_message(chat_id, blank)
        return "reset", 200

    # init session if needed
    sess = session_data.setdefault(chat_id, {"structured_data": {}, "awaiting_correction": False})

    # 2) correction flow
    if sess["awaiting_correction"]:
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = updated
        # stay in correction mode
        summary = summarize_data(updated)
        send_telegram_message(
            chat_id,
            f"✅ Got it! Here’s the **full** updated report:\n\n{summary}\n\n✅ Anything else to correct?"
        )
        return "corrected", 200

    # 3) new‐extraction flow
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id, "⚠️ Sorry, I couldn’t detect your site name. Please try again.")
        return "retry", 200

    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True
    summary = summarize_data(enriched)
    send_telegram_message(
        chat_id,
        f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can reply with corrections or say “new report” to reset."
    )
    return "extracted", 200

# GPT extraction prompt template
gpt_prompt_template = """
You are an AI assistant extracting a construction‐site report from a manager’s summary.
⚠️ Only extract fields explicitly mentioned; do NOT guess or fill defaults.
Return a JSON with only the fields mentioned:
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
