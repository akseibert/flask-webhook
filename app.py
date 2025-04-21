from flask import Flask, request
import requests
import os
import openai
import json

app = Flask(__name__)

# Voice-to-text helper using Whisper
def transcribe_audio(media_url):
    # Fetch the audio securely using Twilio credentials
    response = requests.get(media_url, auth=(
        os.getenv("TWILIO_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    ))

    if response.status_code != 200:
        print(f"❌ Failed to download audio. Status code: {response.status_code}")
        return "[Download failed]"

    audio_data = response.content

    # Now send it to Whisper
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
3. people – [{"name": "...", "role": "..."}]- Only include individuals. If a company is mentioned as working, it should go under service or tools instead.
4. tools – [{"item": "...", "company": "..."}]
5. service – [{"task": "...", "company": "..."}]
6. activities
7. issues – [{"description": "...", "caused_by": "...", "has_photo": true/false}]
8. time
9. weather
10. impression
11. comments

If a photo was sent after a message about an issue, set has_photo to true. If something is not mentioned, leave it out of the JSON.

Here is the full transcribed report:
\"\"\"{{transcribed_report}}\"\"\"
"""

# GPT function to extract structured data
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
    except:
        print("❌ GPT did not return valid JSON:")
        print(reply)
        result = {}

    return result

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

            # NEW: GPT extracts structured report from transcription
            structured_data = extract_site_report(transcription)
            print(f"🧠 Structured info:\n{json.dumps(structured_data, indent=2)}")

            return "✅ Voice message transcribed and analyzed!", 200
        except Exception as e:
            print(f"❌ Error during processing: {e}")
            return "⚠️ Could not transcribe and analyze audio.", 200

    return "✅ Message received!", 200
