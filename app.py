from flask import Flask, request
import requests, os, json, re
from datetime import datetime
import openai

# --- Initialize OpenAI client ---
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# --- In‐memory session store ---
session_data = {}  # chat_id → {"structured_data": {...}, "awaiting_correction": bool}

def blank_report():
    today = datetime.now().strftime("%d-%m-%Y")
    return {
        "site_name": "", "segment": "", "category": "",
        "company": [], "people": [], "tools": [], "service": [],
        "activities": [], "issues": [],
        "time": "", "weather": "", "impression": "",
        "comments": "", "date": today
    }

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    fp = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{fp}"

def transcribe_from_telegram_voice(file_id):
    try:
        audio = requests.get(get_telegram_file_path(file_id)).content
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"}
        )
        return resp.json().get("text","")
    except Exception as e:
        print("❌ Transcription failed:", e)
        return ""

def enrich_with_date(d):
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

def summarize_data(d):
    lines = []
    lines.append(f"📍 Site: {d.get('site_name','')}")
    lines.append(f"📆 Segment: {d.get('segment','')}")
    lines.append(f"🌿 Category: {d.get('category','')}")
    lines.append(
        "🏣 Companies: " + 
        ", ".join(c.get("name","") if isinstance(c,dict) else str(c) 
                  for c in d.get("company",[]))
    )
    lines.append(
        "👷 People: " + 
        ", ".join(
            f"{p.get('name','')} ({p.get('role','')})" if isinstance(p,dict) else str(p)
            for p in d.get("people",[])
        )
    )
    lines.append(
        "🔧 Services: " + 
        ", ".join(
            f"{s.get('task','')} ({s.get('company','')})" if isinstance(s,dict) else str(s)
            for s in d.get("service",[])
        )
    )
    lines.append(
        "🛠️ Tools: " + 
        ", ".join(
            f"{t.get('item','')} ({t.get('company','')})" if isinstance(t,dict) else str(t)
            for t in d.get("tools",[])
        )
    )
    lines.append("📋 Activities: " + ", ".join(d.get("activities",[])))

    valid_issues = [
        i for i in d.get("issues",[]) 
        if isinstance(i,dict) and i.get("description","").strip()
    ]
    lines.append("⚠️ Issues:")
    for i in valid_issues:
        desc = i["description"]
        by = i.get("caused_by","")
        photo = " 📸" if i.get("has_photo") else ""
        extra = f" (by {by})" if by else ""
        lines.append(f"• {desc}{extra}{photo}")

    lines.append(f"⏰ Time: {d.get('time','')}")
    lines.append(f"🌦️ Weather: {d.get('weather','')}")
    lines.append(f"💬 Impression: {d.get('impression','')}")
    lines.append(f"📝 Comments: {d.get('comments','')}")
    lines.append(f"🗓️ Date: {d.get('date','')}")
    return "\n".join(lines)

gpt_prompt = """
You are an AI assistant extracting a construction site report. Only extract what’s explicitly mentioned.
Return JSON with any of these fields (omit if not present):
site_name, segment, category,
company:[{"name":...}], people:[{"name":...,"role":...}],
tools:[{"item":...,"company":...}], service:[{"task":...,"company":...}],
activities:[...], issues:[{"description":...,"caused_by":...,"has_photo":...}],
time, weather, impression, comments, date (dd-mm-yyyy)
"""

def extract_site_report(text):
    msgs = [
        {"role":"system","content":"Only extract explicitly stated fields; never guess."},
        {"role":"user","content":gpt_prompt + "\n" + text}
    ]
    try:
        r = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=msgs, temperature=0.2
        )
        return json.loads(r.choices[0].message.content)
    except Exception as e:
        print("❌ GPT extract error:", e)
        return {}

def apply_correction(orig, corr):
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nUser said:\n\"" + corr + "\"\n\n"
        "Return JSON with only corrected fields."
    )
    try:
        r = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        part = json.loads(r.choices[0].message.content)
        merged = orig.copy()
        merged.update(part)
        return merged
    except Exception as e:
        print("❌ GPT correction error:", e)
        return orig

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if "message" not in data:
        return "ok",200

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    # init
    if chat_id not in session_data:
        session_data[chat_id] = {
            "structured_data": blank_report(),
            "awaiting_correction": False
        }
    sess = session_data[chat_id]

    # reset
    if text.lower() in ("new","new report","reset","/new"):
        sess["structured_data"] = blank_report()
        sess["awaiting_correction"] = True
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            "🔄 **Starting a fresh report**\n\n" + tpl +
            "\n\n✅ Speak or type your first field."
        )
        return "ok",200

    # voice
    if "voice" in msg:
        t = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        if not t:
            send_telegram_message(chat_id,
                "⚠️ Couldn't understand the audio. Please try again.")
            return "ok",200
        text = t

    # first extract
    if not sess["awaiting_correction"]:
        ext = extract_site_report(text)
        if not ext.get("site_name"):
            send_telegram_message(chat_id,
                "⚠️ Couldn't detect a site name. Please try again.")
            return "ok",200
        sess["structured_data"] = enrich_with_date(ext)
        sess["awaiting_correction"] = True
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            "Here’s what I understood:\n\n" + tpl +
            "\n\n✅ Is this correct? You can correct anytime."
        )
        return "ok",200

    # correction/addition
    updated = apply_correction(sess["structured_data"], text)
    sess["structured_data"] = enrich_with_date(updated)
    tpl = summarize_data(sess["structured_data"])
    send_telegram_message(chat_id,
        "✅ Got it! Here’s the **full** updated report:\n\n" + tpl +
        "\n\n✅ Anything else to correct?"
    )
    return "ok",200

if __name__=="__main__":
    app.run(port=int(os.getenv("PORT",5000)), debug=True)
