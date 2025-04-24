from flask import Flask, request
import requests
import os
import json
import re
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
    return resp.json()["result"]["file_path"]


def transcribe_from_telegram_voice(file_id):
    try:
        path = get_telegram_file_path(file_id)
        audio_url = f"https://api.telegram.org/file/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/{path}"
        r = requests.get(audio_url)
        if r.status_code != 200:
            print("❌ Failed to fetch audio")
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


def clean_json_reply(raw: str) -> str:
    """
    Strip markdown fences and any non-JSON prefix/suffix so we can safely json.loads().
    """
    # grab content inside ```json ... ```
    m = re.search(r"```json(.*?)```", raw, re.S)
    if m:
        raw = m.group(1)
    # strip any leading/trailing backticks or whitespace
    raw = raw.strip("`\n ")
    return raw


def summarize_data(d):
    lines = []
    if d.get("site_name"):
        lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):
        lines.append(f"📆 Segment: {d['segment']}")
    # Category always shown
    cat = d.get("category","").strip()
    lines.append(f"🌿 Category: {cat}" if cat else "🌿 Category: ")
    # Companies
    comps = []
    for c in d.get("company", []):
        if isinstance(c, dict):
            name = c.get("name","").strip()
            if name: comps.append(name)
        elif isinstance(c, str) and c.strip():
            comps.append(c.strip())
    lines.append("🏣 Companies: " + ", ".join(comps))
    # People
    ppl = []
    for p in d.get("people", []):
        if isinstance(p, dict):
            name = p.get("name","").strip()
            role = p.get("role","").strip()
            if name:
                ppl.append(f"{name} ({role})" if role else name)
        elif isinstance(p, str) and p.strip():
            ppl.append(p.strip())
    lines.append("👷 People: " + ", ".join(ppl))
    # Tools
    tools = []
    for t in d.get("tools", []):
        if isinstance(t, dict):
            item = t.get("item","").strip()
            comp = t.get("company","").strip()
            if item:
                tools.append(f"{item} ({comp})" if comp else item)
        elif isinstance(t, str) and t.strip():
            tools.append(t.strip())
    lines.append("🛠️ Tools: " + ", ".join(tools))
    # Services
    svcs = []
    for s in d.get("service", []):
        if isinstance(s, dict):
            task = s.get("task","").strip()
            comp = s.get("company","").strip()
            if task:
                svcs.append(f"{task} ({comp})" if comp else task)
        elif isinstance(s, str) and s.strip():
            svcs.append(s.strip())
    lines.append("🔧 Services: " + ", ".join(svcs))
    # Activities
    acts = [a for a in d.get("activities", []) if isinstance(a, str)]
    lines.append("📋 Activities: " + ", ".join(acts))
    # Issues
    issues = d.get("issues", [])
    if issues:
        lines.append("⚠️ Issues:")
        for i in issues:
            if isinstance(i, dict):
                desc = i.get("description","").strip()
                cause = i.get("caused_by","").strip()
                photo = " 📸" if i.get("has_photo") else ""
                lines.append(
                    f"• {desc}" + (f" (by {cause})" if cause else "") + photo
                )
    else:
        lines.append("⚠️ Issues: ")
    # Time/Weather/Impression/Comments
    lines.append(f"⏰ Time: {d.get('time','')}")
    lines.append(f"🌦️ Weather: {d.get('weather','')}")
    lines.append(f"💬 Impression: {d.get('impression','')}")
    lines.append(f"📝 Comments: {d.get('comments','')}")
    # Date (always present)
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
        {"role":"system","content":"You only extract fields explicitly mentioned; never guess."},
        {"role":"user","content":prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=msgs,
            temperature=0.2
        )
        raw = resp.choices[0].message.content
        clean = clean_json_reply(raw)
        return json.loads(clean)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}


def apply_correction(orig, corr_text):
    prompt = (
        "Correct this JSON report:\n"
        f"{json.dumps(orig)}\n"
        "User said:\n"
        f"{corr_text}\n"
        "Return only the updated JSON."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2
        )
        raw = resp.choices[0].message.content
        clean = clean_json_reply(raw)
        updated = json.loads(clean)
        # always re‐enrich the date
        return enrich_with_date(updated)
    except Exception as e:
        print("❌ Correction GPT failed:", e)
        return orig


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("📩 Telegram update:", json.dumps(data, indent=2))
    msg = data.get("message", {})
    chat_id = str(msg.get("chat",{}).get("id",""))
    if not chat_id:
        return "no chat", 400

    # get text or transcribe voice
    text = msg.get("text","") or ""
    if not text and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
    if not text:
        send_telegram_message(chat_id, "⚠️ I didn’t catch any text or audio. Try again.")
        return "no content", 200

    tl = text.strip().lower()
    # RESET
    if tl in ("new","/new","reset","/reset","new report","start","/start"):
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}
        blank = (
            "🔄 Starting a fresh report:\n\n"
            "📍 Site: \n📆 Segment: \n🌿 Category: \n"
            "🏣 Companies: \n👷 People: \n🛠️ Tools: \n"
            "🔧 Services: \n📋 Activities: \n⚠️ Issues: \n"
            "⏰ Time: \n🌦️ Weather: \n💬 Impression: \n"
            "📝 Comments: \n"
            f"🗓️ Date: {datetime.now().strftime('%d-%m-%Y')}\n\n"
            "✅ You can now speak or type your first field."
        )
        send_telegram_message(chat_id, blank)
        return "reset", 200

    sess = session_data.setdefault(chat_id, {"structured_data": {}, "awaiting_correction": False})

    # CORRECTION
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

    # FIRST‐PASS EXTRACTION
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id, "⚠️ Sorry, I couldn’t detect the site name. Please try again.")
        return "retry", 200

    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True
    summary = summarize_data(enriched)
    send_telegram_message(
        chat_id,
        f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can now reply with corrections."
    )
    return "extracted", 200


# GPT prompt skeleton
gpt_prompt_template = """
You are an AI assistant extracting a construction‐site report.
Only pull out fields explicitly mentioned; never invent or fill in defaults.
Return JSON with exactly these keys (omit any you don’t see):
- site_name
- segment
- category
- company: [ {name: ...} ]
- people: [ {name: ..., role: ...} ]
- tools: [ {item: ..., company: ...} ]
- service: [ {task: ..., company: ...} ]
- activities: [ string ]
- issues: [ {description: ..., caused_by: ..., has_photo: true/false} ]
- time
- weather
- impression
- comments
- date (dd-mm-yyyy)
"""
