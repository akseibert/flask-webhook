from flask import Flask, request
import requests
import os
import json
from datetime import datetime, timedelta
import openai

# Initialize OpenAI client
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In-memory session store
# { telegram_user_id: {"structured_data": {...}, "awaiting_correction": bool} }
session_data = {}

# --- Telegram helpers ---------------------------------------

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    print("📤 Telegram →", payload)
    requests.post(url, json=payload)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    )
    return "https://api.telegram.org/file/bot" + token + "/" + resp.json()["result"]["file_path"]

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

# --- Data processing helpers -------------------------------

def enrich_with_date(data):
    today = datetime.now()
    today_str = today.strftime("%d-%m-%Y")
    d = data.get("date", "").strip()
    if not d:
        data["date"] = today_str
    else:
        dl = d.lower()
        if dl in ("today", "yesterday", "tomorrow"):
            delta = {"yesterday": -1, "today": 0, "tomorrow": 1}[dl]
            data["date"] = (today + timedelta(days=delta)).strftime("%d-%m-%Y")
        else:
            # attempt parse dd-mm-YYYY
            try:
                parsed = datetime.strptime(d, "%d-%m-%Y")
                if parsed > today:
                    data["date"] = today_str
            except:
                data["date"] = today_str
    return data

def summarize_data(d):
    lines = []
    if d.get("site_name"):
        lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):
        lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):
        lines.append(f"🌿 Category: {d['category']}")
    if d.get("company"):
        comp = d["company"]
        if not isinstance(comp, list):
            comp = [comp]
        names = [c["name"] if isinstance(c, dict) else str(c) for c in comp]
        lines.append(f"🏣 Companies: {', '.join(names)}")
    if d.get("people"):
        ppl = d["people"]
        ppl_list = []
        for p in ppl:
            if isinstance(p, dict):
                ppl_list.append(f"{p.get('name','')} ({p.get('role','')})")
        lines.append("👷 People: " + ", ".join(ppl_list))
    if d.get("service"):
        srv = d["service"]
        srv_list = []
        for s in srv:
            if isinstance(s, dict):
                srv_list.append(f"{s.get('task','')} ({s.get('company','')})")
        lines.append("🔧 Services: " + ", ".join(srv_list))
    if d.get("tools"):
        tls = d["tools"]
        tls_list = []
        for t in tls:
            if isinstance(t, dict):
                tls_list.append(f"{t.get('item','')} ({t.get('company','')})")
        lines.append("🛠️ Tools: " + ", ".join(tls_list))
    if d.get("activities"):
        lines.append("📋 Activities: " + ", ".join(d["activities"]))
    if d.get("issues"):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            if isinstance(i, dict):
                desc = i.get("description","")
                cb = i.get("caused_by","")
                ph = " 📸" if i.get("has_photo") else ""
                note = f" (by {cb})" if cb else ""
                lines.append(f"• {desc}{note}{ph}")
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

def merge_correction(orig, diff):
    list_keys = {"company","people","tools","service","activities","issues"}
    for k, v in diff.items():
        if k in ("date",):
            # handle relative or direct date
            try:
                temp = {"date": v}
                enrich_with_date(temp)
                orig["date"] = temp["date"]
            except:
                orig["date"] = v
        elif k in list_keys:
            vals = v if isinstance(v, list) else [v]
            base = orig.get(k, [])
            if not isinstance(base, list):
                base = []
            for item in vals:
                if item not in base:
                    base.append(item)
            orig[k] = base
        else:
            orig[k] = v
    return orig

def extract_report(text):
    prompt = gpt_prompt_template + "\n" + text
    messages = [
        {"role":"system","content":
         "You are strict: ONLY extract fields explicitly mentioned. Do NOT guess."},
        {"role":"user","content": prompt}
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

def apply_correction(orig, text):
    prompt = (
        "Original JSON:\n" + json.dumps(orig, ensure_ascii=False) +
        "\nUser correction:\n" + text +
        "\nReturn JSON of only updated fields."
    )
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        diff = json.loads(res.choices[0].message.content)
        return merge_correction(orig, diff)
    except Exception as e:
        print("❌ Correction parse error:", e)
        return orig

# --- Flask webhook -----------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("🔔 update:", json.dumps(data))

    if "message" not in data:
        return "no message", 400

    msg    = data["message"]
    chat   = str(msg["chat"]["id"])
    text   = msg.get("text","").strip()
    lower  = text.lower()

    # handle voice
    if not text and "voice" in msg:
        text = transcribe_voice(msg["voice"]["file_id"]).strip()
        lower = text.lower()

    # new/reset
    if chat not in session_data or lower.startswith(("new","reset")):
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}
        send_telegram_message(chat, "✔️ Starting a fresh report. What site are we logging today?")
        return "", 200

    sess = session_data[chat]

    # corrections
    if sess["awaiting_correction"]:
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = updated
        summary = summarize_data(updated)
        send_telegram_message(chat,
            "✅ Got it! Here’s the **full** updated report:\n\n"
            + summary + "\n\n✅ Anything else to correct?"
        )
        return "", 200

    # first pass extraction
    extracted = extract_report(text)
    if not extracted.get("site_name"):
        send_telegram_message(chat,
            "⚠️ Sorry, I couldn't detect the site name. Please try again."
        )
        return "", 200

    enriched = enrich_with_date(extracted)
    sess["structured_data"]    = enriched
    sess["awaiting_correction"] = True

    summary = summarize_data(enriched)
    send_telegram_message(chat,
        "Here’s what I understood:\n\n"
        + summary + "\n\n✅ Is this correct? You can send corrections or type “new” to start over."
    )
    return "", 200

# --- Prompt Template ---------------------------------------

gpt_prompt_template = """
You are an AI assistant extracting a construction site report from a spoken or written summary.
Only pull out fields explicitly mentioned; do NOT infer or guess missing information.
Return JSON with any of these keys that appear:

site_name, segment, category,
company (list of {{name}}),
people (list of {{name,role}}),
tools (list of {{item,company}}),
service (list of {{task,company}}),
activities (list of strings),
issues (list of {{description,caused_by,has_photo}}),
time, weather, impression, comments, date (dd-mm-YYYY)
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
