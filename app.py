from flask import Flask, request
import requests
import os
import json
import re
import logging
import signal
import sys
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
try:
    logging.basicConfig(
        filename="/opt/render/project/src/app.log",
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    logger.addHandler(logging.StreamHandler())
    logger.info({"event": "logging_initialized"})
except Exception as e:
    print(f"Failed to initialize logging: {e}")
    raise

app = Flask(__name__)

# --- Handle shutdown signals ---
def handle_shutdown(signum, frame):
    logger.info({"event": "shutdown_signal", "signal": signum})
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# --- Validate environment variables ---
required_env_vars = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"]
optional_env_vars = ["SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET", "SHAREPOINT_TENANT_ID", "SHAREPOINT_SITE_ID", "SHAREPOINT_LIST_ID"]
try:
    for var in required_env_vars:
        if not os.getenv(var):
            logger.error(f"Missing required environment variable: {var}")
            raise ValueError(f"Missing {var}")
    for var in optional_env_vars:
        if not os.getenv(var):
            logger.warning(f"Optional environment variable {var} not set; SharePoint integration disabled")
    logger.info({"event": "env_vars_validated"})
except Exception as e:
    logger.error(f"Environment variable validation failed: {e}")
    raise

# --- Initialize OpenAI client ---
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    logger.info({"event": "openai_client_initialized"})
except Exception as e:
    logger.error(f"OpenAI initialization failed: {e}")
    raise

# --- GPT Prompt for complex input parsing ---
gpt_prompt = """
You are an AI assistant extracting a construction site report from user input. Extract all explicitly mentioned fields and return them in JSON format. Process the entire input as a single unit, splitting on commas or periods only when fields are clearly separated by keywords. Map natural language phrases and standardized commands (add, delete, correct) to fields accurately, prioritizing specific fields over comments or site_name. Do not treat reset commands ("new", "new report", "reset", "reset report", "/new") as comments or fields; return {} for these. Handle "none" inputs (e.g., "Tools: none") as clearing the respective field, and vague inputs (e.g., "Activities: many") by adding them and noting clarification needed.

Fields to extract (omit if not present):
- site_name: string (e.g., "Downtown Project")
- segment: string (e.g., "5")
- category: string (e.g., "Bestand")
- company: list of objects with "name" (e.g., [{"name": "Acme Corp"}])
- people: list of strings (e.g., ["Anna", "Tobias"])
- roles: list of objects with "name" and "role" (e.g., [{"name": "Anna", "role": "Supervisor"}])
- tools: list of objects with "item" (e.g., [{"item": "Crane"}])
- service: list of objects with "task" (e.g., [{"task": "Excavation"}])
- activities: list of strings (e.g., ["Concrete pouring"])
- issues: list of objects with "description" (required), "caused_by" (optional), "has_photo" (optional, default false)
- time: string (e.g., "morning", "full day")
- weather: string (e.g., "cloudy")
- impression: string (e.g., "productive")
- comments: string (e.g., "Ensure safety protocols")
- date: string (format dd-mm-yyyy)

Commands:
- add <category> <value>: Add a value to the category (e.g., "add site Downtown Project" -> "site_name": "Downtown Project").
- delete <category> [value]: Remove a value or clear the category (e.g., "delete activities Laying foundation").
- correct <category> <old> to <new>: Update a value (e.g., "correct site Downtown to Uptown").
- <category>: <value>: Add a value (e.g., "Services: abc" -> "service": [{"task": "abc"}]).
- <category>: none: Clear the category (e.g., "Tools: none" -> "tools": []).

Rules:
- Extract fields from colon-separated inputs (e.g., "Services: abc"), natural language (e.g., "weather was cloudy" -> "weather": "cloudy"), or commands (e.g., "add people Anna").
- For segment and category:
  - Extract only the value (e.g., "Segment: 5" -> "segment": "5").
- For issues:
  - Recognize keywords: "Issue", "Issues", "Problem", "Delay", "Injury".
  - "Issues: none" clears the issues list.
- For activities:
  - Recognize keywords: "Activity", "Activities", "Task", "Progress", "Construction", or action-oriented phrases.
  - "Activities: none" clears the activities list.
  - Handle vague inputs like "Activities: many" by adding them and noting clarification needed.
- For site_name:
  - Recognize location-like phrases following "at", "in", "on" (e.g., "Work was done at East Wing" -> "site_name": "East Wing", "activities": ["Work was done"]).
- For people and roles:
  - Recognize "add [name] as [role]" (e.g., "add Anna as engineer" -> "people": ["Anna"], "roles": [{"name": "Anna", "role": "Engineer"}]).
  - "Roles supervisor" assigns "Supervisor" to the user.
  - Do not assign "Supervisor" unless explicitly stated.
- For tools and service:
  - Recognize "Tool: [item]", "Service: [task]", or commands like "add service abc".
- Comments should only include non-field-specific notes.
- Return {} for reset commands or irrelevant inputs.
- Case-insensitive matching.

Examples:
1. Input: "add site Central Plaza, add segment 5, add issue Power outage"
   Output: {"site_name": "Central Plaza", "segment": "5", "issues": [{"description": "Power outage"}]}
2. Input: "new report"
   Output: {}
3. Input: "Services: abc"
   Output: {"service": [{"task": "abc"}]}
4. Input: "Tools: none"
   Output: {"tools": []}
5. Input: "Roles supervisor"
   Output: {"people": ["User"], "roles": [{"name": "User", "role": "Supervisor"}]}
6. Input: "Work was done at the East Wing."
   Output: {"site_name": "East Wing", "activities": ["Work was done"]}
7. Input: "add Anna as engineer to people"
   Output: {"people": ["Anna"], "roles": [{"name": "Anna", "role": "Engineer"}]}
8. Input: "Activities: many"
   Output: {"activities": ["many"]}
"""

# --- Session data persistence ---
SESSION_FILE = "/opt/render/project/src/session_data.json"
PAUSE_THRESHOLD = 300  # 5 minutes in seconds
MAX_HISTORY = 10  # Max commands to store for undo

def load_session_data():
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE) as f:
                data = json.load(f)
                for chat_id in data:
                    if "command_history" in data[chat_id]:
                        data[chat_id]["command_history"] = deque(data[chat_id]["command_history"], maxlen=MAX_HISTORY)
                logger.info({"event": "session_data_loaded"})
                return data
        logger.info({"event": "session_data_not_found", "file": SESSION_FILE})
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data):
    try:
        serializable_data = {}
        for chat_id, session in data.items():
            serializable_session = session.copy()
            if "command_history" in serializable_session:
                serializable_session["command_history"] = list(serializable_session["command_history"])
            serializable_data[chat_id] = serializable_session
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(serializable_data, f)
        logger.info({"event": "session_data_saved", "file": SESSION_FILE})
    except Exception as e:
        logger.error(f"Failed to save session data: {e}")

try:
    session_data = load_session_data()
    logger.info({"event": "session_data_initialized"})
except Exception as e:
    logger.error(f"Session data initialization failed: {e}")
    raise

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
    "site_name": r'^(?:add\s+)?(?:site\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)(.+?)(?=(?:\s*,\s*(?:segment|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "segment": r'^(?:add\s+)?(?:segment\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "category": r'^(?:add\s+)?(?:category\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|segment|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "impression": r'^(?:add\s+)?(?:impression\s*[:,]?\s*)(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|weather|comments)\s*:)|$)',
    "people": r'^(?:add\s+)?(?:people\s+|person\s+|people\s*[:,]?\s*|person\s*[:,]?\s*)(.+?)(?:\s+as\s+[^,\s]+)?(?=(?:\s*,\s*(?:site|segment|category|company|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "role": r'^(?:add\s+)?(?:people\s+|person\s+)?(.+?)\s*[:,]?\s*as\s+([^,\s]+)(?:\s+to\s+people)?(?=(?:\s*,\s*(?:site|segment|category|company|people|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)|^(?:add\s+)?(?:person|people)\s*[:,]?\s*(.+?)\s*,\s*role\s*[:,]?\s*([^,\s]+)(?=(?:\s*,\s*(?:site|segment|category|company|people|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "supervisor": r'^(?:add\s+)?(?:supervisors\s*(?:were|are)\s+|i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised|roles?\s*[:,]?\s*supervisor\s*$)(.+?)?(?=(?:\s*,\s*(?:site|segment|category|company|people|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "company": r'^(?:add\s+)?(?:company\s*[:,]?\s*|companies\s*[:,]?\s*)(.+?)(?=(?:\s*,\s*(?:site|segment|category|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "service": r'^(?:add\s+)?(?:service\s*[:,]?\s*|services\s*[:,]?\s*|services\s*(?:were|provided)\s+)(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "tool": r'^(?:add\s+)?(?:tool\s*[:,]?\s*|tools\s*[:,]?\s*|tools\s*used\s*(?:included|were)\s+)(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "activity": r'^(?:add\s+)?(?:activity\s*[:,]?\s*|activities\s*[:,]?\s*|activities\s*(?:covered|included)\s+)(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|issue|time|weather|impression|comments)\s*:)|$)',
    "issue": r'^(?:add\s+)?(?:issue\s*[:,]?\s*|issues\s*[:,]?\s*|issues\s*(?:encountered|included)\s+)(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|time|weather|impression|comments)\s*:)|$)',
    "weather": r'^(?:add\s+)?(?:weather\s*[:,]?\s*|weather\s+was\s+|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
    "time": r'^(?:add\s+)?(?:time\s*[:,]?\s*|time\s+spent\s+|morning\s*time\s*|afternoon\s*time\s*|evening\s*time\s*)(morning|afternoon|evening|full day)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|weather|impression|comments)\s*:)|$)',
    "comments": r'^(?:add\s+)?(?:comment\s*[:,]?\s*|comments\s*[:,]?\s*)(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|weather|impression)\s*:)|$)',
    "clear": r'^(issues|activities|comments|tools|service|company|people|roles)\s*[:,]?\s*none$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": r'^(?:delete|remove)\s+(site|segment|category|company|person|people|role|roles|tool|service|activity|activities|issue|issues|time|weather|impression|comments)\s+(.+?)$',
    "correct": r'^(?:correct\s+|update\s+)(site|segment|category|company|person|people|role|roles|tool|service|activity|activities|issue|issues|time|weather|impression|comments)\s+(.+?)\s+to\s+(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)'
}

# Validate regex patterns
try:
    for field, pattern in FIELD_PATTERNS.items():
        re.compile(pattern, re.IGNORECASE)
    logger.info({"event": "regex_patterns_validated"})
except Exception as e:
    logger.error(f"Regex pattern validation failed for field {field}: {e}")
    raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id, text):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        response.raise_for_status()
        logger.info({"event": "send_telegram_message", "chat_id": chat_id, "text": text[:50]})
        return response
    except Exception as e:
        logger.error({"event": "send_telegram_message_error", "chat_id": chat_id, "error": str(e)})
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        response = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
        response.raise_for_status()
        file_path = response.json()["result"]["file_path"]
        logger.info({"event": "get_telegram_file_path", "file_id": file_id})
        return f"https://api.telegram.org/file/bot{token}/{file_path}"
    except Exception as e:
        logger.error({"event": "get_telegram_file_path_error", "file_id": file_id, "error": str(e)})
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
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
        # Clean transcribed text from common voice command artifacts
        text = re.sub(r'^\s*(s|add|delete|remove)\s+', '', text, flags=re.IGNORECASE).strip()
        logger.info({"event": "transcription_success", "text": text})
        return text
    except Exception as e:
        logger.error({"event": "transcription_failed", "error": str(e)})
        return ""

def save_to_sharepoint(chat_id, report_data):
    logger.info({"event": "save_to_sharepoint", "chat_id": chat_id, "status": "placeholder"})
    try:
        logger.warning({"event": "save_to_sharepoint", "status": "not_implemented"})
        return False
    except Exception as e:
        logger.error({"event": "sharepoint_error", "error": str(e)})
        return False

def generate_pdf_report(report_data):
    logger.info({"event": "generate_pdf_report", "status": "placeholder"})
    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []

        story.append(Paragraph("Construction Site Report", styles['Title']))
        story.append(Spacer(1, 12))

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
    try:
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
    except Exception as e:
        logger.error({"event": "enrich_with_date_error", "error": str(e)})
        raise

def summarize_data(d):
    try:
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
    except Exception as e:
        logger.error({"event": "summarize_data_error", "error": str(e)})
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    try:
        logger.info({"event": "extract_site_report", "input_text": text})
        result = {}

        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())

        reset_match = re.match(FIELD_PATTERNS["reset"], normalized_text, re.IGNORECASE)
        if reset_match:
            logger.info({"event": "reset_command_detected", "input": normalized_text})
            return {"reset": True}

        commands = [cmd.strip() for cmd in re.split(r',\s*(?=(?:[^:]*:)[^,]*$)|(?<!\w)\.\s*(?=[A-Z])', text) if cmd.strip()]
        if len(commands) > 1:
            seen_fields = set()
            for cmd in commands:
                cmd_result = extract_single_command(cmd)
                if cmd_result.get("reset"):
                    return {"reset": True}
                for key, value in cmd_result.items():
                    if key in seen_fields and key not in ["people", "company", "roles", "tools", "service", "activities", "issues"]:
                        continue
                    seen_fields.add(key)
                    if key in ["people", "company", "roles", "tools", "service", "activities", "issues"]:
                        result.setdefault(key, []).extend(value)
                    else:
                        result[key] = value
            logger.info({"event": "multi_field_extracted", "result": result})
            return result

        return extract_single_command(text)
    except Exception as e:
        logger.error({"event": "extract_site_report_error", "input": text, "error": str(e)})
        raise

def extract_single_command(text):
    try:
        result = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())

        # Clean voice command artifacts
        cleaned_text = re.sub(r'^\s*(s|add|delete|remove)\s+', '', normalized_text, flags=re.IGNORECASE).strip()

        reset_match = re.match(FIELD_PATTERNS["reset"], cleaned_text, re.IGNORECASE)
        if reset_match:
            logger.info({"event": "reset_command", "input": cleaned_text})
            return {"reset": True}

        if cleaned_text.lower() in ("undo", "/undo"):
            logger.info({"event": "undo_command"})
            return {"undo": True}

        if cleaned_text.lower() in ("status", "/status"):
            logger.info({"event": "status_command"})
            return {"status": True}

        if cleaned_text.lower() in ("export pdf", "/export pdf"):
            logger.info({"event": "export_pdf_command"})
            return {"export_pdf": True}

        # Handle deletion commands first
        delete_match = re.match(FIELD_PATTERNS["delete"], cleaned_text, re.IGNORECASE)
        if delete_match:
            field = delete_match.group(1).lower()
            value = delete_match.group(2).strip()
            if field in ["person", "people"]:
                field = "people"
                result[field] = {"delete": value}
            elif field in ["role", "roles"]:
                field = "roles"
                result[field] = {"delete": value}
            elif field in ["activity", "activities"]:
                field = "activities"
                result[field] = {"delete": value}
            elif field in ["issue", "issues"]:
                field = "issues"
                result[field] = [{"description": value, "delete": True}]
            elif field == "company":
                result[field] = [{"name": value, "delete": True}]
            elif field == "tool":
                result[field] = [{"item": value, "delete": True}]
            elif field == "service":
                result[field] = [{"task": value, "delete": True}]
            elif field in ["site_name", "segment", "category", "time", "weather", "impression", "comments"]:
                result[field] = {"delete": value}
            logger.info({"event": "delete_command", "field": field, "value": value})
            return result

        correct_match = re.match(FIELD_PATTERNS["correct"], cleaned_text, re.IGNORECASE)
        if correct_match:
            field = correct_match.group(1).lower()
            old_value = correct_match.group(2).strip()
            new_value = correct_match.group(3).strip()
            if field in ["site_name", "segment", "category", "time", "weather", "impression", "comments"]:
                result[field] = new_value
            elif field == "company":
                result[field] = [{"name": new_value}]
            elif field == "people":
                result[field] = [new_value]
            elif field == "roles":
                result[field] = [{"name": new_value, "role": old_value}]
            elif field == "tools":
                result[field] = [{"item": new_value}]
            elif field == "service":
                result[field] = [{"task": new_value}]
            elif field == "activities":
                result[field] = [new_value]
            elif field == "issues":
                result[field] = [{"description": new_value}]
            logger.info({"event": "corrected_field", "field": field, "old": old_value, "new": new_value})
            return result

        for field, pattern in FIELD_PATTERNS.items():
            if field in ["reset", "delete", "correct"]:
                continue
            match = re.match(pattern, cleaned_text, re.IGNORECASE)
            if match:
                if field == "site_name" and re.search(r'\b(add|delete|remove|correct|update|none|as|role|new|reset)\b', cleaned_text.lower()):
                    continue
                if field == "people":
                    names = [name.strip() for name in match.group(1).split("and") if name.strip()]
                    role_match = re.search(r'\s+as\s+([^,\s]+)', cleaned_text, re.IGNORECASE)
                    if role_match:
                        role = role_match.group(1).title()
                        result["people"] = names
                        result["roles"] = [{"name": name, "role": role} for name in names]
                    else:
                        result["people"] = names
                    logger.info({"event": "extracted_field", "field": "people", "value": names})
                elif field == "role":
                    name = (match.group(1) or match.group(3)).strip()
                    role = (match.group(2) or match.group(4)).title()
                    names = [n.strip() for n in name.split("and") if n.strip()]
                    result["people"] = names
                    result["roles"] = [{"name": n, "role": role} for n in names]
                    logger.info({"event": "extracted_field", "field": "roles", "names": names, "role": role})
                elif field == "supervisor":
                    if match.group(1):
                        names = [name.strip() for name in match.group(1).split("and") if name.strip()]
                        result["people"] = names
                        result["roles"] = [{"name": name, "role": "Supervisor"} for name in names]
                    else:
                        result["people"] = ["User"]
                        result["roles"] = [{"name": "User", "role": "Supervisor"}]
                    logger.info({"event": "extracted_field", "field": "roles", "value": match.group(1) or "User"})
                elif field == "company":
                    name = match.group(1).strip()
                    result["company"] = [{"name": name}]
                    logger.info({"event": "extracted_field", "field": "company", "value": name})
                elif field == "clear":
                    field_name = match.group(1).lower()
                    result[field_name] = []
                    logger.info({"event": "extracted_field", "field": field_name, "value": "none"})
                elif field == "service":
                    value = match.group(1).strip()
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        services = [s.strip() for s in value.split("and") if s.strip()]
                        result[field] = [{"task": s} for s in services]
                    logger.info({"event": "extracted_field", "field": "service", "value": value})
                elif field == "tool":
                    value = match.group(1).strip()
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        # Handle misspelling with fuzzy matching
                        tools = [t.strip() for t in value.split("and") if t.strip()]
                        corrected_tools = []
                        known_tools = ["excavator", "scaffold", "crane", "drill"]  # Expand as needed
                        for tool in tools:
                            best_match = max(known_tools, key=lambda x: SequenceMatcher(None, tool.lower(), x).ratio(), default=tool)
                            similarity = SequenceMatcher(None, tool.lower(), best_match).ratio()
                            corrected_tools.append(best_match if similarity > 0.8 else tool)
                        result[field] = [{"item": t} for t in corrected_tools]
                    logger.info({"event": "extracted_field", "field": "tools", "value": value})
                elif field == "issue":
                    value = match.group(1).strip()
                    result["issues"] = [{"description": value}]
                    logger.info({"event": "extracted_field", "field": "issues", "value": value})
                elif field == "activity":
                    value = match.group(1).strip()
                    result["activities"] = [value]
                    logger.info({"event": "extracted_field", "field": "activities", "value": value})
                else:
                    value = match.group(1).strip()
                    result[field] = value
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                return result

        # Fallback to GPT for complex inputs
        messages = [
            {"role": "system", "content": "Extract explicitly stated fields from construction site report input. Handle multi-field inputs by processing the entire input as a single unit. Return JSON with extracted fields."},
            {"role": "user", "content": gpt_prompt + "\nInput text: " + cleaned_text}
        ]
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo", messages=messages, temperature=0.2
            )
            raw_response = response.choices[0].message.content
            logger.info({"event": "gpt_response", "raw_response": raw_response})
            data = json.loads(raw_response)
            logger.info({"event": "gpt_extracted", "data": data})
            return data
        except Exception as e:
            logger.error({"event": "gpt_extract_error", "input": cleaned_text, "error": str(e)})
            return {"comments": cleaned_text} if cleaned_text.strip() else {}
    except Exception as e:
        logger.error({"event": "extract_single_command_error", "input": text, "error": str(e)})
        raise

def merge_structured_data(existing, new):
    try:
        merged = existing.copy()
        for key, value in new.items():
            if key in ["reset", "undo", "status", "export_pdf"]:
                continue
            if key in ["company", "roles", "tools", "service", "activities", "issues"]:
                if value == []:
                    merged[key] = []
                    logger.info({"event": "cleared_list", "field": key})
                    continue
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else []
                for new_item in new_items:
                    if not isinstance(new_item, dict):
                        if key == "activities":
                            if new_item not in existing_list:
                                existing_list.append(new_item)
                        continue
                    if "delete" in new_item:
                        target = new_item.get("name") or new_item.get("item") or new_item.get("task") or new_item.get("description")
                        existing_list = [item for item in existing_list if target.lower() not in str(item.get("name" if key == "company" else "item" if key == "tools" else "task" if key == "service" else "description", "")).lower()]
                    else:
                        if key == "company" and any(e.get("name", "").lower() == new_item.get("name", "").lower() for e in existing_list):
                            continue
                        elif key == "tools" and any(e.get("item", "").lower() == new_item.get("item", "").lower() for e in existing_list):
                            continue
                        elif key == "service" and any(e.get("task", "").lower() == new_item.get("task", "").lower() for e in existing_list):
                            continue
                        elif key == "issues" and any(e.get("description", "").lower() == new_item.get("description", "").lower() for e in existing_list):
                            continue
                        elif key == "roles" and any(e.get("name", "").lower() == new_item.get("name", "").lower() for e in existing_list):
                            for i, e in enumerate(existing_list):
                                if e.get("name", "").lower() == new_item.get("name", "").lower():
                                    existing_list[i] = new_item
                                    break
                            else:
                                existing_list.append(new_item)
                        else:
                            existing_list.append(new_item)
                merged[key] = existing_list
            elif key == "people":
                existing_list = merged.get(key, [])
                if isinstance(value, dict) and "delete" in value:
                    target = value["delete"]
                    existing_list = [item for item in existing_list if target.lower() not in item.lower()]
                    merged["roles"] = [r for r in merged.get("roles", []) if target.lower() not in r.get("name", "").lower()]
                else:
                    new_items = value if isinstance(value, list) else []
                    for item in new_items:
                        if item and item not in existing_list:
                            existing_list.append(item)
                merged[key] = existing_list
            else:
                if isinstance(value, dict) and "delete" in value:
                    merged[key] = ""
                elif value:
                    merged[key] = value
        logger.info({"event": "merged_data", "data": json.dumps(merged, indent=2)})
        return merged
    except Exception as e:
        logger.error({"event": "merge_structured_data_error", "error": str(e)})
        raise

def delete_entry(data, field, value=None):
    try:
        logger.info({"event": "delete_entry", "field": field, "value": value})
        if field in ["company", "tools", "service", "issues", "activities"]:
            if value:
                data[field] = [item for item in data[field] if value.lower() not in str(item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description" if field == "issues" else item, "")).lower()]
            else:
                data[field] = []
        elif field == "people":
            if value:
                data[field] = [item for item in data[field] if value.lower() not in item.lower()]
                data["roles"] = [role for role in data.get("roles", []) if value.lower() not in role.get("name", "").lower()]
            else:
                data[field] = []
                data["roles"] = []
        elif field in ["roles"]:
            if value:
                data[field] = [role for role in data[field] if value.lower() not in role.get("name", "").lower()]
            else:
                data[field] = []
        elif field in ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]:
            data[field] = ""
        logger.info({"event": "data_after_deletion", "data": json.dumps(data, indent=2)})
        return data
    except Exception as e:
        logger.error({"event": "delete_entry_error", "field": field, "error": str(e)})
        raise

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        logger.info({"event": "webhook_hit"})
        data = request.get_json(force=True)
        if "message" not in data:
            logger.info({"event": "no_message"})
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        logger.info({"event": "received_message", "chat_id": chat_id, "text": text})

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

        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id,
                    "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'add site Downtown Project' or 'add people Tobias').")
                return "ok", 200
            logger.info({"event": "transcribed_voice", "text": text})

        current_time = time()
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip()) if text else ""

        if not normalized_text:
            send_telegram_message(chat_id,
                "⚠️ Empty input received. Please provide a valid command (e.g., 'add site Downtown Project' or 'add issue Power outage').")
            return "ok", 200

        # Handle reset confirmation
        if sess.get("awaiting_reset_confirmation", False):
            normalized_text_lower = normalized_text.lower()
            if normalized_text_lower in ("yes", "new", "new report"):
                sess["structured_data"] = blank_report()
                sess["awaiting_correction"] = False
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["command_history"].clear()
                save_session_data(session_data)
                tpl = summarize_data(sess["structured_data"])
                send_telegram_message(chat_id,
                    "**Starting a fresh report**\n\n" + tpl +
                    "\n\nSpeak or type your first field (e.g., 'add site Downtown Project').")
                return "ok", 200
            elif normalized_text_lower in ("no", "existing", "continue"):
                if sess["pending_input"].lower() in ("new", "new report", "reset", "reset report", "/new"):
                    sess["awaiting_reset_confirmation"] = False
                    sess["pending_input"] = None
                    send_telegram_message(chat_id,
                        "Report not reset. Please provide your next input.")
                    return "ok", 200
                else:
                    text = sess["pending_input"]
                    sess["awaiting_reset_confirmation"] = False
                    sess["pending_input"] = None
                    sess["last_interaction"] = current_time
            else:
                send_telegram_message(chat_id,
                    "Please clarify: Reset the report? Reply 'yes' or 'no'.")
                return "ok", 200

        # Check for reset based on pause
        normalized_text_lower = normalized_text.lower()
        if (current_time - sess.get("last_interaction", 0) > PAUSE_THRESHOLD and
                normalized_text_lower not in ("yes", "no", "new", "new report", "reset", "reset report", "/new", "existing", "continue")):
            sess["pending_input"] = text
            sess["awaiting_reset_confirmation"] = True
            sess["last_interaction"] = current_time
            save_session_data(session_data)
            send_telegram_message(chat_id,
                "It’s been a while! Reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        sess["last_interaction"] = current_time

        # Handle explicit reset commands
        if normalized_text_lower in ("new", "new report", "reset", "reset report", "/new"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            send_telegram_message(chat_id,
                "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        # Handle undo command
        if normalized_text_lower in ("undo", "/undo"):
            if sess["command_history"]:
                prev_state = sess["command_history"].pop()
                sess["structured_data"] = prev_state
                save_session_data(session_data)
                tpl = summarize_data(sess["structured_data"])
                send_telegram_message(chat_id,
                    "Undone last action. Here’s the updated report:\n\n" + tpl +
                    "\n\nAnything else to add or correct?")
            else:
                send_telegram_message(chat_id,
                    "No actions to undo. Add fields like 'add site X' or 'add people Y'.")
            return "ok", 200

        # Handle status command
        if normalized_text_lower in ("status", "/status"):
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "Current report status:\n\n" + tpl +
                "\n\nAdd more fields or use commands like '/export pdf'.")
            return "ok", 200

        # Handle export pdf command
        if normalized_text_lower in ("export pdf", "/export pdf"):
            pdf_buffer = generate_pdf_report(sess["structured_data"])
            if pdf_buffer:
                if send_pdf_to_user(chat_id, pdf_buffer):
                    send_telegram_message(chat_id, "PDF report sent successfully!")
                else:
                    send_telegram_message(chat_id, "⚠️ Failed to send PDF report. Please try again later.")
            else:
                send_telegram_message(chat_id, "⚠️ Failed to generate PDF report. Please try again later.")
            return "ok", 200

        # Handle clear commands
        clear_match = re.match(FIELD_PATTERNS["clear"], text, re.IGNORECASE)
        if clear_match:
            field = clear_match.group(1).lower()
            sess["command_history"].append(sess["structured_data"].copy())
            sess["structured_data"][field] = []
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Cleared {field}\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Process new data or corrections
        extracted = extract_site_report(text)
        logger.info({"event": "extracted_data", "extracted": extracted})
        if extracted.get("reset"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            send_telegram_message(chat_id,
                "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        if not extracted:
            # Fuzzy matching for misspelled commands
            known_commands = ["add site", "add people", "add tools", "delete company", "correct site"]
            best_match = max(known_commands, key=lambda x: SequenceMatcher(None, text.lower(), x).ratio(), default="")
            similarity = SequenceMatcher(None, text.lower(), best_match).ratio()
            suggestion = f" Did you mean '{best_match}'?" if similarity > 0.6 else ""
            send_telegram_message(chat_id,
                f"⚠️ Unrecognized input: '{text}'. Try formats like 'add site Downtown Project', 'add people Tobias', 'add issue Power outage', or 'delete company Acme'.{suggestion}")
            return "ok", 200

        sess["command_history"].append(sess["structured_data"].copy())
        original_data = sess["structured_data"].copy()
        sess["structured_data"] = merge_structured_data(
            sess["structured_data"], enrich_with_date(extracted)
        )
        save_to_sharepoint(chat_id, sess["structured_data"])
        save_session_data(session_data)

        # Check if deletion was successful
        for field in ["company", "people", "tools", "service", "issues", "activities", "roles"]:
            if field in extracted and isinstance(extracted[field], (dict, list)):
                if isinstance(extracted[field], dict) and "delete" in extracted[field]:
                    value = extracted[field]["delete"]
                    if any(value.lower() in str(item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description" if field == "issues" else "name" if field == "roles" else item, "")).lower() for item in original_data.get(field, [])) and \
                       all(value.lower() in str(item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description" if field == "issues" else "name" if field == "roles" else item, "")).lower() for item in sess["structured_data"].get(field, [])):
                        send_telegram_message(chat_id,
                            f"⚠️ Couldn't find '{value}' in {field} to delete.\n\nCurrent report:\n\n{summarize_data(sess['structured_data'])}\n\nTry 'delete {field} {value}' with an existing entry.")
                        return "ok", 200
                elif isinstance(extracted[field], list) and any("delete" in item for item in extracted[field]):
                    value = next(item["name"] or item["item"] or item["task"] or item["description"] for item in extracted[field] if "delete" in item)
                    if any(value.lower() in str(item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description" if field == "issues" else item, "")).lower() for item in original_data.get(field, [])) and \
                       all(value.lower() in str(item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description" if field == "issues" else item, "")).lower() for item in sess["structured_data"].get(field, [])):
                        send_telegram_message(chat_id,
                            f"⚠️ Couldn't find '{value}' in {field} to delete.\n\nCurrent report:\n\n{summarize_data(sess['structured_data'])}\n\nTry 'delete {field} {value}' with an existing entry.")
                        return "ok", 200

        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            f"Here’s what I understood:\n\n{tpl}\n\nIs this correct? Reply with corrections or more details.")
        return "ok", 200
    except Exception as e:
        logger.error({"event": "webhook_error", "error": str(e)})
        return "error", 500

@app.get("/")
def health():
    logger.info({"event": "health_check"})
    return "OK", 200

# Log startup
logger.info({"event": "app_init", "message": "Initializing Flask app for deployment"})

if __name__ == "__main__":
    try:
        logger.info({"event": "app_start", "mode": "local"})
        app.run(port=int(os.getenv("PORT", 10000)), debug=True)
    except Exception as e:
        logger.error({"event": "app_start_error", "error": str(e)})
        raise
