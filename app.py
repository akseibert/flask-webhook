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
