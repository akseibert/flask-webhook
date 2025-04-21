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

def extract_site_report(text):
    prompt = gpt_prompt_template + f"""
{text}
"""
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0.3,
        messages=[
            {"role": "system", "content": "You are a strict assistant. Never guess missing info. Only return fields explicitly stated."},
            {"role": "user", "content": prompt}
        ]
    )
    try:
        return json.loads(response.choices[0].message["content"])
    except Exception as e:
        print(f"❌ Error parsing GPT response: {e}")
        return {}

def apply_correction(original_data, correction_text):
    correction_prompt = f"""
You are helping correct structured site data. This is the original structured JSON:
{json.dumps(original_data)}

The user said:
"{correction_text}"

Return the updated JSON with only the corrected fields changed.
"""
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": correction_prompt}]
    )
    try:
        return json.loads(response.choices[0].message["content"])
    except Exception as e:
        print(f"❌ Error parsing correction response: {e}")
        return original_data

@app.route("/webhook", methods=["POST"])
def webhook():
    sender = request.form.get("From")
    message = request.form.get("Body")
    media_url = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0")

    print(f"📩 Message from {sender}: {message}")

    if media_url and "audio" in media_type:
        transcription = transcribe_audio(media_url)
        print(f"🗣 Transcription: {transcription}")

        if sender in session_data and session_data[sender].get("awaiting_correction"):
            updated = apply_correction(session_data[sender]["structured_data"], transcription)
            session_data[sender]["structured_data"] = updated
            session_data[sender]["awaiting_correction"] = False
            reply = f"✅ Got it! Here's the updated version:\n\n{summarize_data(updated)}"
            send_whatsapp_reply(sender, reply)
            return "Updated with correction.", 200

        structured = extract_site_report(transcription)

        for field in ["impression", "time", "weather", "comments", "category"]:
            if field in structured and not structured[field]:
                del structured[field]

        if not structured or "site_name" not in structured:
            send_whatsapp_reply(sender, "Hmm, I didn’t catch any clear site information. Could you try again?")
            return "⚠️ GPT returned empty or invalid data", 200

        print("🧠 Structured data:\n" + json.dumps(structured, indent=2, ensure_ascii=False))

        session_data[sender] = {
            "structured_data": structured,
            "awaiting_correction": True
        }

        summary = summarize_data(structured)
        confirm_msg = f"Here’s what I understood:\n\n{summary}\n\n✅ Is this correct? You can also send corrections via text or voice."
        send_whatsapp_reply(sender, confirm_msg)
        return "Summary sent for confirmation.", 200

    if sender in session_data and session_data[sender].get("awaiting_correction") and message:
        updated = apply_correction(session_data[sender]["structured_data"], message)
        session_data[sender]["structured_data"] = updated
        session_data[sender]["awaiting_correction"] = False
        reply = f"✅ Got it! Here's the updated version:\n\n{summarize_data(updated)}"
        send_whatsapp_reply(sender, reply)
        return "Updated with correction.", 200

    send_whatsapp_reply(sender, "Thanks! You can speak your report or send a correction.")
    return "✅ Message processed", 200

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
