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

# Health check
@app.route("/", methods=["GET"])
def index():
    return "Running", 200

# In‐memory session store
# { chat_id: { "structured_data": {...}, "awaiting_correction": False } }
session_data = {}

# === TELEGRAM HELPERS ===

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    resp = requests.post(url, json=payload)
    print("↪︎", resp.status_code, resp.text)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    return "https://api.telegram.org/file/bot" + token + "/" + r.json()["result"]["file_path"]

def transcribe_voice(file_id):
    audio_url = get_telegram_file_path(file_id)
    r = requests.get(audio_url)
    if r.status_code != 200:
        return ""
    whisper = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
        files={"file": ("voice.ogg", r.content, "audio/ogg")},
        data={"model": "whisper-1"}
    )
    if whisper.status_code != 200:
        return ""
    return whisper.json().get("text","")

# === DATA FORMAT HELPERS ===

def summarize_data(d):
    # For every field, show either value or blank
    parts = [
        f"📍 Site: {d.get('site_name','')}",
        f"📆 Segment: {d.get('segment','')}",
        f"🌿 Category: {d.get('category','')}",
        f"🏣 Companies: " + ", ".join(c.get("name","") for c in d.get("company",[])),
        f"👷 People: " + ", ".join(f"{p.get('name','')} ({p.get('role','')})" for p in d.get("people",[])),
        f"🛠️ Tools: " + ", ".join(f"{t.get('item','')} ({t.get('company','')})" for t in d.get("tools",[])),
        f"🔧 Services: " + ", ".join(f"{s.get('task','')} ({s.get('company','')})" for s in d.get("service",[])),
        f"📋 Activities: " + ", ".join(d.get("activities",[])),
        f"⚠️ Issues: " + "; ".join(f"{i.get('description','')} (by {i.get('caused_by','')}){' 📸' if i.get('has_photo') else ''}" for i in d.get("issues",[])),
        f"⏰ Time: {d.get('time','')}",
        f"🌦️ Weather: {d.get('weather','')}",
        f"💬 Impression: {d.get('impression','')}",
        f"📝 Comments: {d.get('comments','')}",
        f"🗓️ Date: {d.get('date','')}"
    ]
    return "\n".join(parts)

def enrich_with_date(d):
    today = datetime.now()
    fmt = "%d-%m-%Y"
    if not d.get("date"):
        d["date"] = today.strftime(fmt)
    else:
        try:
            parsed = datetime.strptime(d["date"],fmt)
            if parsed > today:
                d["date"] = today.strftime(fmt)
        except:
            d["date"] = today.strftime(fmt)
    return d

# === LLM FUNCTIONS ===

def _strip_fences(s):
    # remove ```json ... ``` or ``` ... ```
    return re.sub(r"```(?:json)?\n([\s\S]*?)```", r"\1", s).strip()

def extract_site_report(text):
    prompt = (
        gpt_prompt_template
        + "\n\n"
        + text
    )
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        temperature=0.2,
        messages=[
            {"role":"system","content":"You ONLY extract fields explicitly mentioned. Do NOT guess or infer."},
            {"role":"user","content":prompt}
        ]
    )
    raw = resp.choices[0].message.content
    try:
        body = _strip_fences(raw)
        return json.loads(body)
    except Exception as e:
        print("❌ GPT parsing failed:", e, "\nRAW:", raw)
        return {}

def apply_correction(original, correction):
    prompt = (
        "Here is the original JSON:\n"
        + json.dumps(original, ensure_ascii=False, indent=2)
        + "\n\nUser correction: \"" + correction + "\"\n\n"
        + "Return the UPDATED JSON with only the corrected fields changed."
    )
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}],
        temperature=0.2
    )
    raw = resp.choices[0].message.content
    try:
        return json.loads(_strip_fences(raw))
    except Exception as e:
        print("❌ Correction parsing failed:", e, "\nRAW:", raw)
        return original

# === TELEGRAM WEBHOOK ===

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    print("↪ Received:", json.dumps(data, indent=2))
    msg = data.get("message",{})
    chat_id = str(msg.get("chat",{}).get("id",""))
    text = msg.get("text")
    if not text and msg.get("voice"):
        text = transcribe_voice(msg["voice"]["file_id"])
    if not chat_id:
        return "no chat",400

    # initialize
    session = session_data.setdefault(chat_id,{"structured_data":{}, "awaiting_correction":False})

    # RESET COMMAND?
    if text and text.lower().strip() in ("new report","reset","start over"):
        session["structured_data"] = {}
        session["awaiting_correction"] = False
        blank = summarize_data(enrich_with_date({}))
        send_telegram_message(chat_id, "🔄 Starting a fresh report:\n\n" + blank + "\n\nYou can now speak or type any field.")
        return "reset",200

    # CORRECTION ROUND
    if session["awaiting_correction"]:
        updated = apply_correction(session["structured_data"], text)
        session["structured_data"] = updated
        # stay in correction mode
        summary = summarize_data(updated)
        send_telegram_message(chat_id, f"✅ Got it! Here’s the **full** updated report:\n\n{summary}\n\nAnything else to correct?")
        return "corrected",200

    # FIRST EXTRACTION
    struct = extract_site_report(text or "")
    if not struct.get("site_name"):
        send_telegram_message(chat_id, "⚠️ Sorry, I couldn’t detect site info. Please try again.")
        return "need retry",200

    # enrich date, store and enter correction mode
    struct = enrich_with_date(struct)
    session["structured_data"] = struct
    session["awaiting_correction"] = True

    summary = summarize_data(struct)
    send_telegram_message(chat_id, f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections.")
    return "ok",200

# === PROMPT TEMPLATE ===

gpt_prompt_template = """
You are an AI assistant extracting a construction site report from a single block of text.

⚠️ Only extract what is explicitly mentioned. Do NOT fill or infer missing fields.

Return JSON with any of these fields that appeared:
- site_name
- segment
- category
- company (list of {"name":...})
- people (list of {"name":...,"role":...})
- tools (list of {"item":...,"company":...})
- service (list of {"task":...,"company":...})
- activities (list of strings)
- issues (list of {"description":...,"caused_by":...,"has_photo":true/false})
- time
- weather
- impression
- comments
- date (dd-mm-yyyy)
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
