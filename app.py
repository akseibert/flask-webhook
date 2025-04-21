from flask import Flask, request
import requests
import os
import openai
import json
from twilio.rest import Client  # Twilio Messaging for auto-reply

app = Flask(__name__)

# Voice-to-text helper using Whisper
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

# GPT prompt for extracting structured site report
gpt_prompt_template = """
You are an AI assistant helping extract a construction site report based on a spoken summary from a site manager. 
The user provided voice messages in response to 10 specific questions. You will receive their answers as one full block of text.

Please extract the following fields as structured JSON:

1. site_name (required)
2. segment (optional)
3. category – high-level topic or type of documentation (e.g. "Abnahme", "Mängelerfassung", "Grundriss", "Besonderheiten", "Zugang")
4. company – list of companies mentioned (e.g. [{"name": "ABC AG"}, {"name": "Müller Tiefbau"}])
Only include named individuals under "people".  
- If the input says “Company Müller” or “Müller AG worked with its team,” treat "Müller" as a company, not a person.  
- Do not invent people or roles unless explicitly stated.  
- If a company worked but no individual names are mentioned, list it only under `company`, `tools`, or `service`, and leave out `people`.

5. people – [{"name": "...", "role": "..."}]  
6. tools – [{"item": "...", "company": "..."}] – company may be listed more than once here
7. service – [{"task": "...", "company": "..."}] – what was done (e.g. "tiling", "concrete pouring")
8. activities – free-form list of where or how service was applied (e.g. "on ground floor", "in unit 4A")
9. issues – [{"description": "...", "caused_by": "...", "has_photo": true/false}]
10. time – morning / afternoon / evening / full day
11. weather – short description
12. impression – summary or sentiment
13. comments – any additional notes or plans

If a photo was sent after a message about an issue, set has_photo to true.
Only include fields that were explicitly mentioned in the transcribed message.  
Do not guess or infer missing values.  
If something is unclear or not said, omit it entirely — do not fill in with defaults like "full day", "no notes", or positive impressions.  
Return only actual information said by the user.

Here is the full transcribed report:
\"\"\"{{transcribed_report}}\"\"\"
"""

def extract_site_report(transcribed_text):
    full_prompt = gpt_prompt_template.replace("{{transcribed_report}}", transcribed_text)

    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0.3,
        messages=[{"role": "user", "content": full_prompt}]
    )

    reply = response.choices[0].message["content"]

    try:
        result = json.loads(reply)
    except Exception as e:
        print(f"❌ GPT did not return valid JSON. Error: {e}")
        print("🧠 Raw GPT reply:")
        print(reply)
        result = {}

    return result

# Twilio reply helper
def send_whatsapp_reply(to_number, message):
    account_sid = os.getenv("TWILIO_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    client = Client(account_sid, auth_token)

    from_number = "whatsapp:" + os.getenv("TWILIO_PHONE_NUMBER")
    if not to_number.startswith("whatsapp:"):
        to_number = "whatsapp:" + to_number

    print(f"📤 Sending WhatsApp reply to: {to_number}")
    print(f"📤 From bot number: {from_number}")

    client.messages.create(
        body=message,
        from_=from_number,
        to=to_number
    )

@app.route("/webhook", methods=["POST"])
def webhook():
    sender = request.form.get("From")
    message = request.form.get("Body")
    media_url = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0")

    print(f"📩 Message from {sender}: {message}")
    print(f"📎 Media URL: {media_url}")
    print(f"📎 Media Type: {media_type}")

    if media_url and "audio" in media_type:
        try:
            transcription = transcribe_audio(media_url)
            print(f"🗣 Transcription from {sender}: {transcription}")

            if transcription.strip() == "[No text found]":
                print("❌ Whisper failed to transcribe speech.")
                send_whatsapp_reply(sender, "Sorry, I couldn’t hear what you said. Could you please repeat it?")
                return "⚠️ No transcribable text.", 200

            structured_data = extract_site_report(transcription)

            if not structured_data:
                print("❌ GPT returned no usable data.")
                send_whatsapp_reply(sender, "Hmm, I didn’t catch any site details. Could you repeat what happened today?")
                return "⚠️ GPT returned no data.", 200

            # Clean out optional fields with empty strings
            for key in ["impression", "time", "weather", "comments", "category"]:
                if key in structured_data and (structured_data[key] == "" or structured_data[key] is None):
                    del structured_data[key]

            print("🧠 Structured info:\n" + json.dumps(structured_data, indent=2, ensure_ascii=False))

            send_whatsapp_reply(sender, "Thanks! I’ve received your update. Let me know who worked with you and what their roles were, if you haven’t said that yet.")

            return "✅ Voice message transcribed, analyzed, and replied.", 200

        except Exception as e:
            print(f"❌ Error during processing: {e}")
            send_whatsapp_reply(sender, "Oops, something went wrong while analyzing your message.")
            return "⚠️ Could not transcribe and analyze audio.", 200

    return "✅ Message received!", 200
