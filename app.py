from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# Per-user in-progress report
session_data = {}  # chat_id → {"structured_data": {...}, "awaiting_correction": bool}

# Health check
@app.route("/", methods=["GET"])
def index():
    return "Running", 200

def send_telegram_message(chat_id: str, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text})
    print("→ Telegram send:", resp.status_code, resp.text)

def get_telegram_file_path(file_id: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}").json()
    path = r["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_from_telegram_voice(file_id: str) -> str:
    try:
        url = get_telegram_file_path(file_id)
        audio = requests.get(url).content
        w = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"}
        ).json()
        return w.get("text", "")
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def summarize_data(d: dict) -> str:
    # Join helper
    def join_list(key, fmt):
        return fmt.format(", ".join(key)) if key else fmt.format("")
    lines = [
        f"📍 Site: {d.get('site_name','')}",
        f"📆 Segment: {d.get('segment','')}",
        f"🌿 Category: {d.get('category','')}",
        f"🏣 Companies: {', '.join(c.get('name','') for c in d.get('company',[]))}",
        f"👷 People: {', '.join(f\"{p.get('name','')} ({p.get('role','')})\" for p in d.get('people',[]))}",
        f"🔧 Services: {', '.join(f\"{s.get('task','')} ({s.get('company','')})\" for s in d.get('service',[]))}",
        f"🛠️ Tools: {', '.join(f\"{t.get('item','')} ({t.get('company','')})\" for t in d.get('tools',[]))}",
        f"📋 Activities: {', '.join(d.get('activities',[]))}"
    ]
    if d.get("issues"):
        lines.append("⚠️ Issues:")
        for issue in d["issues"]:
            desc = issue.get("description","")
            cb   = issue.get("caused_by","")
            ph   = " 📸" if issue.get("has_photo") else ""
            lines.append(f"• {desc} (by {cb}){ph}")
    lines += [
        f"⏰ Time: {d.get('time','')}",
        f"🌦️ Weather: {d.get('weather','')}",
        f"💬 Impression: {d.get('impression','')}",
        f"📝 Comments: {d.get('comments','')}",
        f"🗓️ Date: {d.get('date','')}"
    ]
    return "\n".join(lines)

def enrich_with_date(d: dict) -> dict:
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

def extract_site_report(text: str) -> dict:
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role":"system","content":"Extract only explicitly mentioned fields; omit anything else."},
        {"role":"user","content":prompt}
    ]
    try:
        r = client.chat.completions.create(model="gpt-3.5-turbo", messages=msgs, temperature=0.2)
        return json.loads(r.choices[0].message.content.strip())
    except Exception as e:
        print("❌ GPT extract failed:", e)
        return {}

def apply_correction_gpt(orig: dict, corr: str) -> dict:
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nUser correction:\n" + corr +
        "\n\nReturn the **entire** updated JSON only."
    )
    try:
        r = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(r.choices[0].message.content.strip())
    except Exception as e:
        print("❌ GPT correction failed:", e)
        return orig

def handle_manual_correction(data: dict, text: str) -> bool:
    txt = text.strip()
    if ":" not in txt:
        return False
    key, val = txt.split(":",1)
    k = key.strip().lower()
    v = val.strip()
    mapping = {
        'site': 'site_name','site_name':'site_name',
        'segment':'segment',
        'category':'category',
        'company':'company','companies':'company',
        'people':'people',
        'tools':'tools','tool':'tools',
        'service':'service','services':'service',
        'activities':'activities','activity':'activities',
        'issues':'issues',
        'time':'time','weather':'weather',
        'impression':'impression','comments':'comments',
        'date':'date'
    }
    if k not in mapping:
        return False
    field = mapping[k]

    # Single-value fields
    if field in ('site_name','segment','category','time','weather','impression','comments','date'):
        data[field] = v
        return True

    # List-of-objects or list-of-strings
    if field == 'company':
        arr = data.setdefault('company',[])
        for name in [x.strip() for x in v.split(",") if x.strip()]:
            arr.append({'name':name})
        return True

    if field == 'people':
        arr = data.setdefault('people',[])
        for part in [x.strip() for x in v.split(",") if x.strip()]:
            if 'role' in part:
                nm, rl = part.split('role',1)
                name = nm.strip().rstrip(",:")
                role = rl.strip(" :")
            else:
                name, role = part, ""
            arr.append({'name':name,'role':role})
        return True

    if field == 'tools':
        arr = data.setdefault('tools',[])
        for item in [x.strip() for x in v.split(",") if x.strip()]:
            arr.append({'item':item,'company':""})
        return True

    if field == 'service':
        arr = data.setdefault('service',[])
        for item in [x.strip() for x in v.split(",") if x.strip()]:
            arr.append({'task':item,'company':""})
        return True

    if field == 'activities':
        lst = data.setdefault('activities',[])
        for item in [x.strip() for x in v.split(",") if x.strip()]:
            lst.append(item)
        return True

    if field == 'issues':
        arr = data.setdefault('issues',[])
        for part in [x.strip() for x in v.split(";") if x.strip()]:
            arr.append({'description':part,'caused_by':"","has_photo":False})
        return True

    return False

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        msg = request.get_json().get("message",{})
        chat_id = str(msg.get("chat",{}).get("id",""))
        text = msg.get("text","") or ""
        if not text and msg.get("voice"):
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])

        cmd = text.lower().strip()
        # RESET / NEW
        if cmd in ("new","/new","reset","/reset","new report","start over"):
            session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}
            blank = summarize_data({})
            send_telegram_message(chat_id, f"🔄 New report:\n\n{blank}\n\n✅ Speak or type any field to begin.")
            return "",200

        # Ensure session
        if chat_id not in session_data:
            session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}
        state = session_data[chat_id]
        data = state["structured_data"]

        # CORRECTION PHASE
        if state["awaiting_correction"]:
            if handle_manual_correction(data, text):
                full = summarize_data(data)
                send_telegram_message(chat_id, f"✅ Full updated report:\n\n{full}\n\n✅ Anything else?")
                return "",200
            # fallback to GPT correction
            updated = apply_correction_gpt(data, text)
            session_data[chat_id]["structured_data"] = updated
            full = summarize_data(updated)
            send_telegram_message(chat_id, f"✅ Full updated report:\n\n{full}\n\n✅ Anything else?")
            return "",200

        # FIRST EXTRACTION
        extracted = extract_site_report(text)
        if not extracted.get("site_name"):
            send_telegram_message(chat_id, "⚠️ Sorry, I couldn't detect site info. Please try again.")
            return "",200

        enriched = enrich_with_date(extracted)
        session_data[chat_id] = {"structured_data":enriched, "awaiting_correction":True}
        full = summarize_data(enriched)
        send_telegram_message(
            chat_id,
            f"Here’s what I understood:\n\n{full}\n\n✅ You can correct any field now (e.g. “Category: …”)."
        )
        return "",200

    except Exception as e:
        print("❌ Webhook error:", e)
        return "Error", 500

gpt_prompt_template = """
You are an AI assistant extracting a construction site report.
Only extract fields explicitly mentioned; omit any not stated.

Return JSON with:
site_name, segment, category,
company:[{"name":...}], people:[{"name":...,"role":...}],
tools:[{"item":...,"company":...}], service:[{"task":...,"company":...}],
activities:[...], issues:[{"description":...,"caused_by":...,"has_photo":true/false}],
time, weather, impression, comments, date (dd-mm-yyyy).
"""

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT",10000)))
