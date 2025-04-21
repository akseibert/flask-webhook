from flask import Flask, request
import requests
import os

app = Flask(__name__)

# Voice-to-text helper using Whisper
def transcribe_audio(media_url):
    # Download the voice message from Twilio using basic auth
    audio_data = requests.get(media_url, auth=(
        os.getenv("TWILIO_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    )).content

    # Send to Whisper for transcription
    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"
        },
        files={"file": ("audio.ogg", audio_data, "audio/ogg")},
        data={"model": "whisper-1"}
    )

    result = response.json()
    return result.get("text", "[No text found]")

@app.route("/webhook", methods=["POST"])
def webhook():
    sender = request.form.get("From")
    message = request.form.get("Body")
    media_url = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0")

    print(f"📩 Message from {sender}: {message}")

    if media_url and "audio" in media_type:
        transcription = transcribe_audio(media_url)
        print(f"🗣 Transcription from {sender}: {transcription}")
        return "✅ Voice message transcribed!", 200

    return "✅ Message received!", 200
