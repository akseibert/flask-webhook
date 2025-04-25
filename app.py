import os
import json
import logging
import requests

from datetime import datetime
from flask import Flask, request
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

# --- Logging setup ---
logging.basicConfig(
    filename="/opt/render/project/src/app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())

# --- OpenAI client ---
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    logger.info("OpenAI client initialized")
except Exception as e:
    logger.error(f"OpenAI client init failed: {e}")
    raise

app = Flask(__name__)

# --- Session storage ---
SESSION_FILE = "/opt/render/project/src/session_data.json"

def load_session_data():
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading session data: {e}")
    return {}

def save_session_data(data):
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving session data: {e}")

session_data = load_session_data()

def blank_report():
    today = datetime.now().strftime("%d-%m-%Y")
    return {
        "site_name": "", "segment": "", "category": "",
        "company": [], "people": [], "tools": [], "service": [],
        "activities": [], "issues": [],
        "time": "", "weather": "", "impression": "",
        "comments": "", "date": today
    }

# --- Telegram helpers ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    })
    resp.raise_for_status()
    logger.info(f"Sent to {chat_id}: {text[:50]}…")
    return resp

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    resp = requests.get(url)
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        logger.info(f"Fetching audio: {audio_url}")
        r = requests.get(audio_url)
        r.raise_for_status()
        audio_bytes = r.content

        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio_bytes, "audio/ogg")
        )
        text = getattr(result, "text", "").strip()
        if not text:
            logger.warning(f"Empty transcription, full result: {result}")
            return ""
        logger.info(f"Transcribed: {text}")
        return text
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return ""

# --- Data extraction + merge + formatting ---
gpt_prompt = """
You are an AI assistant extracting a construction site report from text. Return JSON with only explicitly mentioned fields.
Possible fields:
- site_name, segment, category, time, weather, impression, comments, date (dd-mm-yyyy)
- company: [{"name": "..."}]
- people: [{"name": "...", "role": "..."}]
- tools: [{"item": "...", "company": "..."}]
- service: [{"task": "...", "company": "..."}]
- activities: ["..."]
- issues: [{"description": "...", "caused_by": "...", "has_photo": true}]
Omit empty or unclear fields. Return {} if none.
Input text: {text}
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    if not text.strip():
        return {}
    messages = [
        {"role": "system", "content": "Extract only explicitly stated fields; never guess."},
        {"role": "user", "content": gpt_prompt.replace("{text}", text)}
    ]
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
        return data
    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return {}

def merge_structured_data(existing, new):
    merged = existing.copy()
    for key, val in new.items():
        if key in ["company","people","tools","service","activities","issues"]:
            lst = merged.get(key, [])
            for item in (val or []):
                if key == "issues":
                    if not isinstance(item, dict) or "description" not in item:
                        continue
                    if not any(i.get("description")==item["description"] for i in lst if isinstance(i,dict)):
                        lst.append(item)
                elif item not in lst:
                    lst.append(item)
            merged[key] = lst
        else:
            if val:
                merged[key] = val
    return merged

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def apply_correction(orig, corr):
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nUser correction:\n\"" + corr + "\"\n\n"
        "Return JSON with only corrected fields."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        part = json.loads(resp.choices[0].message.content)
        merged = orig.copy()
        merged.update(part)
        return merged
    except Exception as e:
        logger.error(f"Correction error: {e}")
        return orig

def delete_from_report(data, target):
    t = target.strip().lower()
    updated = data.copy()
    # scalar fields
    for field in ["site_name","segment","category","time","weather","impression","comments","date"]:
        if t == field or t.startswith(field+" "):
            updated[field] = ""
            return updated
    # list fields
    mapping = {"company":"name","people":"name","tools":"item","service":"task","activities":None,"issues":"description"}
    for field,key in mapping.items():
        if t.startswith(field+" ") or (key and t.startswith(key+" ")):
            val = t.split(" ",1)[1].strip()
            lst = updated.get(field,[])
            if key:
                lst = [i for i in lst if i.get(key,"").lower()!=val]
            else:
                lst = [i for i in lst if i.lower()!=val]
            updated[field] = lst
            return updated
    return updated

def enrich_with_date(d):
    today = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today
    else:
        try:
            dt = datetime.strptime(d["date"], "%d-%m-%Y")
            if dt > datetime.now():
                d["date"] = today
        except:
            d["date"] = today
    return d

def summarize_data(d):
    lines = []
    lines.append(f"🏗️ **Site**: {d.get('site_name','')}")
    lines.append(f"🛠️ **Segment**: {d.get('segment','')}")
    lines.append(f"📋 **Category**: {d.get('category','')}")
    lines.append("🏢 **Companies**: " + ", ".join(c.get("name","") for c in d.get("company",[])))
    lines.append("👷 **People**: " + ", ".join(f\"{p.get('name')} ({p.get('role')})\" for p in d.get("people",[])))
    lines.append("🔧 **Services**: " + ", ".join(f\"{s.get('task')} ({s.get('company')})\" for s in d.get("service",[])))
    lines.append("🛠️ **Tools**: " + ", ".join(f\"{t.get('item')} ({t.get('company')})\" for t in d.get("tools",[])))
    lines.append("📅 **Activities**: " + ", ".join(d.get("activities",[])))
    lines.append("⚠️ **Issues**:")
    for i in d.get("issues",[]):
        desc = i.get("description","")
        by = i.get("caused_by","")
        photo = " 📸" if i.get("has_photo") else ""
        extra = f" (by {by})" if by else ""
        lines.append(f"  • {desc}{extra}{photo}")
    lines.append(f"⏰ **Time**: {d.get('time','')}")
    lines.append(f"🌦️ **Weather**: {d.get('weather','')}")
    lines.append(f"😊 **Impression**: {d.get('impression','')}")
    lines.append(f"💬 **Comments**: {d.get('comments','')}")
    lines.append(f"📆 **Date**: {d.get('date','')}")
    return "\n".join(lines)

# --- HTTP routes ---
@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True)
        msg = payload.get("message", {})
        chat_id = str(msg.get("chat",{}).get("id",""))

        # get text from voice or text
        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
        else:
            text = (msg.get("text") or "").strip()

        logger.info(f"Incoming [{chat_id}]: {text}")

        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False
            }
        sess = session_data[chat_id]

        # reset
        if text.lower() in ("new","new report","reset","/new"):
            sess["structured_data"] = blank_report()
            sess["awaiting_correction"] = False
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "**Starting a fresh report**\n\n" + tpl +
                "\n\nTell me any details.")
            return "ok", 200

        # delete
        if text.lower().startswith("delete "):
            target = text[7:].strip()
            sess["structured_data"] = delete_from_report(sess["structured_data"], target)
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Deleted '{target}'.\n\n" + tpl)
            return "ok", 200

        # empty or transcription failed
        if not text:
            send_telegram_message(chat_id,
                "Couldn't understand the audio or message. Please try again.")
            return "ok", 200

        extracted = extract_site_report(text)
        if not extracted and not sess["awaiting_correction"]:
            send_telegram_message(chat_id,
                "I couldn't extract any info. Please try like 'Issue: Delayed delivery'.")
            return "ok", 200

        if sess["awaiting_correction"]:
            corrected = apply_correction(sess["structured_data"], text)
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(corrected)
            )
            sess["awaiting_correction"] = False
        else:
            sess["structured_data"] = merge_structured_data(
                sess["structured_data"], enrich_with_date(extracted)
            )
            sess["awaiting_correction"] = True

        save_session_data(session_data)
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            "Here's what I understood:\n\n" + tpl +
            "\n\nIs this correct? Reply to confirm or correct.")
        return "ok", 200

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return "error", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
