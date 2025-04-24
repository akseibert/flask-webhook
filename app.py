from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In‐memory session store: chat_id → { structured_data, awaiting_correction }
session_data = {}

@app.route("/", methods=["GET"])
def index():
    return "Running", 200

def send_telegram_message(chat_id: str, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤", url, payload)
    r = requests.post(url, json=payload)
    print("✅", r.status_code, r.text)

def get_telegram_file_path(file_id: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_from_telegram_voice(file_id: str) -> str:
    try:
        audio_url = get_telegram_file_path(file_id)
        resp = requests.get(audio_url)
        if resp.status_code != 200:
            return ""
        whisper = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", resp.content, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return whisper.json().get("text", "").strip()
    except Exception as e:
        print("❌ Transcription error:", e)
        return ""

def summarize_data(d: dict) -> str:
    lines = []
    if d.get("site_name"): lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):   lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):  lines.append(f"🌿 Category: {d['category']}")
    if isinstance(d.get("company"), list):
        lines.append("🏣 Companies: " + ", ".join(c.get("name","") for c in d["company"]))
    if isinstance(d.get("people"), list):
        lines.append("👷 People: " + ", ".join(f"{p.get('name','')} ({p.get('role','')})" for p in d["people"]))
    if isinstance(d.get("service"), list):
        lines.append("🔧 Services: " + ", ".join(f"{s.get('task','')} ({s.get('company','')})" for s in d["service"]))
    if isinstance(d.get("tools"), list):
        lines.append("🛠️ Tools: " + ", ".join(f"{t.get('item','')} ({t.get('company','')})" for t in d["tools"]))
    if isinstance(d.get("activities"), list):
        lines.append("📋 Activities: " + ", ".join(d["activities"]))
    if isinstance(d.get("issues"), list):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            lines.append(
                f"• {i.get('description','')} (by {i.get('caused_by','')})"
                + (" 📸" if i.get("has_photo") else "")
            )
    if d.get("time"):      lines.append(f"⏰ Time: {d['time']}")
    if d.get("weather"):   lines.append(f"🌦️ Weather: {d['weather']}")
    if d.get("impression"):lines.append(f"💬 Impression: {d['impression']}")
    if d.get("comments"):  lines.append(f"📝 Comments: {d['comments']}")
    if d.get("date"):      lines.append(f"🗓️ Date: {d['date']}")
    return "\n".join(lines)

def enrich_with_date(d: dict) -> dict:
    today = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today
    else:
        try:
            if datetime.strptime(d["date"], "%d-%m-%Y") > datetime.now():
                d["date"] = today
        except:
            d["date"] = today
    return d

gpt_prompt_template = """
You are an AI that only extracts explicitly mentioned fields from a construction site report.

Return JSON with keys that appear:
site_name, segment, category, company, people, tools, service,
activities, issues, time, weather, impression, comments, date (dd-mm-yyyy).
"""

def extract_site_report(text: str) -> dict:
    messages = [
        {"role":"system","content":"Only extract explicitly mentioned fields."},
        {"role":"user","content":gpt_prompt_template + "\n" + text}
    ]
    try:
        r = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.2
        )
        return json.loads(r.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parse failed:", e)
        return {}

def apply_correction(orig: dict, corr: str) -> dict:
    prompt = f"Original JSON:\n{json.dumps(orig)}\n\nCorrection:\n{corr}"
    try:
        r = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(r.choices[0].message.content)
    except Exception as e:
        print("❌ Correction parse failed:", e)
        return orig

# shorthand field updates
_manual_fields = {
    "site":      "site_name",
    "segment":   "segment",
    "category":  "category",
    "company":   "company",
    "people":    "people",
    "tools":     "tools",
    "service":   "service",
    "activities":"activities",
    "issues":    "issues",
    "time":      "time",
    "weather":   "weather",
    "impression":"impression",
    "comments":  "comments",
    "date":      "date"
}

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    msg  = data.get("message", {})
    chat = str(msg.get("chat",{}).get("id",""))
    text = msg.get("text")

    # handle voice → text once
    if not text and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not text:
            send_telegram_message(chat, "⚠️ Couldn't understand audio. Please repeat.")
            return "ok",200

    # init session
    if chat not in session_data:
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}
    sess = session_data[chat]
    sd   = sess["structured_data"]

    # NEW / RESET
    if text and text.strip().lower() in ("new","/new","reset","start again"):
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}
        blank = summarize_data(enrich_with_date({}))
        send_telegram_message(chat,
            "🔄 **Starting a fresh report**\n\n" + blank +
            "\n\nPlease say the site name."
        )
        return "ok",200

    # CORRECTION round
    if sess["awaiting_correction"]:
        try:
            lc = text.strip().lower()
            # manual shorthand first
            for key, field in _manual_fields.items():
                if lc.startswith(key + ":") or lc.startswith(key + " "):
                    val = text.split(":",1)[-1].strip() if ":" in text else text.split(" ",1)[-1].strip()
                    if field == "company":
                        sd["company"] = [{"name": val}]
                    elif field == "people":
                        parts = val.split(None,1)
                        name, role = parts[0], (parts[1] if len(parts)>1 else "")
                        sd.setdefault("people",[]).append({"name":name,"role":role})
                    elif field in ("tools","service","activities","issues"):
                        if field=="tools":
                            sd.setdefault("tools",[]).append({"item":val,"company":""})
                        elif field=="service":
                            sd.setdefault("service",[]).append({"task":val,"company":""})
                        elif field=="activities":
                            sd.setdefault("activities",[]).append(val)
                        else:
                            sd.setdefault("issues",[]).append({"description":val,"caused_by":"","has_photo":False})
                    else:
                        sd[field] = val
                    summary = summarize_data(sd)
                    send_telegram_message(chat, "✅ Full updated report:\n\n" + summary + "\n\nAnything else?")
                    return "ok",200
            # fallback GPT correction
            updated = apply_correction(sd, text)
            session_data[chat]["structured_data"] = updated
            summary = summarize_data(updated)
            send_telegram_message(chat, "✅ Full updated report:\n\n" + summary + "\n\nAnything else?")
        except Exception as e:
            print("❌ Correction error:", e)
            send_telegram_message(chat, "⚠️ Something went wrong applying your correction. Try again.")
        return "ok",200

    # FIRST‐TIME extraction
    extracted = extract_site_report(text or "")
    if not extracted.get("site_name"):
        send_telegram_message(chat,
            "⚠️ I couldn’t detect a site name. Please say it clearly."
        )
        return "ok",200

    # save & confirm
    sess["structured_data"] = enrich_with_date(extracted)
    sess["awaiting_correction"] = True
    summary = summarize_data(sess["structured_data"])
    send_telegram_message(chat,
        "Here’s what I understood:\n\n" + summary +
        "\n\n✅ Is this correct? You can send corrections (e.g. “Company: X AG”) or type “new” to restart."
    )
    return "ok",200

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT","10000")), debug=True)
