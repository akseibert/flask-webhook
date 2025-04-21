from flask import Flask, request
import requests
import os
import openai
import json
from twilio.rest import Client
from msal import ConfidentialClientApplication

app = Flask(__name__)

# Validate environment setup
if not all([os.getenv("TENANT_ID"), os.getenv("CLIENT_ID"), os.getenv("CLIENT_SECRET")]):
    raise ValueError("❌ Missing one or more required environment variables: TENANT_ID, CLIENT_ID, CLIENT_SECRET")

# In-memory session store
session_data = {}  # { "whatsapp:+4176...": {"structured_data": {...}} }

def transcribe_audio(media_url):
    response = requests.get(media_url, auth=(
        os.getenv("TWILIO_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    ))

    if response.status_code != 200:
        print(f"❌ Failed to download audio. Status code: {response.status_code}")
        return "[Download failed]"

    audio_data = response.content

    whisper_response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"
        },
        files={"file": ("audio.ogg", audio_data, "audio/ogg")},
        data={"model": "whisper-1"}
    )

    if whisper_response.status_code != 200:
        print(f"❌ Whisper error: {whisper_response.status_code} – {whisper_response.text}")
        return "[Whisper failed]"

    result = whisper_response.json()
    return result.get("text", "[No text found]")

def send_whatsapp_reply(to_number, message):
    client = Client(os.getenv("TWILIO_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    from_number = "whatsapp:" + os.getenv("TWILIO_PHONE_NUMBER")
    if not to_number.startswith("whatsapp:"):
        to_number = "whatsapp:" + to_number

    print(f"📤 Sending WhatsApp message from {from_number} to {to_number}")
    print(f"📤 Message content: {message}")

    client.messages.create(
        body=message,
        from_=from_number,
        to=to_number
    )

def summarize_data(data):
    lines = []
    if "site_name" in data:
        lines.append(f"📍 Site: {data['site_name']}")
    if "segment" in data:
        lines.append(f"📦 Segment: {data['segment']}")
    if "category" in data:
        lines.append(f"🏷️ Category: {data['category']}")
    if "company" in data and isinstance(data["company"], list):
        lines.append("🏣 Companies: " + ", ".join(c.get("name", "") for c in data["company"] if isinstance(c, dict)))
    if "people" in data and isinstance(data["people"], list):
        lines.append("👷 People: " + ", ".join(f"{p.get('name', '')} ({p.get('role', '')})" for p in data["people"] if isinstance(p, dict)))
    if "service" in data and isinstance(data["service"], list):
        lines.append("🔧 Services: " + ", ".join(f"{s.get('task', '')} ({s.get('company', '')})" for s in data["service"] if isinstance(s, dict)))
    if "tools" in data and isinstance(data["tools"], list):
        lines.append("🛠️ Tools: " + ", ".join(f"{t.get('item', '')} ({t.get('company', '')})" for t in data["tools"] if isinstance(t, dict)))
    if "activities" in data and isinstance(data["activities"], list):
        lines.append("📋 Activities: " + ", ".join(data["activities"]))
    if "issues" in data and isinstance(data["issues"], list):
        lines.append("⚠️ Issues:")
        for i in data["issues"]:
            if isinstance(i, dict):
                description = i.get("description", "No description")
                caused_by = i.get("caused_by", "unknown")
                photo_flag = " 📸" if i.get("has_photo") else ""
                lines.append(f"• {description} (by {caused_by}){photo_flag}")
    if "time" in data:
        lines.append(f"⏰ Time: {data['time']}")
    if "weather" in data:
        lines.append(f"🌦️ Weather: {data['weather']}")
    if "impression" in data:
        lines.append(f"💬 Impression: {data['impression']}")
    if "comments" in data:
        lines.append(f"📝 Comments: {data['comments']}")
    return "\n".join(lines)

# (Rest of the code remains unchanged)
