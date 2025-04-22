from flask import Flask, request
import requests
import os
import openai
import json
from datetime import datetime

app = Flask(__name__)

# Health check route
@app.route("/", methods=["GET"])
def index():
    return "Running", 200

# In-memory session store
session_data = {}  # { telegram_user_id: {"structured_data": {...}, "awaiting_correction": True/False} }

def send_telegram_message(chat_id, text):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    response = requests.post(url, json=payload)
    print("✅ Telegram message sent:", response.status_code, response.text)

def summarize_data(data):
    lines = []
    if "site_name" in data:
        lines.append(f"📍 Site: {data['site_name']}")
    if "segment" in data:
        lines.append(f"📦 Segment: {data['segment']}")
    if "category" in data:
        lines.append(f"🏷️ Category: {data['category']}")
    if "company" in data and isinstance(data["company"], list):
        lines.append("🏣 Companies: " + ", ".join(c["name"] for c in data["company"] if isinstance(c, dict)))
    if "people" in data and isinstance(data["people"], list):
        lines.append("👷 People: " + ", ".join(f"{p['name']} ({p['role']})" for p in data["people"] if isinstance(p, dict)))
    if "service" in data and isinstance(data["service"], list):
        lines.append("🔧 Services: " + ", ".join(f"{s['task']} ({s['company']})" for s in data["service"] if isinstance(s, dict)))
    if "tools" in data and isinstance(data["tools"], list):
        lines.append("🛠️ Tools: " + ", ".join(f"{t['item']} ({t['company']})" for t in data["tools"] if isinstance(t, dict)))
    if "activities" in data and isinstance(data["activities"], list):
        lines.append("📋 Activities: " + ", ".join(data["activities"]))
    if "issues" in data and isinstance(data["issues"], list):
        lines.append("⚠️ Issues:")
        for i in data["issues"]:
            if isinstance(i, dict):
                caused_by = i.get("caused_by", "unknown")
                has_photo = i.get("has_photo", False)
                lines.append(f"• {i['description']} (by {caused_by}){' 📸' if has_photo else ''}")
    if "time" in data:
        lines.append(f"⏰ Time: {data['time']}")
    if "weather" in data:
        lines.append(f"🌦️ Weather: {data['weather']}")
    if "impression" in data:
        lines.append(f"💬 Impression: {data['impression']}")
    if "comments" in data:
        lines.append(f"📝 Comments: {data['comments']}")
    if "date" in data:
        lines.append(f"📅 Date: {data['date']}")
    return "\n".join(lines)

def enrich_with_date(data):
    today_str = datetime.now().strftime("%Y-%m-%d")
    if "date" not in data or not data["date"]:
        data["date"] = today_str
    else:
        try:
            input_date = datetime.strptime(data["date"], "%Y-%m-%d")
            if input_date > datetime.now():
                data["date"] = today_str  # fallback to today if future
        except Exception as e:
            print("❌ Date format invalid, defaulting to today.", e)
            data["date"] = today_str
    return data

def extract_site_report(text):
    prompt = gpt_prompt_template + f"""
{text}
"""
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0.3,
        messages=[
            {"role": "system", "content": "You only return fields explicitly mentioned in the transcribed message. Never guess or fill missing info."},
            {"role": "user", "content": prompt}
        ]
    )
    try:
        return json.loads(response.choices[0].message["content"])
    except Exception as e:
        print("❌ GPT parsing failed:", e)
        return {}

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print("📩 Telegram webhook received:", json.dumps(data, indent=2))

        if "message" not in data:
            return "No message found", 400

        message_data = data["message"]
        chat_id = str(message_data["chat"]["id"])
        message_text = message_data.get("text", "[No text found]")

        print(f"📩 Message from Telegram user {chat_id}: {message_text}")

        if chat_id not in session_data:
            session_data[chat_id] = {"structured_data": {}, "awaiting_correction": False}

        structured = session_data[chat_id].get("structured_data", {})

        if session_data[chat_id]["awaiting_correction"]:
            updated = apply_correction(structured, message_text)
            session_data[chat_id]["structured_data"] = updated
            session_data[chat_id]["awaiting_correction"] = True  # Allow multiple corrections
            summary = summarize_data(updated)
            send_telegram_message(chat_id, f"✅ Got it! Updated version:\n\n{summary}\n\n✅ Anything else to correct?")
            return "Updated with correction.", 200

        extracted = extract_site_report(message_text)
        if not extracted or "site_name" not in extracted:
            send_telegram_message(chat_id, "⚠️ Sorry, I couldn't detect site info. Please try again.")
            return "Missing required fields", 200

        enriched = enrich_with_date(extracted)
        session_data[chat_id]["structured_data"] = enriched
        session_data[chat_id]["awaiting_correction"] = True
        summary = summarize_data(enriched)

        send_telegram_message(chat_id, f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can still send corrections.")
        return "Summary sent", 200

    except Exception as e:
        print("❌ Error in Telegram webhook:", e)
        return "Error", 500

# GPT Prompt
gpt_prompt_template = """
You are an AI assistant helping extract a construction site report based on a spoken summary from a site manager. 
The user provided voice messages in response to 13 specific questions. You will receive their answers as one full block of text.
You are a strict and accurate assistant. Your task is to extract structured information from a voice transcription made by a site manager on a construction site.

⚠️ Only extract information that is **explicitly mentioned** in the transcribed report. 
❌ Do NOT guess, assume, or infer any missing fields.
❌ Do NOT fill in placeholders like “none,” “no issues,” “unspecified,” or summaries like “a productive day” unless clearly said.
Return a JSON with only the fields that were mentioned.
    
Please extract the following fields as structured JSON:

1. site_name (required)
2. segment (optional)
3. category – high-level topic or type of documentation (e.g. "Abnahme", "Mängelerfassung", "Grundriss", "Besonderheiten", "Zugang")
4. company – list of companies mentioned (e.g. [{"name": "ABC AG"}])
5. people – [{"name": "...", "role": "..."}]
6. tools – [{"item": "...", "company": "..."}]
7. service – [{"task": "...", "company": "..."}]
8. activities – free-form list of where or how service was applied
9. issues – [{"description": "...", "caused_by": "...", "has_photo": true/false}]
10. time – morning / afternoon / evening / full day
11. weather – short description
12. impression – summary or sentiment
13. comments – any additional notes or plans

Only include fields that were explicitly mentioned in the transcribed message.
"""

def apply_correction(original_data, correction_text):
    prompt = f"""
You are helping correct structured site data. This is the original structured JSON:
{json.dumps(original_data, indent=2)}

The user said:
"{correction_text}"

Return the updated JSON with only the corrected fields changed.
"""
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        return json.loads(response.choices[0].message["content"])
    except Exception as e:
        print("❌ GPT correction failed:", e)
        return original_data
