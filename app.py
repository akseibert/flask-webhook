from flask import Flask, request

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    sender = request.form.get("From")
    message = request.form.get("Body")
    print(f"📩 Message from {sender}: {message}")
    return "✅ Message received!", 200
