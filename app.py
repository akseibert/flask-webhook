from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# Store one in-progress report per user
session_data = {}  # chat_id -> {"structured_data": {...}, "awaiting_correction": bool}

@app.route("/", methods=["GET"])
def index():
    return "Running", 200

def send_telegram_message(chat_id: str, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text})
    print("Telegram send:", resp.status_code, resp.text)

def get_telegram_file_path(file_id: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    res = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}").json()
    return f"https://api.telegram.org/file/bot{token}/{res['result']['file_path']}"

def transcribe_from_telegram_voice(file_id: str) -> str:
    try:
        url = get_telegram_file_path(file_id)
        audio = requests.get(url).content
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"}
        ).json()
        return r.get("text","")
    except Exception as e:
        print("Transcription error:", e)
        return ""

def summarize_data(d: dict) -> str:
    """Render the full report, blank for missing."""
    lines = [
        f"📍 Site: {d.get('site_name','')}",
        f"📆 Segment: {d.get('segment','')}",
        f"🌿 Category: {d.get('category','')}",
        "🏣 Companies: " + ", ".join(c.get("name","") for c in d.get("company",[])),
        "👷 People: " + ", ".join(f"{p.get('name','')} ({p.get('role','')})" for p in d.get("people",[])),
        "🔧 Services: " + ", ".join(f"{s.get('task','')} ({s.get('company','')})" for s in d.get("service",[])),
        "🛠️ Tools: " + ", ".join(f"{t.get('item','')} ({t.get('company','')})" for t in d.get("tools",[])),
        "📋 Activities: " + ", ".join(d.get("activities",[])),
    ]
    # Issues may span multiple lines:
    if d.get("issues"):
        lines.append("⚠️ Issues:")
        for issue in d["issues"]:
            cb = issue.get("caused_by","")
            photo = " 📸" if issue.get("has_photo") else ""
            lines.append(f"• {issue.get('description','')} (by {cb}){photo}")
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
        {"role":"system","content":"Extract only explicitly mentioned fields; omit any not stated."},
        {"role":"user","content":prompt}
    ]
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=msgs, temperature=0.2
        )
        return json.loads(res.choices[0].message.content.strip())
    except Exception as e:
        print("GPT extract failed:", e)
        return {}

def apply_correction_gpt(orig: dict, corr: str) -> dict:
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nUser correction:\n" + corr +
        "\n\nReturn the full updated JSON (no markdown)."
    )
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=[{"role":"user","content":prompt}]
        )
        return json.loads(res.choices[0].message.content.strip())
    except Exception as e:
        print("GPT correction failed:", e)
        return orig

def handle_manual_correction(data: dict, text: str) -> bool:
    """Return True if applied manually."""
    low = text.lower()
    if ":" in text:
        key, val = text.split(":",1)
        key = key.strip().lower()
        val = val.strip().rstrip(".")
        if key == "category":
            data["category"] = val
            return True
        if key == "people":
            # Expect "People: Name role: Role"
            part = val
            if "role" in part.lower():
                name, role_part = part.split("role",1)
                name = name.strip().rstrip(",")
                role = role_part.strip().lstrip(":").strip()
                lst = data.setdefault("people",[])
                # remove any existing same name
                lst = [p for p in lst if p.get("name")!=name]
                lst.append({"name":name,"role":role})
                data["people"] = lst
                return True
    return False

@app.route("/webhook", methods=["POST"])
def webhook():
    msg = request.get_json().get("message",{})
    chat_id = str(msg.get("chat",{}).get("id",""))
    text = msg.get("text","") or ""
    if not text and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])

    # new/reset
    if text.lower().strip() in ("new","reset","new report","start over"):
        session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}
        blank = summarize_data({})
        send_telegram_message(chat_id, f"🔄 New report:\n\n{blank}\n\n✅ Start by speaking or typing.")
        return "",200

    if chat_id not in session_data:
        session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}

    state = session_data[chat_id]
    data = state["structured_data"]

    # If we're awaiting corrections, try manual first:
    if state["awaiting_correction"]:
        if handle_manual_correction(data, text):
            full = summarize_data(data)
            send_telegram_message(chat_id, f"✅ Full updated report:\n\n{full}\n\n✅ Anything else?")
            return "",200
        # else fallback to GPT correction:
        updated = apply_correction_gpt(data, text)
        session_data[chat_id]["structured_data"] = updated
        full = summarize_data(updated)
        send_telegram_message(chat_id, f"✅ Full updated report:\n\n{full}\n\n✅ Anything else?")
        return "",200

    # First-time extraction
    extracted = extract_site_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat_id, "⚠️ Sorry, I couldn't detect site info. Please try again.")
        return "",200

    enriched = enrich_with_date(extracted)
    session_data[chat_id] = {"structured_data":enriched, "awaiting_correction":True}
    full = summarize_data(enriched)
    send_telegram_message(chat_id, f"Here’s what I understood:\n\n{full}\n\n✅ You can correct anytime.")
    return "",200

# Prompt template
gpt_prompt_template = """
You are an AI assistant extracting a construction site report.
Only extract fields explicitly mentioned; omit any not stated.

Return JSON with keys:
site_name, segment, category,
company:[{"name":...}], people:[{"name":...,"role":...}],
tools:[{"item":...,"company":...}], service:[{"task":...,"company":...}],
activities:[...], issues:[{"description":...,"caused_by":...,"has_photo":true/false}],
time, weather, impression, comments, date(dd-mm-yyyy).
"""
