from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client (new SDK style)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In‐memory session store
# chat_id → {"structured_data": {...}, "awaiting_correction": bool}
session_data = {}

# Health check
@app.route("/", methods=["GET"])
def index():
    return "Running", 200

def send_telegram_message(chat_id: str, text: str):
    """Send plain-text Telegram message (no Markdown parsing)."""
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        # omit parse_mode so Telegram treats it as plain text
    }
    print("📤 Sending to Telegram:", url)
    print("📤 Payload:", json.dumps(payload, indent=2))
    resp = requests.post(url, json=payload)
    print("✅ Telegram response:", resp.status_code, resp.text)

def get_telegram_file_path(file_id: str) -> str:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{telegram_token}/getFile?file_id={file_id}")
    path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{telegram_token}/{path}"

def transcribe_from_telegram_voice(file_id: str) -> str:
    try:
        audio_url = get_telegram_file_path(file_id)
        audio_resp = requests.get(audio_url)
        if audio_resp.status_code != 200:
            print("❌ Could not download voice file")
            return ""
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio_resp.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper.json().get("text", "")
    except Exception as e:
        print("❌ Transcription failed:", e)
        return ""

def summarize_data(d: dict) -> str:
    lines = []
    if d.get("site_name"):
        lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):
        lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):
        lines.append(f"🌿 Category: {d['category']}")
    if isinstance(d.get("company"), list):
        companies = ", ".join(c.get("name","") for c in d["company"] if isinstance(c, dict))
        lines.append(f"🏣 Companies: {companies}")
    if isinstance(d.get("people"), list):
        people = ", ".join(f"{p.get('name','')} ({p.get('role','')})"
                           for p in d["people"] if isinstance(p, dict))
        lines.append(f"👷 People: {people}")
    if isinstance(d.get("service"), list):
        services = ", ".join(f"{s.get('task','')} ({s.get('company','')})"
                             for s in d["service"] if isinstance(s, dict))
        lines.append(f"🔧 Services: {services}")
    if isinstance(d.get("tools"), list):
        tools = ", ".join(f"{t.get('item','')} ({t.get('company','')})"
                          for t in d["tools"] if isinstance(t, dict))
        lines.append(f"🛠️ Tools: {tools}")
    if isinstance(d.get("activities"), list):
        lines.append(f"📋 Activities: {', '.join(d['activities'])}")
    if isinstance(d.get("issues"), list):
        lines.append("⚠️ Issues:")
        for issue in d["issues"]:
            desc = issue.get("description","")
            by = issue.get("caused_by","")
            photo = " 📸" if issue.get("has_photo") else ""
            lines.append(f"• {desc} (by {by}){photo}")
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

def enrich_with_date(d: dict) -> dict:
    today = datetime.now()
    today_str = today.strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today_str
    else:
        try:
            parsed = datetime.strptime(d["date"], "%d-%m-%Y")
            if parsed > today:
                d["date"] = today_str
        except:
            d["date"] = today_str
    return d

# The strict JSON-extraction prompt
gpt_prompt_template = """
You are an AI assistant helping extract a construction site report based on a spoken or written summary.

⚠️ Only extract fields that are explicitly mentioned. Never guess or fill in missing details.

Return JSON with any of these keys that were said:
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

def extract_site_report(text: str) -> dict:
    messages = [
        {"role": "system", "content": "You ONLY extract explicitly mentioned fields."},
        {"role": "user", "content": gpt_prompt_template + "\n" + text}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.2
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

def apply_correction(orig: dict, correction: str) -> dict:
    prompt = f"""
Original JSON:
{json.dumps(orig)}

User said (correction):
"{correction}"

Return the updated JSON, changing only what the user requested.
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ Correction GPT parsing failed:", e)
        return orig

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    print("📩 Telegram webhook received:", json.dumps(data))
    if "message" not in data:
        return "ok", 200

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text")

    # Voice → text
    if not text and "voice" in msg:
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not text:
            send_telegram_message(chat_id, "⚠️ Couldn't understand audio; please try again.")
            return "ok", 200

    # Initialize session
    if chat_id not in session_data:
        session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}

    sess = session_data[chat_id]

    # New / reset commands
    if text and text.lower().strip() in ("new", "/new", "reset", "start again"):
        sess["structured_data"] = {}
        sess["awaiting_correction"] = False
        template = summarize_data(enrich_with_date({})) or ""
        send_telegram_message(chat_id,
            "🔄 **Starting a fresh report**\n\n" + template +
            "\n\n✅ Now please speak or type your first field."
        )
        return "ok", 200

    # If awaiting correction, apply it
    if sess["awaiting_correction"]:
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = updated
        # remain in correction mode
        summary = summarize_data(updated)
        send_telegram_message(chat_id,
            "✅ Got it! Here’s the full updated report:\n\n" + summary +
            "\n\n✅ Anything else to correct?"
        )
        return "ok", 200

    # Otherwise, first extraction
    extracted = extract_site_report(text or "")
    if not extracted or "site_name" not in extracted:
        send_telegram_message(chat_id,
            "⚠️ Sorry, I couldn't detect the site name. Please try again."
        )
        return "ok", 200

    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True
    summary = summarize_data(enriched)
    send_telegram_message(chat_id,
        "Here’s what I understood:\n\n" + summary +
        "\n\n✅ Is this correct? You can send corrections or type “new” to start over."
    )
    return "ok", 200

if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "10000")))
