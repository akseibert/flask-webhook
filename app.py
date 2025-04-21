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

            # 🚿 Clean out optional fields with empty strings
            for key in ["impression", "time", "weather", "comments"]:
                if key in structured_data and (structured_data[key] == "" or structured_data[key] is None):
                    del structured_data[key]

            print("🧠 Structured info:\n" + json.dumps(structured_data, indent=2, ensure_ascii=False))

            # 💬 Smarter auto-reply instead of always asking for people/roles
            send_whatsapp_reply(sender, "Thanks! I’ve received your update. Let me know who worked with you and what their roles were, if you haven’t said that yet.")

            return "✅ Voice message transcribed, analyzed, and replied.", 200

        except Exception as e:
            print(f"❌ Error during processing: {e}")
            send_whatsapp_reply(sender, "Oops, something went wrong while analyzing your message.")
            return "⚠️ Could not transcribe and analyze audio.", 200

    return "✅ Message received!", 200
