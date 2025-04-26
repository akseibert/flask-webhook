from flask import Flask, request
import requests
import os
import json
import re
import logging
from datetime import datetime
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from difflib import SequenceMatcher
from time import time
from collections import deque
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
import io

# --- Initialize logging ---
logging.basicConfig(
    filename="/opt/render/project/src/app.log",
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())

# --- Validate environment variables ---
required_env_vars = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"]
optional_env_vars = ["SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET", "SHAREPOINT_TENANT_ID", "SHAREPOINT_SITE_ID", "SHAREPOINT_LIST_ID"]
for var in required_env_vars:
    if not os.getenv(var):
        logger.error(f"Missing required environment variable: {var}")
        raise ValueError(f"Missing {var}")
for var in optional_env_vars:
    if not os.getenv(var):
        logger.warning(f"Optional environment variable {var} not set; SharePoint integration disabled until configured")

# --- Initialize OpenAI client ---
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    logger.info("OpenAI client initialized")
except Exception as e:
    logger.error(f"OpenAI init failed: {e}")
    raise

app = Flask(__name__)

# --- Session data persistence ---
SESSION_FILE = "/opt/render/project/src/session_data.json"
PAUSE_THRESHOLD = 300  # 5 minutes in seconds
MAX_HISTORY = 10  # Max commands to store for undo

def load_session_data():
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE) as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data):
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed to save session data: {e}")

session_data = load_session_data()

def blank_report():
    today = datetime.now().strftime("%d-%m-%Y")
    return {
        "site_name": "", "segment": "", "category": "",
        "company": [], "people": [], "roles": [], "tools": [], "service": [],
        "activities": [], "issues": [],
        "time": "", "weather": "", "impression": "",
        "comments": "", "date": today
    }

# --- Centralized regex patterns ---
FIELD_PATTERNS = {
    "site_name": r'^(?:site\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)?([^,]+?)(?=(?:\s*,\s*(?:segment|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "segment": r'^(?:segment\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "category": r'^(?:category\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "impression": r'^(?:impression\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|weather|comments)\s*:)|$)',
    "people": r'^(?:add\s+|people\s+|person\s+|add\s+people\s+|people\s+add\s+|person\s+add\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "role": r'^(?:add\s+|people\s+|person\s+)?(\w+\s*\w*)\s*[:,]?\s*as\s+([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)|^(?:person|people)\s*[:,]?\s*(\w+\s*\w*)\s*,\s*role\s*[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "supervisor": r'^(?:i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised)(?:\s+.*)?$',
    "company": r'^(?:add\s+company\s+|company\s+|companies\s+|add\s+([^,]+?)\s+as\s+company\s*)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "service": r'^(?:add\s+service\s+|service\s+|services\s+)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "tool": r'^(?:add\s+tool\s+|tool\s+|tools\s+)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "activity": r'^(?:add\s+activity\s+|activity\s+|activities\s+)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|issue|time|weather|impression|comments)\s*:)|$)',
    "issue": r'^(?:add\s+issue\s+|issue\s+|issues\s+)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|time|weather|impression|comments)\s*:)|$)',
    "weather": r'^(?:weather\s*[:,]?\s*|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|impression|comments)\s*:)|$)',
    "time": r'^(?:time\s*[:,]?\s*|morning\s*time\s*|afternoon\s*time\s*|evening\s*time\s*)(morning|afternoon|evening)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|weather|impression|comments)\s*:)|$)',
    "clear": r'^(issues|activities|comments)\s*[:,]?\s*none$'
}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    response.raise_for_status()
    logger.info({"event": "send_telegram_message", "chat_id": chat_id, "text": text[:50]})
    return response

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    response = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    response.raise_for_status()
    file_path = response.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        logger.info({"event": "fetch_audio", "url": audio_url})
        audio_response = requests.get(audio_url)
        audio_response.raise_for_status()
        audio = audio_response.content
        logger.info({"event": "audio_fetched", "size_bytes": len(audio)})
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio, "audio/ogg")
        )
        text = response.text.strip()
        if not text:
            logger.warning({"event": "transcription_empty", "result": text})
            return ""
        logger.info({"event": "transcription_success", "text": text})
        return text
    except Exception as e:
        logger.error({"event": "transcription_failed", "error": str(e)})
        return ""

def save_to_sharepoint(chat_id, report_data):
    """
    Save report data to a SharePoint list. To be implemented after testing.
    Requires SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET, SHAREPOINT_TENANT_ID,
    SHAREPOINT_SITE_ID, and SHAREPOINT_LIST_ID environment variables.
    """
    logger.info({"event": "save_to_sharepoint", "chat_id": chat_id, "status": "placeholder"})
    try:
        # TODO: Implement Microsoft Graph API or SharePoint REST API call
        # 1. Authenticate using client credentials
        # 2. Create/update list item with mapped fields
        # 3. Handle errors and retries
        # Example mapping:
        # - site_name -> Title (string)
        # - segment -> Segment (string)
        # - category -> Category (string)
        # - company -> Companies (multi-line text, JSON or comma-separated)
        # - people -> People (multi-line text, JSON or comma-separated)
        # - roles -> Roles (multi-line text, JSON)
        # - service -> Services (multi-line text, JSON)
        # - tools -> Tools (multi-line text, JSON)
        # - activities -> Activities (multi-line text, comma-separated)
        # - issues -> Issues (multi-line text, JSON)
        # - time -> Time (string)
        # - weather -> Weather (string)
        # - impression -> Impression (string)
        # - comments -> Comments (string)
        # - date -> ReportDate (date)
        logger.warning({"event": "save_to_sharepoint", "status": "not_implemented"})
        return False
    except Exception as e:
        logger.error({"event": "sharepoint_error", "error": str(e)})
        return False

def generate_pdf_report(report_data):
    """
    Generate a PDF report from the provided report data.
    Returns a BytesIO buffer containing the PDF.
    """
    logger.info({"event": "generate_pdf_report", "status": "placeholder"})
    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []

        # Add report title
        story.append(Paragraph("Construction Site Report", styles['Title']))
        story.append(Spacer(1, 12))

        # Add report fields
        fields = [
            ("Site", report_data.get("site_name", "")),
            ("Segment", report_data.get("segment", "")),
            ("Category", report_data.get("category", "")),
            ("Companies", ", ".join(c.get("name", "") for c in report_data.get("company", []))),
            ("People", ", ".join(report_data.get("people", []))),
            ("Roles", ", ".join(f"{r.get('name', '')} ({r.get('role', '')})" for r in report_data.get("roles", []))),
            ("Services", ", ".join(s.get("task", "") for s in report_data.get("service", []))),
            ("Tools", ", ".join(t.get("item", "") for t in report_data.get("tools", []))),
            ("Activities", ", ".join(report_data.get("activities", []))),
            ("Issues", "; ".join(i.get("description", "") + (f" (by {i.get('caused_by', '')})" if i.get("caused_by") else "") for i in report_data.get("issues", []))),
            ("Time", report_data.get("time", "")),
            ("Weather", report_data.get("weather", "")),
            ("Impression", report_data.get("impression", "")),
            ("Comments", report_data.get("comments", "")),
            ("Date", report_data.get("date", ""))
        ]

        for label, value in fields:
            if value:
                story.append(Paragraph(f"<b>{label}:</b> {value}", styles['Normal']))
                story.append(Spacer(1, 6))

        doc.build(story)
        buffer.seek(0)
        logger.info({"event": "pdf_generated", "size_bytes": buffer.getbuffer().nbytes})
        return buffer
    except Exception as e:
        logger.error({"event": "pdf_generation_error", "error": str(e)})
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_pdf_to_user(chat_id, pdf_buffer):
    """
    Send the generated PDF to the user via Telegram.
    """
    logger.info({"event": "send_pdf_to_user", "chat_id": chat_id, "status": "placeholder"})
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        files = {'document': ('report.pdf', pdf_buffer, 'application/pdf')}
        data = {'chat_id': chat_id, 'caption': 'Here is your construction site report.'}
        response = requests.post(url, files=files, data=data)
        response.raise_for_status()
        logger.info({"event": "pdf_sent", "chat_id": chat_id})
        return True
    except Exception as e:
        logger.error({"event": "pdf_send_error", "error": str(e)})
        return False

def enrich_with_date(d):
    today = datetime.now().strftime("%d-%m-%Y")
    if not d.get("date"):
        d["date"] = today
    else:
        try:
            input_date = datetime.strptime(d["date"], "%d-%m-%Y")
            if input_date > datetime.now():
                d["date"] = today
        except ValueError:
            d["date"] = today
    return d

def summarize_data(d):
    logger.info({"event": "summarize_data", "data": json.dumps(d, indent=2)})
    lines = []
    lines.append(f"🏗️ **Site**: {d.get('site_name', '') or ''}")
    lines.append(f"🛠️ **Segment**: {d.get('segment', '') or ''}")
    lines.append(f"📋 **Category**: {d.get('category', '') or ''}")
    lines.append(
        "🏢 **Companies**: " +
        ", ".join(c.get("name", "") if isinstance(c, dict) else str(c)
                  for c in d.get("company", [])) or ""
    )
    lines.append(
        "👷 **People**: " +
        ", ".join(p for p in d.get("people", []) if p) or ""
    )
    lines.append(
        "🎭 **Roles**: " +
        ", ".join(
            f"{r.get('name', '')} ({r.get('role', '')})" if isinstance(r, dict) and r.get('role') else r.get('name', '')
            for r in d.get("roles", [])
        ) or ""
    )
    lines.append(
        "🔧 **Services**: " +
        ", ".join(
            f"{s.get('task', '')}" if isinstance(s, dict) and s.get('task') else str(s)
            for s in d.get("service", []) if s.get('task')
        ) or ""
    )
    lines.append(
        "🛠️ **Tools**: " +
        ", ".join(
            f"{t.get('item', '')}" if isinstance(t, dict) and t.get('item') else str(t)
            for t in d.get("tools", []) if t.get('item')
        ) or ""
    )
    lines.append("📅 **Activities**: " + ", ".join(d.get("activities", [])) or "")
    lines.append("⚠️ **Issues**:")
    valid_issues = [
        i for i in d.get("issues", [])
        if isinstance(i, dict) and i.get("description", "").strip()
    ]
    if valid_issues:
        for i in valid_issues:
            desc = i["description"]
            by = i.get("caused_by", "")
            photo = " 📸" if i.get("has_photo") else ""
            extra = f" (by {by})" if by else ""
            lines.append(f"  • {desc}{extra}{photo}")
    else:
        lines.append("")
    lines.append(f"⏰ **Time**: {d.get('time', '') or ''}")
    lines.append(f"🌦️ **Weather**: {d.get('weather', '') or ''}")
    lines.append(f"😊 **Impression**: {d.get('impression', '') or ''}")
    lines.append(f"💬 **Comments**: {d.get('comments', '') or ''}")
    lines.append(f"📆 **Date**: {d.get('date', '') or ''}")
    summary = "\n".join(line for line in lines if line.strip())
    logger.info({"event": "summary_generated", "summary": summary})
    return summary

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    logger.info({"event": "extract_site_report", "input_text": text})
    result = {}

    # Handle multi-field inputs
    commands = [cmd.strip() for cmd in re.split(r',\s*(?=(?:[^:]*:)?[^:]*$)', text) if cmd.strip()]
    if len(commands) > 1:
        seen_fields = set()
        for cmd in commands:
            cmd_result = extract_single_command(cmd)
            for key, value in cmd_result.items():
                if key in seen_fields and key not in ["people", "company", "roles", "tools", "service", "activities", "issues"]:
                    continue
