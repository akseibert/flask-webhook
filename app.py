from flask import Flask, request
import requests
import os
import json
import re
from datetime import datetime
import openai

# Initialize OpenAI client
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In‐memory sessions
session_data = {}  # chat_id → {"structured_data": {...}, "awaiting_correction": bool}


def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤 Sending to Telegram:", url)
    print("📤 Payload:", payload)
    r = requests.post(url, json=payload)
    print("✅ Telegram response:", r.status_code, r.text)


def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    r = requests.get(url).json()
    return r["result"]["file_path"]


def transcribe_from_telegram_voice(file_id):
    try:
        path = get_telegram_file_path(file_id)
        audio_url = f"https://api.telegram.org/file/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/{path}"
        r = requests.get(audio_url)
        if r.status_code != 200:
            return ""
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", r.content, "audio/ogg")},
            data={"model": "whisper-1"}
        ).json()
        return whisper.get("text", "") or ""
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""


def clean_json_reply(raw: str) -> str:
    m = re.search(r"```json(.*?)```", raw, re.S)
    if m:
        raw = m.group(1)
    return raw.strip("`\n ")


def to_list(val):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def summarize_data(d):
    # Helper to coerce each field
    lines = []

    # Always show site_name, even if blank
    lines.append(f"📍 Site: {d.get('site_name','') or ''}")
    lines.append(f"📆 Segment: {d.get('segment','') or ''}")
    lines.append(f"🌿 Category: {d.get('category','') or ''}")

    # Companies
    comps = []
    for c in to_list(d.get("company")):
        if isinstance(c, dict) and c.get("name"):
            comps.append(c["name"])
        elif isinstance(c, str):
            comps.append(c)
    lines.append("🏣 Companies: " + ", ".join(comps))

    # People
    ppl = []
    for p in to_list(d.get("people")):
        if isinstance(p, dict) and p.get("name"):
            role = p.get("role","")
            ppl.append(f"{p['name']} ({role})" if role else p["name"])
        elif isinstance(p, str):
            ppl.append(p)
    lines.append("👷 People: " + ", ".join(ppl))

    # Tools
    tools = []
    for t in to_list(d.get("tools")):
        if isinstance(t, dict) and t.get("item"):
            comp = t.get("company","")
            tools.append(f"{t['item']} ({comp})" if comp else t["item"])
        elif isinstance(t, str):
            tools.append(t)
    lines.append("🛠️ Tools: " + ", ".join(tools))

    # Services
    svcs = []
    for s in to_list(d.get("service")):
        if isinstance(s, dict) and s.get("task"):
            comp = s.get("company","")
            svcs.append(f"{s['task']} ({comp})" if comp else s["task"])
        elif isinstance(s, str):
            svcs.append(s)
    lines.append("🔧 Services: " + ", ".join(svcs))

    # Activities
    acts = [a for a in to_list(d.get("activities")) if isinstance(a, str)]
    lines.append("📋 Activities: " + ", ".join(acts))

    # Issues
    iss = to_list(d.get("issues"))
    if iss:
        lines.append("⚠️ Issues:")
        for i in iss:
            if isinstance(i, dict) and i.get("description"):
                desc = i["description"]
                cause = i.get("caused_by","")
                photo = " 📸" if i.get("has_photo") else ""
                lines.append(f"• {desc}" + (f" (by {cause})" if cause else "") + photo)
    else:
        lines.append("⚠️ Issues: ")

    # Time / Weather / Impression / Comments
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
            if datetime.strptime(dt, "%d-%m-%Y") > datetime.now():
                d["date"] = today
        except:
            d["date"] = today
    return d


def extract_site_report(text):
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role":"system","content":"Only extract explicitly mentioned fields; never guess."},
        {"role":"user","content":prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=msgs, temperature=0.2
        )
        raw = resp.choices[0].message.content
        clean = clean_json_reply(raw)
        return json.loads(clean)
    except Exception as e:
        print("❌ GPT parse fail:", e)
        return {}


def apply_correction(orig, corr_text):
    prompt = (
        "Correct this JSON:\n"
        f"{json.dumps(orig)}\n"
        "User correction:\n"
        f"{corr_text}\n"
        "Return only the updated JSON."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2
        )
        clean = clean_json_reply(resp.choices[0].message.content)
        updated = json.loads(clean)
        return enrich_with_date(updated)
    except Exception as e:
        print("❌ Correction parse fail:", e)
        return orig


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("📩 Update:", json.dumps(data, indent=2))
    msg = data.get("message", {})
    chat_id = str(msg.get("chat",{}).get("id",""))
    if not chat_id:
        return "no chat id", 400

    # get text or transcribe voice
    text = msg.get("text","") or ""
    if not text and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
    if not text:
        send_telegram_message(chat_id, "⚠️ I didn’t catch text or voice. Try again.")
        return "no content", 200

    cmd = text.strip().lower()
    # RESET / NEW
    if cmd in ("new","/new","reset","/reset","start","/start","new report"):
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}
        blank = (
            "🔄 **Starting a fresh report**\n\n"
            "📍 Site: \n📆 Segment: \n🌿 Category: \n"
            "🏣 Companies: \n👷 People: \n🛠️ Tools: \n"
            "🔧 Services: \n📋 Activities: \n⚠️ Issues: \n"
            "⏰ Time: \n🌦️ Weather: \n💬 Impression: \n"
            "📝 Comments: \n"
            f"🗓️ Date: {datetime.now().strftime('%d-%m-%Y')}\n\n"
            "✅ Now please speak or type your first field."
        )
        send_telegram_message(chat_id, blank)
        return "reset", 200

    sess = session_data.setdefault(chat_id, {"structured_data": {}, "awaiting_correction": False})

    # CORRECTION MODE
    if sess["awaiting_correction"]:
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = updated
        summary = summarize_data(updated)
        send_telegram_message(
            chat_id,
            f"✅ Got it! Here’s the **full** updated report:\n\n{summary}\n\n✅ Anything else to correct?"
        )
        return "corrected", 200

    # FIRST PASS EXTRACTION
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id, "⚠️ Couldn’t detect `site_name`. Please try again.")
        return "retry", 200

    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True
    summary = summarize_data(enriched)
    send_telegram_message(
        chat_id,
        f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can reply with corrections."
    )
    return "extracted", 200


# GPT prompt skeleton
gpt_prompt_template = """
You are an AI assistant extracting a construction‐site report.
Only pull out fields explicitly mentioned; never invent or guess.
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
