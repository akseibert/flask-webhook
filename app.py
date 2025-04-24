from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# — Initialize OpenAI client (new SDK style) —
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In-memory sessions
# chat_id → {"structured_data": {...}, "awaiting_correction": bool}
session_data = {}

# — Helpers — #

def send_telegram_message(chat_id: str, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
    print("📤 Sending to Telegram:", url)
    print("📤 Payload:", json.dumps(payload, indent=2))
    resp = requests.post(url, json=payload)
    print("✅ Telegram response:", resp.status_code, resp.text)

def get_telegram_file_path(file_id: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    r = requests.get(url).json()
    path = r["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_from_telegram_voice(file_id: str) -> str:
    try:
        audio_url = get_telegram_file_path(file_id)
        audio = requests.get(audio_url).content
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"}
        ).json()
        return r.get("text","")
    except Exception as e:
        print("❌ Transcription error:", e)
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
        names = ", ".join(c.get("name","") for c in d["company"] if isinstance(c,dict))
        lines.append(f"🏣 Companies: {names}")
    if isinstance(d.get("people"), list):
        ppl = ", ".join(f"{p.get('name','')} ({p.get('role','')})" for p in d["people"] if isinstance(p,dict))
        lines.append(f"👷 People: {ppl}")
    if isinstance(d.get("tools"), list):
        tl = ", ".join(f"{t.get('item','')} ({t.get('company','')})" for t in d["tools"] if isinstance(t,dict))
        lines.append(f"🛠️ Tools: {tl}")
    if isinstance(d.get("service"), list):
        sv = ", ".join(f"{s.get('task','')} ({s.get('company','')})" for s in d["service"] if isinstance(s,dict))
        lines.append(f"🔧 Services: {sv}")
    if isinstance(d.get("activities"), list):
        lines.append("📋 Activities: " + ", ".join(d["activities"]))
    if isinstance(d.get("issues"), list):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            if isinstance(i,dict):
                cb = i.get("caused_by","")
                ph = " 📸" if i.get("has_photo") else ""
                lines.append(f"• {i.get('description','')} (by {cb}){ph}")
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
    today = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today
    else:
        try:
            parsed = datetime.strptime(d["date"], "%d-%m-%Y")
            if parsed > datetime.now():
                d["date"] = today
        except:
            d["date"] = today
    return d

def extract_site_report(text: str) -> dict:
    prompt = gpt_prompt_template + "\n" + text
    msgs = [
        {"role":"system","content":"You ONLY extract fields that are *explicitly* mentioned. Never guess or fill missing values."},
        {"role":"user","content":prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=msgs, temperature=0.2
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parse fail:", e)
        return {}

def apply_correction(original: dict, corr_text: str) -> dict:
    prompt = (
        "You are correcting this JSON:\n"
        f"{json.dumps(original,indent=2)}\n\n"
        "User said:\n"
        f"\"{corr_text}\"\n\n"
        "Return *only* the fields that changed, as JSON."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("❌ Correction parse fail:", e)
        return {}

# — Main webhook — #

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    msg = data.get("message",{})
    chat_id = str(msg.get("chat",{}).get("id",""))
    text = msg.get("text")

    # 1) Reset / new report
    if text and text.lower() in ["new","new report","reset","start again","/new"]:
        session_data[chat_id] = {"structured_data":{}, "awaiting_correction":False}
        today = datetime.now().strftime("%d-%m-%Y")
        template = "🔄 *Starting a fresh report*\n\n"
        fields = [
            ("📍","Site"),
            ("📆","Segment"),
            ("🌿","Category"),
            ("🏣","Companies"),
            ("👷","People"),
            ("🛠️","Tools"),
            ("🔧","Services"),
            ("📋","Activities"),
            ("⚠️","Issues"),
            ("⏰","Time"),
            ("🌦️","Weather"),
            ("💬","Impression"),
            ("📝","Comments"),
            ("🗓️","Date")
        ]
        for emoji,label in fields:
            if label=="Date":
                template += f"{emoji} {label}: {today}\n"
            else:
                template += f"{emoji} {label}: \n"
        template += "\n✅ You can now speak or type your first field."
        send_telegram_message(chat_id, template)
        return "",200

    # 2) Transcribe voice if needed
    if not text and msg.get("voice"):
        text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not text:
            send_telegram_message(chat_id, "⚠️ Sorry, couldn't understand the audio. Try again.")
            return "",200

    # ensure session exists
    sess = session_data.setdefault(chat_id, {"structured_data":{}, "awaiting_correction":False})

    # 3) Correction branch
    if sess["awaiting_correction"]:
        original = sess["structured_data"]
        corr = apply_correction(original, text)
        # merge
        merged = {**original, **corr}
        enriched = enrich_with_date(merged)
        sess["structured_data"] = enriched
        sess["awaiting_correction"] = True
        summary = summarize_data(enriched)
        send_telegram_message(
            chat_id,
            f"✅ Got it! Here’s the **full** updated report:\n\n{summary}\n\n✅ Anything else to correct?"
        )
        return "",200

    # 4) First‐time extraction
    extracted = extract_site_report(text or "")
    if not extracted.get("site_name"):
        send_telegram_message(chat_id, "⚠️ Sorry, I couldn't detect site info. Please try again.")
        return "",200

    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True
    summary = summarize_data(enriched)
    send_telegram_message(
        chat_id,
        f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections."
    )
    return "",200

# — GPT Prompt Template — #
gpt_prompt_template = """
You are an AI assistant extracting a construction site report from a spoken or written summary.

⚠️ Only extract fields *explicitly mentioned*. Do NOT infer or guess missing data.

Return a JSON with these fields (omit any not mentioned):
- site_name
- segment
- category
- company: list of {{ "name": "…" }}
- people: list of {{ "name": "…", "role": "…" }}
- tools: list of {{ "item": "…", "company": "…" }}
- service: list of {{ "task": "…", "company": "…" }}
- activities: list of strings
- issues: list of {{ "description": "…", "caused_by": "…", "has_photo": true/false }}
- time
- weather
- impression
- comments
- date (format: DD-MM-YYYY)
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
