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

def blank_report():
    """Return a fresh, empty report with today's date."""
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

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)

def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    return "https://api.telegram.org/file/bot{}/{}".format(token, r.json()["result"]["file_path"])

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
    except Exception as e:
        print("❌ Transcription failed:", e)
        return ""

def enrich_with_date(data):
    today = datetime.now().strftime("%d-%m-%Y")
    d = data.get("date","")
    if not d:
        data["date"] = today
    else:
        try:
            parsed = datetime.strptime(d, "%d-%m-%Y")
            if parsed > datetime.now():
                data["date"] = today
        except:
            data["date"] = today
    return data

def summarize_data(d):
    lines = []
    lines.append(f"📍 Site: {d.get('site_name','')}")
    lines.append(f"📆 Segment: {d.get('segment','')}")
    lines.append(f"🌿 Category: {d.get('category','')}")
    comps = [c.get("name","") if isinstance(c,dict) else str(c) for c in d.get("company",[])]
    lines.append("🏣 Companies: " + ", ".join(comps))
    ppl = []
    for p in d.get("people",[]):
        if isinstance(p,dict):
            name, role = p.get("name",""), p.get("role","")
            ppl.append(f"{name} ({role})" if role else name)
        else:
            ppl.append(str(p))
    lines.append("👷 People: " + ", ".join(ppl))
    svcs = []
    for s in d.get("service",[]):
        if isinstance(s,dict):
            t, c = s.get("task",""), s.get("company","")
            svcs.append(f"{t} ({c})" if c else t)
        else:
            svcs.append(str(s))
    lines.append("🔧 Services: " + ", ".join(svcs))
    tls = []
    for t in d.get("tools",[]):
        if isinstance(t,dict):
            i, c = t.get("item",""), t.get("company","")
            tls.append(f"{i} ({c})" if c else i)
        else:
            tls.append(str(t))
    lines.append("🛠️ Tools: " + ", ".join(tls))
    lines.append("📋 Activities: " + ", ".join(d.get("activities",[])))
    lines.append("⚠️ Issues:")
    for i in d.get("issues",[]):
        if isinstance(i,dict):
            desc, by = i.get("description",""), i.get("caused_by","")
            photo = " 📸" if i.get("has_photo") else ""
            lines.append(f"• {desc} (by {by}){photo}")
    lines.append(f"⏰ Time: {d.get('time','')}")
    lines.append(f"🌦️ Weather: {d.get('weather','')}")
    lines.append(f"💬 Impression: {d.get('impression','')}")
    lines.append(f"📝 Comments: {d.get('comments','')}")
    lines.append(f"🗓️ Date: {d.get('date','')}")
    return "\n".join(lines)

# GPT prompt template
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
        {"role":"system","content":"Only extract explicitly stated fields; never guess."},
        {"role":"user","content":gpt_prompt + "\n" + text}
    ]
    try:
        res = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=msgs, temperature=0.2
        )
        content = res.choices[0].message.content.strip()
        data = json.loads(content)
        return data
    except Exception as e:
        print("❌ GPT extract error:", e)
        # fallback: regex for “at the X segment”
        m = re.search(r'at the ([^,]+?) segment', text, re.IGNORECASE)
        if m:
            return {"site_name": m.group(1).title()}
        return {}

def apply_correction(original, correction):
    prompt = f"""
Original JSON:
{json.dumps(original)}

User correction:
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

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if "message" not in data:
        return "ok", 200

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    # init session
    if chat_id not in session_data:
        session_data[chat_id] = {
            "structured_data": blank_report(),
            "awaiting_correction": False
        }
    sess = session_data[chat_id]

    # new/reset
    if text.lower() in ("new","new report","/new","reset"):
        sess["structured_data"] = blank_report()
        sess["awaiting_correction"] = True
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            "🔄 **Starting a fresh report**\n\n" + tpl +
            "\n\n✅ Now speak or type your first field."
        )
        return "ok",200

    # voice?
    if "voice" in msg:
        file_id = msg["voice"]["file_id"]
        text = transcribe_from_telegram_voice(file_id)
        if not text:
            send_telegram_message(chat_id,
                "⚠️ Couldn't understand audio. Please retry.")
            return "ok",200

    # first extraction
    if not sess["awaiting_correction"]:
        extracted = extract_site_report(text)
        if not extracted.get("site_name"):
            send_telegram_message(chat_id,
                "⚠️ Couldn't detect a site name. Please try again.")
            return "ok",200
        enriched = enrich_with_date(extracted)
        sess["structured_data"] = enriched
        sess["awaiting_correction"] = True
        tpl = summarize_data(enriched)
        send_telegram_message(chat_id,
            "Here’s what I understood:\n\n" + tpl +
            "\n\n✅ Is this correct? You can send corrections anytime."
        )
        return "ok",200

    # corrections/additions
    updated = apply_correction(sess["structured_data"], text)
    updated = enrich_with_date(updated)
    sess["structured_data"] = updated
    tpl = summarize_data(updated)
    send_telegram_message(chat_id,
        "✅ Got it! Here’s the **full** updated report:\n\n" + tpl +
        "\n\n✅ Anything else to correct?"
    )
    return "ok",200

if __name__=="__main__":
    app.run(port=int(os.getenv("PORT",5000)), debug=True)
