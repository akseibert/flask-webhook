from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client (new SDK style)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# Health check route
@app.route("/", methods=["GET"])
def index():
    return "Running", 200

# In-memory session store
# { telegram_user_id: {"structured_data": {...}, "awaiting_correction": bool} }
session_data = {}

# --- Helpers ----------------------------------------------

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤 Telegram →", payload)
    requests.post(url, json=payload)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    resp = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    path = resp.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_voice(file_id):
    try:
        url = get_telegram_file_path(file_id)
        audio = requests.get(url).content
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return resp.json().get("text", "")
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def enrich_with_date(data):
    today_str = datetime.now().strftime("%d-%m-%Y")
    d = data.get("date", "").strip()
    if not d:
        data["date"] = today_str
    else:
        try:
            parsed = datetime.strptime(d, "%d-%m-%Y")
            if parsed > datetime.now():
                data["date"] = today_str
        except:
            data["date"] = today_str
    return data

def summarize_data(d):
    lines = []
    if "site_name" in d:    lines.append(f"📍 Site: {d['site_name']}")
    if "segment" in d:      lines.append(f"📆 Segment: {d['segment']}")
    if "category" in d:     lines.append(f"🌿 Category: {d['category']}")
    if d.get("company"):
        comps = ", ".join(c["name"] for c in d["company"])
        lines.append(f"🏣 Companies: {comps}")
    if d.get("people"):
        ppl = ", ".join(f"{p['name']} ({p['role']})" for p in d["people"])
        lines.append(f"👷 People: {ppl}")
    if d.get("service"):
        srv = ", ".join(f"{s['task']} ({s['company']})" for s in d["service"])
        lines.append(f"🔧 Services: {srv}")
    if d.get("tools"):
        tls = ", ".join(f"{t['item']} ({t['company']})" for t in d["tools"])
        lines.append(f"🛠️ Tools: {tls}")
    if d.get("activities"):
        lines.append("📋 Activities: " + ", ".join(d["activities"]))
    if d.get("issues"):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            cb = i.get("caused_by", "")
            photo = " 📸" if i.get("has_photo") else ""
            lines.append(f"• {i['description']}{' (by '+cb+')' if cb else ''}{photo}")
    if "time" in d:        lines.append(f"⏰ Time: {d['time']}")
    if "weather" in d:     lines.append(f"🌦️ Weather: {d['weather']}")
    if "impression" in d:  lines.append(f"💬 Impression: {d['impression']}")
    if "comments" in d:    lines.append(f"📝 Comments: {d['comments']}")
    if "date" in d:        lines.append(f"🗓️ Date: {d['date']}")
    return "\n".join(lines)

def extract_report(text):
    prompt = gpt_prompt_template + "\n" + text
    messages = [
        {"role": "system", "content": 
         "You are a strict assistant. ONLY extract fields that are explicitly mentioned. "
         "Do NOT guess or fill in defaults."},
        {"role": "user", "content": prompt}
    ]
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.2
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parse error:", e)
        return {}

def apply_correction(orig, corr_text):
    # ask GPT only for the fields to change
    prompt = (
        "Original data:\n" + json.dumps(orig, ensure_ascii=False) +
        "\nUser correction:\n" + corr_text +
        "\nReturn JSON with ONLY the fields that should be updated."
    )
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        diff = json.loads(res.choices[0].message.content)
        # merge diff into orig
        for k,v in diff.items():
            orig[k] = v
        return orig
    except Exception as e:
        print("❌ Correction parse error:", e)
        return orig

# --- Webhook Handler ---------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("🔔 update:", json.dumps(data))

    if "message" not in data:
        return "no message", 400

    msg = data["message"]
    chat = str(msg["chat"]["id"])
    text = msg.get("text", "").strip().lower()
    
    # voice → text?
    if not text and "voice" in msg:
        text = transcribe_voice(msg["voice"]["file_id"]).strip().lower()
    
    # initialize session
    if chat not in session_data:
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}

    # RESET on keywords
    if any(text.startswith(k) for k in ["new", "new report", "new entry", "reset"]):
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}
        send_telegram_message(chat, "✔️ Starting a fresh report. What site are we logging today?")
        return "", 200

    sess = session_data[chat]
    
    # if waiting for correction, merge
    if sess["awaiting_correction"]:
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = updated
        # remain in correction mode
        summary = summarize_data(updated)
        send_telegram_message(chat,
            "✅ Got it! Here’s the **full** updated report:\n\n"
            + summary + "\n\n✅ Anything else to correct?"
        )
        return "", 200

    # otherwise, first extraction
    extracted = extract_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat, "⚠️ Sorry, I couldn't detect the site name. Please try again.")
        return "", 200

    # enrich date, store and switch to corrections
    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True

    summary = summarize_data(enriched)
    send_telegram_message(chat,
        "Here’s what I understood:\n\n"
        + summary + "\n\n✅ Is this correct? You can send corrections or say “new report” to start over."
    )
    return "", 200

# --- Prompt Template ---------------------------------------

gpt_prompt_template = """
You are an AI assistant extracting a construction-site report from a spoken or written summary.
Only pull out fields explicitly mentioned; do NOT infer or guess missing information.
Return JSON with any of these keys that appear:

site_name, segment, category,
company (list of {name}),
people (list of {name,role}),
tools (list of {item,company}),
service (list of {task,company}),
activities (list of strings),
issues (list of {description,caused_by,has_photo}),
time, weather, impression, comments, date (dd-mm-yyyy)
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
