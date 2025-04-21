# ✅ Full working chatbot backend with persistent corrections
from flask import Flask, request
import requests
import os
import openai
import json
from twilio.rest import Client
from msal import ConfidentialClientApplication

app = Flask(__name__)

# Session store to keep user progress
session_data = {}  # e.g. {"whatsapp:+4176...": {"structured_data": {...}, "awaiting_correction": True}}

# Ensure environment is set up correctly
for var in ["TENANT_ID", "CLIENT_ID", "CLIENT_SECRET"]:
    if not os.getenv(var):
        raise ValueError(f"❌ Missing env variable: {var}")


def transcribe_audio(media_url):
    response = requests.get(media_url, auth=(os.getenv("TWILIO_SID"), os.getenv("TWILIO_AUTH_TOKEN")))
    if response.status_code != 200:
        return "[Download failed]"
    audio_data = response.content
    whisper_response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
        files={"file": ("audio.ogg", audio_data, "audio/ogg")},
        data={"model": "whisper-1"}
    )
    return whisper_response.json().get("text", "[No text found]")


def send_whatsapp_reply(to, msg):
    client = Client(os.getenv("TWILIO_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    from_num = "whatsapp:" + os.getenv("TWILIO_PHONE_NUMBER")
    if not to.startswith("whatsapp:"):
        to = "whatsapp:" + to
    print(f"📤 Sending to {to}:\n{msg}")
    client.messages.create(body=msg, from_=from_num, to=to)


def summarize_data(data):
    lines = []
    if "site_name" in data:
        lines.append(f"📍 Site: {data['site_name']}")
    if "segment" in data:
        lines.append(f"📦 Segment: {data['segment']}")
    if "category" in data:
        lines.append(f"🏷️ Category: {data['category']}")
    if "company" in data:
        lines.append("🏣 Companies: " + ", ".join(c['name'] for c in data.get("company", []) if 'name' in c))
    if "people" in data:
        lines.append("👷 People: " + ", ".join(f"{p['name']} ({p['role']})" for p in data.get("people", [])))
    if "service" in data:
        lines.append("🔧 Services: " + ", ".join(f"{s['task']} ({s['company']})" for s in data.get("service", [])))
    if "tools" in data:
        lines.append("🛠️ Tools: " + ", ".join(f"{t['item']} ({t['company']})" for t in data.get("tools", [])))
    if "activities" in data:
        lines.append("📋 Activities: " + ", ".join(data.get("activities", [])))
    if "issues" in data:
        lines.append("⚠️ Issues:")
        for i in data.get("issues", []):
            lines.append(f"* {i['description']} (by {i['caused_by']}){' 📸' if i.get('has_photo') else ''}")
    if "time" in data:
        lines.append(f"⏰ Time: {data['time']}")
    if "weather" in data:
        lines.append(f"🌦️ Weather: {data['weather']}")
    if "impression" in data:
        lines.append(f"💬 Impression: {data['impression']}")
    if "comments" in data:
        lines.append(f"📝 Comments: {data['comments']}")
    return "\n".join(lines)


def extract_site_report(text):
    prompt = gpt_prompt_template + f"\n{text}"
    messages = [
        {"role": "system", "content": "You only return fields explicitly mentioned in the transcribed message. Never guess or fill missing info."},
        {"role": "user", "content": prompt}
    ]
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0.3,
        messages=messages
    )
    return json.loads(response.choices[0].message.content)


def apply_correction(original_data, correction_text):
    prompt = f"""
You are correcting JSON site report data. This is the current JSON:
{json.dumps(original_data, ensure_ascii=False)}

The user said:
"{correction_text}"

Update ONLY the fields the user intended to change. Return full updated JSON.
"""
    messages = [
        {"role": "system", "content": "You update the structured JSON, preserving unchanged fields."},
        {"role": "user", "content": prompt}
    ]
    response = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=messages)
    return json.loads(response.choices[0].message.content)


@app.route("/webhook", methods=["POST"])
def webhook():
    sender = request.form.get("From")
    message = request.form.get("Body")
    media_url = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0")

    if media_url and "audio" in media_type:
        transcription = transcribe_audio(media_url)
        print(f"🗣 Transcription: {transcription}")

        structured = extract_site_report(transcription)
        if not structured or "site_name" not in structured:
            send_whatsapp_reply(sender, "❌ I couldn't extract valid data. Try again?")
            return "Failed", 200

        session_data[sender] = {
            "structured_data": structured,
            "awaiting_correction": True
        }
        summary = summarize_data(structured)
        send_whatsapp_reply(sender, f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? Reply with corrections or say yes.")
        return "Processed voice", 200

    if sender in session_data and session_data[sender].get("awaiting_correction"):
        current = session_data[sender]["structured_data"]
        updated = apply_correction(current, message)
        session_data[sender]["structured_data"] = updated  # Allow further corrections
        summary = summarize_data(updated)
        send_whatsapp_reply(sender, f"✅ Got it! Updated version:\n\n{summary}\n\n✅ Anything else to correct?")
        return "Processed correction", 200

    send_whatsapp_reply(sender, "👋 Hi! Please send a voice message with your site report.")
    return "Default fallback", 200


# 🎯 Prompt Template
gpt_prompt_template = """
You are an AI assistant helping extract a construction site report based on a voice message from a site manager.
Only extract information explicitly mentioned in the transcription.

Return a JSON with only these fields:
1. site_name
2. segment
3. category
4. company – list: [{"name": "..."}]
5. people – [{"name": "...", "role": "..."}]
6. tools – [{"item": "...", "company": "..."}]
7. service – [{"task": "...", "company": "..."}]
8. activities – list of strings
9. issues – [{"description": "...", "caused_by": "...", "has_photo": true/false}]
10. time
11. weather
12. impression
13. comments
"""
