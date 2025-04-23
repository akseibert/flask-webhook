from flask import Flask, request
import requests
import os
import json
from datetime import datetime
import openai

# Initialize OpenAI client (new SDK style)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# In-memory session store
# { telegram_user_id: {"structured_data": {...}, "awaiting_correction": bool} }
session_data = {}

# --- Helpers --------------------------------------------------

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
    """Download voice from Telegram, send to Whisper, return text."""
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
    """Ensure `date` field is in dd-mm-YYYY and not in the future."""
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
    """Build a multi-line summary from the structured data."""
    lines = []
    if d.get("site_name"):
        lines.append(f"📍 Site: {d['site_name']}")
    if d.get("segment"):
        lines.append(f"📆 Segment: {d['segment']}")
    if d.get("category"):
        lines.append(f"🌿 Category: {d['category']}")
    # companies
    comp = d.get("company")
    if comp:
        if isinstance(comp, list):
            names = [c.get("name", c) if isinstance(c, dict) else str(c) for c in comp]
            comp_str = ", ".join(names)
        else:
            comp_str = str(comp)
        lines.append(f"🏣 Companies: {comp_str}")
    # people
    if isinstance(d.get("people"), list):
        ppl = ", ".join(f"{p.get('name','')} ({p.get('role','')})" for p in d["people"] if isinstance(p, dict))
        lines.append(f"👷 People: {ppl}")
    # services
    if isinstance(d.get("service"), list):
        srv = ", ".join(f"{s.get('task','')} ({s.get('company','')})" for s in d["service"] if isinstance(s, dict))
        lines.append(f"🔧 Services: {srv}")
    # tools
    if isinstance(d.get("tools"), list):
        tls = ", ".join(f"{t.get('item','')} ({t.get('company','')})" for t in d["tools"] if isinstance(t, dict))
        lines.append(f"🛠️ Tools: {tls}")
    # activities
    if isinstance(d.get("activities"), list):
        lines.append("📋 Activities: " + ", ".join(d["activities"]))
    # issues
    if isinstance(d.get("issues"), list):
        lines.append("⚠️ Issues:")
        for i in d["issues"]:
            if isinstance(i, dict):
                desc = i.get("description","")
                cb   = i.get("caused_by","")
                ph   = " 📸" if i.get("has_photo") else ""
                note = f" (by {cb})" if cb else ""
                lines.append(f"• {desc}{note}{ph}")
    # other single fields
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
    """
    Merge GPT's diff into orig:
     - For list-fields: wrap single items, extend without duplicates.
     - Otherwise: replace.
    """
    list_keys = {"company","people","tools","service","activities","issues"}
    for k, v in diff.items():
        if k in list_keys:
            # normalize v to a list
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
    """Call GPT to extract the initial structured report."""
    prompt = gpt_prompt_template + "\n" + text
    messages = [
        {"role": "system", "content":
         "You are strict: ONLY extract fields explicitly mentioned. Do NOT guess."},
        {"role": "user", "content": prompt}
    ]
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print("❌ GPT parse error:", e)
        return {}

def apply_correction(orig, text):
    """
    Ask GPT which fields to update, then merge into orig.
    """
    prompt = (
        "Original JSON:\n" + json.dumps(orig, ensure_ascii=False) +
        "\nUser correction:\n" + text +
        "\nReturn JSON containing only the fields to update."
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

# --- Webhook -------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("🔔 update:", json.dumps(data))

    if "message" not in data:
        return "no message", 400

    msg = data["message"]
    chat = str(msg["chat"]["id"])
    text = msg.get("text","").strip()
    lower = text.lower()

    # handle voice
    if not text and "voice" in msg:
        text = transcribe_voice(msg["voice"]["file_id"]).strip()
        lower = text.lower()

    # init session
    if chat not in session_data:
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}

    # reset if user wants a fresh report
    if any(lower.startswith(k) for k in ("new","reset")):
        session_data[chat] = {"structured_data": {}, "awaiting_correction": False}
        send_telegram_message(chat, "✔️ Starting a fresh report. What site are we logging today?")
        return "", 200

    sess = session_data[chat]

    # if waiting for corrections, merge them
    if sess["awaiting_correction"]:
        updated = apply_correction(sess["structured_data"], text)
        sess["structured_data"] = updated
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

    enriched = enrich_with_date(extracted)
    sess["structured_data"] = enriched
    sess["awaiting_correction"] = True

    summary = summarize_data(enriched)
    send_telegram_message(chat,
        "Here’s what I understood:\n\n"
        + summary + "\n\n✅ Is this correct? You can send corrections or type “new” to start over."
    )
    return "", 200

# --- GPT Prompt Template ------------------------------------

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
