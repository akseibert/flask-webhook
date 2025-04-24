from flask import Flask, request
import requests, os, json, re
from datetime import datetime
import openai

# --- Initialize OpenAI client (new SDK style) ---
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# --- In‐memory session store ---
# chat_id → {"structured_data": {...}, "awaiting_correction": bool}
session_data = {}

# --- Blank template for a fresh report ---
def blank_report():
    today = datetime.now().strftime("%d-%m-%Y")
    return {
        "site_name": "",
        "segment": "",
        "category": "",
        "company": [],
        "people": [],
        "tools": [],
        "service": [],
        "activities": [],
        "issues": [],
        "time": "",
        "weather": "",
        "impression": "",
        "comments": "",
        "date": today
    }

# --- Send a message back to Telegram ---
def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)

# --- Fetch and transcribe voice from Telegram via Whisper ---
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{path}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        audio = requests.get(audio_url).content
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return resp.json().get("text","")
    except:
        return ""

# --- Enrich date, ensure not future ---
def enrich_with_date(data):
    today = datetime.now()
    today_str = today.strftime("%d-%m-%Y")
    d = data.get("date","")
    if not d:
        data["date"] = today_str
    else:
        try:
            dt = datetime.strptime(d, "%d-%m-%Y")
            if dt > today:
                data["date"] = today_str
        except:
            data["date"] = today_str
    return data

# --- Summarize the structured data into your template ---
def summarize_data(d):
    lines = []
    lines.append(f"📍 Site: {d.get('site_name','')}")
    lines.append(f"📆 Segment: {d.get('segment','')}")
    lines.append(f"🌿 Category: {d.get('category','')}")
    # companies
    comps = []
    for c in d.get("company",[]):
        if isinstance(c, dict):
            comps.append(c.get("name",""))
        else:
            comps.append(str(c))
    lines.append("🏣 Companies: " + ", ".join(comps))
    # people
    ppl = []
    for p in d.get("people",[]):
        if isinstance(p, dict):
            name = p.get("name","")
            role = p.get("role","")
            ppl.append(f"{name} ({role})" if role else name)
        else:
            ppl.append(str(p))
    lines.append("👷 People: " + ", ".join(ppl))
    # services
    svcs = []
    for s in d.get("service",[]):
        if isinstance(s, dict):
            task = s.get("task","")
            comp = s.get("company","")
            svcs.append(f"{task} ({comp})" if comp else task)
        else:
            svcs.append(str(s))
    lines.append("🔧 Services: " + ", ".join(svcs))
    # tools
    tls = []
    for t in d.get("tools",[]):
        if isinstance(t, dict):
            item = t.get("item","")
            comp = t.get("company","")
            tls.append(f"{item} ({comp})" if comp else item)
        else:
            tls.append(str(t))
    lines.append("🛠️ Tools: " + ", ".join(tls))
    # activities
    lines.append("📋 Activities: " + ", ".join(d.get("activities",[])))
    # issues
    lines.append("⚠️ Issues:")
    for i in d.get("issues",[]):
        if isinstance(i, dict):
            desc = i.get("description","")
            by = i.get("caused_by","")
            photo = " 📸" if i.get("has_photo") else ""
            lines.append(f"• {desc} (by {by}){photo}")
    # time, weather, impression, comments, date
    lines.append(f"⏰ Time: {d.get('time','')}")
    lines.append(f"🌦️ Weather: {d.get('weather','')}")
    lines.append(f"💬 Impression: {d.get('impression','')}")
    lines.append(f"📝 Comments: {d.get('comments','')}")
    lines.append(f"🗓️ Date: {d.get('date','')}")
    return "\n".join(lines)

# --- Extract via GPT ---
gpt_prompt = """
You are an AI assistant extracting a construction site report. Only pull out exactly what’s mentioned.
Return JSON with any of these fields (omit if not said):

site_name, segment, category,
company:[{"name":...}], people:[{"name":...,"role":...}],
tools:[{"item":...,"company":...}], service:[{"task":...,"company":...}],
activities:[...], issues:[{"description":...,"caused_by":...,"has_photo":...}],
time, weather, impression, comments, date (dd-mm-yyyy)
"""

def extract_site_report(text):
    msgs = [
        {"role":"system","content": "You only extract explicitly stated fields, never guess."},
        {"role":"user","content": gpt_prompt + "\n" + text}
    ]
    try:
        res = client.chat.completions.create(model="gpt-3.5-turbo",
                                             messages=msgs, temperature=0.2)
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print("❌ GPT extract error:", e)
        return {}

# --- Correction via GPT ---
def apply_correction(original, correction):
    prompt = f"""
Original JSON:
{json.dumps(original)}

User said correction:
\"{correction}\"

Return the full JSON with only those fields updated.
"""
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print("❌ GPT correction error:", e)
        return original

# --- Flask webhook handler ---
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    # Telegram update
    if "message" not in data:
        return "ok", 200

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text","").strip()

    # Initialize session if needed
    if chat_id not in session_data:
        session_data[chat_id] = {
            "structured_data": blank_report(),
            "awaiting_correction": False
        }

    sess = session_data[chat_id]

    # --- New / Reset command ---
    if text.lower() in ("new", "new report", "/new", "reset"):
        sess["structured_data"] = blank_report()
        sess["awaiting_correction"] = True
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id, "🔄 **Starting a fresh report**\n\n" + tpl + "\n\n✅ Now speak or type your first field.")
        return "ok", 200

    # --- If voice message, transcribe it ---
    if "voice" in msg:
        file_id = msg["voice"]["file_id"]
        text = transcribe_from_telegram_voice(file_id)
        if not text:
            send_telegram_message(chat_id, "⚠️ Couldn't understand audio. Please retry.")
            return "ok", 200

    # --- First pass: no awaiting_correction → extract initial report ---
    if not sess["awaiting_correction"]:
        extracted = extract_site_report(text)
        if not extracted.get("site_name"):
            send_telegram_message(chat_id, "⚠️ Couldn't detect a site name. Please try again.")
            return "ok", 200
        enriched = enrich_with_date(extracted)
        sess["structured_data"] = enriched
        sess["awaiting_correction"] = True
        tpl = summarize_data(enriched)
        send_telegram_message(chat_id, "Here’s what I understood:\n\n" + tpl + "\n\n✅ Is this correct? You can send corrections anytime.")
        return "ok", 200

    # --- Otherwise, we’re in correction mode ---
    # Apply the new text (either a correction or an addition)
    updated = apply_correction(sess["structured_data"], text)
    updated = enrich_with_date(updated)
    sess["structured_data"] = updated
    # still awaiting more corrections
    tpl = summarize_data(updated)
    send_telegram_message(chat_id, "✅ Got it! Here’s the **full** updated report:\n\n" + tpl + "\n\n✅ Anything else to correct?")
    return "ok", 200

if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))
