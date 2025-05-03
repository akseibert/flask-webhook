import os
import logging
from time import time
from flask import Flask, request
from config import ENV_VARS, SESSION_FILE, PAUSE_THRESHOLD, MAX_HISTORY
from utils import load_session_data, save_session_data, blank_report, enrich_with_date, summarize_data
from handlers import extract_single_command, merge_structured_data
from services import send_telegram_message, transcribe_from_telegram_voice

app = Flask(__name__)

# Logging setup
logging.basicConfig(filename="/opt/render/project/src/app.log", level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())

# Validate environment variables
for var in ENV_VARS["required"]:
    if not os.getenv(var):
        logger.error(f"Missing required environment variable: {var}")
        raise ValueError(f"Missing {var}")
for var in ENV_VARS["optional"]:
    if not os.getenv(var):
        logger.warning(f"Optional environment variable {var} not set")

# Load session data
session_data = load_session_data()

@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram webhook requests."""
    try:
        data = request.get_json(force=True)
        if "message" not in data:
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()

        # Initialize session if new chat
        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False,
                "last_interaction": time(),
                "pending_input": None,
                "awaiting_reset_confirmation": False,
                "command_history": deque(maxlen=MAX_HISTORY)
            }
        sess = session_data[chat_id]

        # Handle voice input
        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id, "⚠️ Couldn't understand the audio.")
                return "ok", 200

        # Process commands and inputs
        if text:
            if text.lower() == "reset" and not sess["awaiting_reset_confirmation"]:
                sess["awaiting_reset_confirmation"] = True
                send_telegram_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' to confirm.")
            elif sess["awaiting_reset_confirmation"] and text.lower() == "yes":
                sess["structured_data"] = blank_report()
                sess["awaiting_reset_confirmation"] = False
                send_telegram_message(chat_id, "✅ Report reset successfully.")
            else:
                sess["awaiting_reset_confirmation"] = False
                structured_data = extract_single_command(text)
                if structured_data:
                    sess["structured_data"] = merge_structured_data(sess["structured_data"], structured_data)
                    sess["structured_data"] = enrich_with_date(sess["structured_data"])
                    summary = summarize_data(sess["structured_data"])
                    send_telegram_message(chat_id, f"✅ Data updated:\n{summary}")
                else:
                    send_telegram_message(chat_id, "⚠️ Could not process input. Please try again.")

        sess["last_interaction"] = time()
        save_session_data(session_data)
        return "ok", 200
    except Exception as e:
        logger.error({"event": "webhook_error", "error": str(e)})
        send_telegram_message(chat_id, "⚠️ An error occurred. Please try again later.")
        return "error", 500

@app.get("/")
def health():
    """Health check endpoint."""
    return "OK", 200

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 10000)), debug=False)
