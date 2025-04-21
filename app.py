from flask import Flask, request
import requests
import os

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
            return "✅ Voice message transcribed!", 200
        except Exception as e:
            print(f"❌ Error transcribing: {e}")
            return "⚠️ Could not transcribe audio.", 200

    return "✅ Message received!", 200
