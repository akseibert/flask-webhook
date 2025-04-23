from flask import Flask, request
import requests, os, json
from datetime import datetime
import openai

# -----------------------------------------------------------------------------
#  Configure & initialize
# -----------------------------------------------------------------------------
app = Flask(__name__)

# Use OpenAI’s new Python SDK
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

# In‐memory per‐chat session
session_data = {}  # chat_id → {"data": {...}, "awaiting_correction": bool}

# -----------------------------------------------------------------------------
#  Helpers
# -----------------------------------------------------------------------------
def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    resp = requests.post(url, json=payload)
    print("→", resp.status_code, resp.text)

def today_str():
    return datetime.now().strftime("%d-%m-%Y")

def enrich_date(item):
    d = item.get("date")
    if not d:
        item["date"] = today_str()
    else:
        # if future or unparsable, reset
        try:
            parsed = datetime.strptime(d, "%d-%m-%Y")
            if parsed > datetime.now():
                item["date"] = today_str()
        except:
            item["date"] = today_str()
    return item

def blank_report():
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
        "date": today_str(),
    }

def summarize(data):
    # for each field, if empty show blank label
    lines = []
    lines.append(f"📍 Site: {data['site_name']}")
    lines.append(f"📆 Segment: {data['segment']}")
    lines.append(f"🌿 Category: {data['category']}")
    lines.append("🏣 Companies: " + ", ".join(c["name"] for c in data["company"]))
    lines.append("👷 People: " + ", ".join(f"{p['name']} ({p['role']})" for p in data["people"]))
    lines.append("🛠️ Tools: " + ", ".join(f"{t['item']} ({t.get('company','')})" for t in data["tools"]))
    lines.append("🔧 Services: " + ", ".join(f"{s['task']} ({s.get('company','')})" for s in data["service"]))
    lines.append("📋 Activities: " + ", ".join(data["activities"]))
    # issues
    issue_lines = []
    for i in data["issues"]:
        desc = i.get("description","")
        by   = i.get("caused_by","")
        flag = " 📸" if i.get("has_photo") else ""
        issue_lines.append(f"• {desc} (by {by}){flag}")
    if issue_lines:
        lines.append("⚠️ Issues:")
        lines.extend(issue_lines)
    else:
        lines.append("⚠️ Issues: ")
    lines.append(f"⏰ Time: {data['time']}")
    lines.append(f"🌦️ Weather: {data['weather']}")
    lines.append(f"💬 Impression: {data['impression']}")
    lines.append(f"📝 Comments: {data['comments']}")
    lines.append(f"🗓️ Date: {data['date']}")
    return "\n".join(lines)

# -----------------------------------------------------------------------------
#  Core extraction via GPT
# -----------------------------------------------------------------------------
gpt_template = """
You are a strict assistant. Only extract fields explicitly mentioned. Never guess.
Return JSON with these keys (omit no others):
site_name, segment, category,
company (list of {{name:str}}),
people (list of {{name:str, role:str}}),
tools (list of {{item:str, company:str}}),
service (list of {{task:str, company:str}}),
activities (list of str),
issues (list of {{description:str, caused_by:str, has_photo:bool}}),
time, weather, impression, comments, date (dd-mm-YYYY).
Here is the user input:
---
"""

def extract(text):
    prompt = gpt_template + text
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("GPT error:", e)
        return {}

def apply_correction(orig, corr_text):
    prompt = f"""
Original JSON:
{json.dumps(orig)}

User correction:
"{corr_text}"

Return only the updated JSON, merging changes into original.
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print("Correction error:", e)
        return orig

# -----------------------------------------------------------------------------
#  Webhook
# -----------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    msg = data.get("message", {})
    chat_id = str(msg.get("chat",{}).get("id"))
    text = msg.get("text","").strip().lower()

    # start new
    if text in ("new report","reset","start again"):
        session_data[chat_id] = {
            "data": blank_report(),
            "awaiting_correction": False
        }
        send_telegram(chat_id,
            "🆕 Starting a fresh report.  Here's the blank template:\n\n" +
            summarize(session_data[chat_id]["data"]) +
            "\n\n✅ You can now speak or type your first field."
        )
        return "",200

    # ensure session exists
    if chat_id not in session_data:
        session_data[chat_id] = {"data": blank_report(), "awaiting_correction": False}

    session = session_data[chat_id]

    # corrections flow
    if session["awaiting_correction"]:
        updated = apply_correction(session["data"], msg.get("text",""))
        session["data"] = enrich_date(updated)
        session["awaiting_correction"] = True
        send_telegram(chat_id, "✅ Got it! Here’s the **full** updated report:\n\n" +
            summarize(session["data"]) +
            "\n\n✅ Anything else to correct?"
        )
        return "",200

    # normal extraction
    extracted = extract(msg.get("text",""))
    if not extracted or "site_name" not in extracted:
        send_telegram(chat_id, "⚠️ Sorry, I couldn’t pull out any required fields. Please try again.")
        return "",200

    # merge & kick off confirmation
    merged = session["data"]
    merged.update(extracted)
    merged = enrich_date(merged)
    session["data"] = merged
    session["awaiting_correction"] = True

    send_telegram(chat_id, "Here’s what I understood:\n\n" +
        summarize(merged) +
        "\n\n✅ Is this correct? You can still send corrections."
    )
    return "",200
