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

# --- Field Mapping ---
field_mapping = {
    'site': 'site_name', 'sites': 'site_name',
    'segment': 'segment', 'segments': 'segment',
    'category': 'category', 'categories': 'category',
    'company': 'company', 'companies': 'company',
    'person': 'people', 'people': 'people', 'persons': 'people', 'peoples': 'people',
    'role': 'roles', 'roles': 'roles',
    'tool': 'tools', 'tools': 'tools',
    'service': 'service', 'services': 'service',
    'activity': 'activities', 'activities': 'activities',
    'issue': 'issues', 'issues': 'issues',
    'time': 'time', 'times': 'time',
    'weather': 'weather', 'weathers': 'weather',
    'impression': 'impression', 'impressions': 'impression',
    'comment': 'comments', 'comments': 'comments'
}

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
You are an AI assistant extracting a construction site report from user input. Extract all explicitly mentioned fields and return them in JSON format. Process the entire input as a single unit, splitting on commas or periods only when fields are clearly separated by keywords. Map natural language phrases and standardized commands (add, insert, delete, correct, adjust) to fields accurately, prioritizing specific fields over comments or site_name. Do not treat reset commands ("new", "new report", "reset", "reset report", "/new") as comments or fields; return {} for these. Handle "none" inputs (e.g., "Tools: none") as clearing the respective field, and vague inputs (e.g., "Activities: many") by adding them and noting clarification needed.

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
- add|insert <category> <value>: Add a value to the category (e.g., "add site Downtown Project" or "insert issues water leakage").
- delete <category> [value|from <category> <value>]: Remove a value or clear the category (e.g., "delete activities Laying foundation", "delete Jonas from people", or "delete companies").
- correct|adjust <category> <old> to <new>|correct spelling <category> <value>: Update a value or correct spelling (e.g., "correct site Downtown to Uptown", "adjust company Techmont to Techmond AG", "correct spelling roles Johnas").
- <category>: <value>: Add a value (e.g., "Services: abc" -> "service": [{"task": "abc"}]).
- <category>: none: Clear the category (e.g., "Tools: none" -> "tools": []).

Rules:
- Accept both singular and plural category names (e.g., "issue" or "issues", "company" or "companies").
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
- For companies:
  - Recognize "add company <name>", "company: <name>", or "add <name> as company".
  - Handle "delete company <name>" to remove the company.
  - Handle "correct company <old> to <new>" to update the company name.
- Comments should only include non-field-specific notes.
- Return {} for reset commands or irrelevant inputs.
- Case-insensitive matching.
- Handle natural language inputs flexibly, allowing variations like "Activities: laying foundation", "Add issue power outage", "Delete Jonas from people", or "correct spelling roles Johnas".

Examples:
1. Input: "add site Central Plaza, insert segment 5, add issues Power outage"
   Output: {"site_name": "Central Plaza", "segment": "5", "issues": [{"description": "Power outage"}]}
2. Input: "new report"
   Output: {}
3. Input: "Services: wall building"
   Output: {"service": [{"task": "wall building"}]}
4. Input: "Tools: none"
   Output: {"tools": []}
5. Input: "Roles supervisor"
   Output: {"people": ["User"], "roles": [{"name": "User", "role": "Supervisor"}]}
6. Input: "Work was done at the East Wing."
   Output: {"site_name": "East Wing", "activities": ["Work was done"]}
7. Input: "insert Anna as engineer to people"
   Output: {"people": ["Anna"], "roles": [{"name": "Anna", "role": "Engineer"}]}
8. Input: "Activities: many"
   Output: {"activities": ["many"]}
9. Input: "delete companies"
   Output: {"company": []}
10. Input: "correct companies Techmont AG to Techmond AG"
    Output: {"company": [{"name": "Techmond AG"}]}
11. Input: "adjust issues water leakage to pipe burst"
    Output: {"issues": [{"description": "pipe burst"}]}
12. Input: "Activities: laying foundation"
    Output: {"activities": ["laying foundation"]}
13. Input: "Add issue power outage"
    Output: {"issues": [{"description": "power outage"}]}
14. Input: "Delete Jonas from people"
    Output: {"people": []} // Assuming Jonas was in the list
15. Input: "correct spelling roles Johnas to Jonas"
    Output: {"roles": [{"name": "Jonas", "role": "Supervisor"}]} // Assuming Johnas was in roles
16. Input: "Activities delete tone"
    Output: {"activities": []} // Assuming tone was in the list
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
        logger.error({"event": "load_session_data_error", "error": str(e)})
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
        logger.error({"event": "save_session_data_error", "error": str(e)})

try:
    session_data = load_session_data()
    logger.info({"event": "session_data_initialized"})
except Exception as e:
    logger.error({"event": "session_data_initialization_failed", "error": str(e)})
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
    "site_name": r'^(?:(?:add|insert)\s+sites?\s+|sites?\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "segment": r'^(?:(?:add|insert)\s+segments?\s+|segments?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "category": r'^(?:(?:add|insert)\s+categories?\s+|categories?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|segment|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "impression": r'^(?:(?:add|insert)\s+impressions?\s+|impressions?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|comments)\s*:)|$|\s*$)',
    "people": r'^(?:(?:add|insert)\s+(?:peoples?|persons?)\s+|(?:peoples?|persons?)\s*[:,]?\s*|(?:add|insert)\s+[^,]+?\s+as\s+(?:peoples?|persons?)\s*)([^,\s]+)(?:\s+as\s+[^,\s]+)?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "role": r'^(?:(?:add|insert)\s+|(?:peoples?|persons?)\s+)?(\w+\s+\w+)\s*[:,]?\s*as\s+([^,\s]+)(?:\s+to\s+(?:peoples?|persons?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)|^(?:persons?|peoples?)\s*[:,]?\s*(\w+\s+\w+)\s*,\s*roles?\s*[:,]?\s*([^,\s]+)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "supervisor": r'^(?:supervisors?\s*(?:were|are)\s+|i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised|(?:add|insert)\s+roles?\s*[:,]?\s*supervisor\s*|roles?\s*[:,]?\s*supervisor\s*$)([^,]+?)?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "company": r'^(?:(?:add|insert)\s+compan(?:y|ies)\s+|compan(?:y|ies)\s*[:,]?\s*|(?:add|insert)\s+([^,]+?)\s+as\s+compan(?:y|ies)\s*)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "service": r'^(?:(?:add|insert)\s+services?\s+|services?\s*[:,]?\s*|services?\s*(?:were|provided)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "tool": r'^(?:(?:add|insert)\s+tools?\s+|tools?\s*[:,]?\s*|tools?\s*used\s*(?:included|were)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "activity": r'^(?:(?:add|insert)\s+activit(?:y|ies)\s+|activit(?:y|ies)\s*[:,]?\s*|activit(?:y|ies)\s*(?:covered|included)?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "issue": r'^(?:(?:add|insert)\s+issues?\s+|issues?\s*[:,]?\s*|issues?\s*(?:encountered|included)?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|time|weather|impression|comments)\s*:)|$|\s*$)',
    "weather": r'^(?:(?:add|insert)\s+weathers?\s+|weathers?\s*[:,]?\s*|weather\s+was\s+|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "time": r'^(?:(?:add|insert)\s+times?\s+|times?\s*[:,]?\s*|time\s+spent\s+|morning\s+time\s*|afternoon\s+time\s*|evening\s+time\s*)(morning|afternoon|evening|full day)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|weather|impression|comments)\s*:)|$|\s*$)',
    "comments": r'^(?:(?:add|insert)\s+comments?\s+|comments?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression)\s*:)|$|\s*$)',
    "clear": r'^(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?)\s*[:,]?\s*none$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": r'^(?:delete|remove)\s+(?:from\s+)?(?:sites?|segments?|categories?|compan(?:y|ies)|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?)\s*(?:from\s+)?\s*([^\s,]+(?:\s+[^\s,]+)*)?$',
    "correct": r'^(?:correct|adjust|update)\s+(?:spelling\s+)?(?:sites?|segments?|categories?|compan(?:y|ies)|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?)\s+([^,\s]+(?:\s+[^,\s]+)*)(?:\s+to\s+([^,]+?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)'
}

# Validate regex patterns
try:
    for field, pattern in FIELD_PATTERNS.items():
        re.compile(pattern, re.IGNORECASE)
    logger.info({"event": "regex_patterns_validated"})
except Exception as e:
    logger.error({"event": "regex_pattern_validation_failed", "field": field, "error": str(e)})
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
    logger.info({"event": "generate_pdf_report", "status": "starting"})
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
    logger.info({"event": "send_pdf_to_user", "chat_id": chat_id, "status": "starting"})
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
        logger.info({"event": "date_enriched", "date": d["date"]})
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
            ", ".join(c.get("name", "") for c in d.get("company", []) if isinstance(c, dict) and c.get("name")) or ""
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
                f"{s.get('task', '')}" for s in d.get("service", []) if isinstance(s, dict) and s.get('task')
            ) or ""
        )
        lines.append(
            "🛠️ **Tools**: " +
            ", ".join(
                f"{t.get('item', '')}" for t in d.get("tools", []) if isinstance(t, dict) and t.get('item')
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

def clean_value(value, field):
    """Clean input value to remove erroneous prefixes like 's:' and normalize."""
    if not value:
        return value
    cleaned = re.sub(r'^(?:s\s*[:\s]*|add\s+|insert\s+|from\s+)', '', value.strip(), flags=re.IGNORECASE)
    # Prevent common transcription errors (e.g., 'tone' for 'stone')
    cleaned = cleaned.replace('tone', 'stone') if 'tone' in cleaned.lower() and field == 'activities' else cleaned
    logger.info({"event": "cleaned_field_value", "field": field, "raw_value": value, "cleaned_value": cleaned})
    return cleaned

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
        processed_result = {"company": [], "roles": [], "tools": [], "service": [], "activities": [], "issues": [], "people": []}
        seen_fields = set()

        for cmd in commands:
            # Handle delete commands
            delete_match = re.match(FIELD_PATTERNS["delete"], cmd, re.IGNORECASE)
            if delete_match:
                raw_field = delete_match.group(1).lower()
                value = delete_match.group(2).strip() if delete_match.group(2) else None
                field = field_mapping.get(raw_field, raw_field)
                logger.info({"event": "delete_command_in_list", "field": field, "value": value})
                if field in processed_result:
                    processed_result[field].append({"delete": value})
                continue

            # Handle correct commands
            correct_match = re.match(FIELD_PATTERNS["correct"], cmd, re.IGNORECASE)
            if correct_match:
                raw_field = correct_match.group(1).lower()
                old_value = correct_match.group(2).strip()
                new_value = correct_match.group(3).strip() if correct_match.group(3) else None
                field = field_mapping.get(raw_field, raw_field)
                logger.info({"event": "correct_command_in_list", "field": field, "old_value": old_value, "new_value": new_value})
                if field in processed_result:
                    processed_result[field].append({"correct": {"old": old_value, "new": new_value}})
                continue

            # Process other commands
            cmd_result = extract_single_command(cmd)
            if cmd_result.get("reset"):
                return {"reset": True}
            for key, value in cmd_result.items():
                if key in seen_fields and key not in ["people", "company", "roles", "tools", "service", "activities", "issues"]:
                    continue
                seen_fields.add(key)
                if key in processed_result:
                    processed_result[key].extend(value)
                elif key in ["people", "activities"]:
                    result.setdefault(key, []).extend(value)
                else:
                    result[key] = value

        # Process deletions and corrections
        for field in processed_result:
            if processed_result[field]:
                final_items = []
                existing_items = (
                    [item["name"] for item in result.get(field, []) if isinstance(item, dict) and "name" in item] if field == "company" else
                    [item["description"] for item in result.get(field, []) if isinstance(item, dict) and "description" in item] if field == "issues" else
                    [item["task"] for item in result.get(field, []) if isinstance(item, dict) and "task" in item] if field == "service" else
                    [item["item"] for item in result.get(field, []) if isinstance(item, dict) and "item" in item] if field == "tools" else
                    [f"{item['name']} ({item['role']})" for item in result.get(field, []) if isinstance(item, dict) and "name" in item and "role" in item] if field == "roles" else
                    result.get(field, []) if field in ["people", "activities"] else
                    []
                )
                for item in processed_result[field]:
                    if "delete" in item:
                        value = item["delete"]
                        if value:
                            if field == "people":
                                existing_items = [i for i in existing_items if i.lower() != value.lower()]
                                logger.info({"event": f"{field}_deleted", "value": value})
                            elif field == "activities":
                                existing_items = [i for i in existing_items if i.lower() != value.lower()]
                                logger.info({"event": f"{field}_deleted", "value": value})
                            else:
                                existing_items = [i for i in existing_items if i.lower() != value.lower()]
                                logger.info({"event": f"{field}_deleted", "value": value})
                        else:
                            existing_items = []
                            logger.info({"event": f"{field}_cleared"})
                    elif "correct" in item:
                        old_value = item["correct"]["old"]
                        new_value = item["correct"]["new"]
                        if new_value:
                            if field == "people":
                                existing_items = [new_value if i.lower() == old_value.lower() else i for i in existing_items]
                                logger.info({"event": f"{field}_corrected", "old_value": old_value, "new_value": new_value})
                            elif field == "activities":
                                existing_items = [new_value if i.lower() == old_value.lower() else i for i in existing_items]
                                logger.info({"event": f"{field}_corrected", "old_value": old_value, "new_value": new_value})
                            elif field == "roles":
                                existing_items = [f"{new_value} ({i.split('(')[1]}" if i.lower().startswith(old_value.lower()) else i for i in existing_items]
                                logger.info({"event": f"{field}_corrected", "old_value": old_value, "new_value": new_value})
                            else:
                                existing_items = [new_value if i.lower() == old_value.lower() else i for i in existing_items]
                                logger.info({"event": f"{field}_corrected", "old_value": old_value, "new_value": new_value})
                        else:
                            logger.info({"event": f"{field}_correct_prompt", "old_value": old_value})
                    else:
                        final_items.append(item)
                if field == "company":
                    result[field] = [{"name": i} for i in existing_items if isinstance(i, str)] + final_items
                elif field == "issues":
                    result[field] = [{"description": i} for i in existing_items if isinstance(i, str)] + final_items
                elif field == "service":
                    result[field] = [{"task": i} for i in existing_items if isinstance(i, str)] + final_items
                elif field == "tools":
                    result[field] = [{"item": i} for i in existing_items if isinstance(i, str)] + final_items
                elif field == "roles":
                    result[field] = [
                        {"name": i.split(' (')[0], "role": i.split(' (')[1].rstrip(')')}
                        for i in existing_items if isinstance(i, str) and ' (' in i
                    ] + final_items
                elif field == "people":
                    result[field] = existing_items + [item for item in final_items if isinstance(item, str)]
                elif field == "activities":
                    result[field] = existing_items + [item for item in final_items if isinstance(item, str)]

        logger.info({"event": "multi_field_extracted", "result": result})
        return result
    except Exception as e:
        logger.error({"event": "extract_site_report_error", "input": text, "error": str(e)})
        raise

def extract_single_command(text):
    try:
        result = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())
        logger.info({"event": "extract_single_command_start", "input": normalized_text})

        reset_match = re.match(FIELD_PATTERNS["reset"], normalized_text, re.IGNORECASE)
        if reset_match:
            logger.info({"event": "reset_command", "input": normalized_text})
            return {"reset": True}

        if normalized_text.lower() in ("undo", "/undo"):
            logger.info({"event": "undo_command"})
            return {"undo": True}

        if normalized_text.lower() in ("status", "/status"):
            logger.info({"event": "status_command"})
            return {"status": True}

        if normalized_text.lower() in ("export pdf", "/export pdf"):
            logger.info({"event": "export_pdf_command"})
            return {"export_pdf": True}

        correct_match = re.match(FIELD_PATTERNS["correct"], normalized_text, re.IGNORECASE)
        if correct_match:
            raw_field = correct_match.group(1).lower()
            old_value = correct_match.group(2).strip()
            new_value = correct_match.group(3).strip() if correct_match.group(3) else None
            field = field_mapping.get(raw_field, raw_field)
            logger.info({"event": "corrected_field", "field": field, "old": old_value, "new": new_value})
            if new_value:
                if field in ["site_name", "segment", "category", "time", "weather", "impression", "comments"]:
                    result[field] = new_value
                elif field in ["company"]:
                    result[field] = [{"name": new_value}]
                elif field in ["tools"]:
                    result[field] = [{"item": new_value}]
                elif field in ["service"]:
                    result[field] = [{"task": new_value}]
                elif field in ["issues"]:
                    result[field] = [{"description": new_value}]
                elif field in ["activities"]:
                    result[field] = [new_value]
                elif field == "people":
                    result["people"] = [new_value]
                elif field == "roles":
                    result["roles"] = [{"name": new_value, "role": "Supervisor"}]  # Preserve existing role or default
            else:
                result["correct_prompt"] = {"field": field, "value": old_value}
            return result

        for raw_field, pattern in FIELD_PATTERNS.items():
            if raw_field in ["reset", "delete", "correct", "clear"]:
                continue
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                field = field_mapping.get(raw_field, raw_field)
                logger.info({"event": "field_matched", "raw_field": raw_field, "mapped_field": field, "input": text})
                if field == "site_name" and re.search(r'\b(add|insert|delete|remove|correct|adjust|update|none|as|role|new|reset)\b', text.lower()):
                    logger.info({"event": "skipped_site_name", "reason": "command-like input"})
                    continue
                if field == "people":
                    name = clean_value(match.group(1), field)
                    result["people"] = [name]
                    logger.info({"event": "extracted_field", "field": "people", "value": name})
                elif field == "role":
                    name = clean_value(match.group(1) or match.group(3), field)
                    role = match.group(2) or match.group(4)
                    role = role.title()
                    result["people"] = [name.strip()]
                    result["roles"] = [{"name": name.strip(), "role": role}]
                    logger.info({"event": "extracted_field", "field": "roles", "name": name, "role": role})
                elif field == "supervisor":
                    if match.group(1):
                        names = [clean_value(name.strip(), field) for name in match.group(1).split("and") if name.strip()]
                        result["people"] = names
                        result["roles"] = [{"name": name, "role": "Supervisor"} for name in names]
                    else:
                        result["people"] = ["User"]
                        result["roles"] = [{"name": "User", "role": "Supervisor"}]
                    logger.info({"event": "extracted_field", "field": "roles", "value": match.group(1) or "User"})
                elif field == "company":
                    name = clean_value(match.group(2) if match.group(2) else match.group(1), field)
                    if re.match(r'^(?:delete|remove|add|insert|correct|adjust|update)\b', name.lower()):
                        logger.info({"event": "skipped_company", "reason": "command-like name", "value": name})
                        continue
                    result["company"] = [{"name": name}]
                    logger.info({"event": "extracted_field", "field": "company", "value": name})
                elif field == "clear":
                    field_name = field_mapping.get(match.group(1).lower(), match.group(1).lower())
                    result[field_name] = [] if field_name in ["issues", "activities", "tools", "service", "company", "people", "roles"] else ""
                    logger.info({"event": "extracted_field", "field": field_name, "value": "none"})
                elif field in ["service"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"task": value}]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                elif field in ["tool"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"item": value}]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                elif field == "issue":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"description": value}]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                elif field == "activity":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [value]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                else:
                    value = clean_value(match.group(1), field)
                    result[field] = value
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                return result

        messages = [
            {"role": "system", "content": "Extract explicitly stated fields from construction site report input. Handle multi-field inputs by processing the entire input as a single unit. Return JSON with extracted fields."},
            {"role": "user", "content": gpt_prompt + "\nInput text: " + text}
        ]
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo", messages=messages, temperature=0.2
            )
            raw_response = response.choices[0].message.content
            logger.info({"event": "gpt_response", "raw_response": raw_response})
            data = json.loads(raw_response)
            logger.info({"event": "gpt_extracted", "data": data})
            for field in ["category", "segment"]:
                if field in data and isinstance(data[field], str):
                    data[field] = clean_value(data[field], field)
            for field in ["tools", "service", "issues"]:
                if field in data:
                    for item in data[field]:
                        if isinstance(item, dict):
                            if field == "tools" and "item" in item:
                                item["item"] = clean_value(item["item"], field)
                            elif field == "service" and "task" in item:
                                item["task"] = clean_value(item["task"], field)
                            elif field == "issues" and "description" in item:
                                item["description"] = clean_value(item["description"], field)
            if "activities" in data:
                data["activities"] = [clean_value(item, "activities") for item in data["activities"] if isinstance(item, str)]
            if "roles" in data:
                for role in data["roles"]:
                    if isinstance(role, dict) and "name" in role and role["name"] not in data.get("people", []):
                        data.setdefault("people", []).append(clean_value(role["name"], "people"))
            if not data and text.strip():
                issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error|injury)\b'
                activity_keywords = r'\b(work\s+was\s+done|activity|activities|task|progress|construction|building|laying|setting|wiring|installation|scaffolding)\b'
                location_keywords = r'\b(at|in|on)\b'
                if re.search(issue_keywords, text.lower()):
                    cleaned_text = clean_value(text.strip(), "issues")
                    data = {"issues": [{"description": cleaned_text}]}
                    logger.info({"event": "fallback_issue", "data": data})
                elif re.search(activity_keywords, text.lower()) and re.search(location_keywords, text.lower()):
                    parts = re.split(r'\b(at|in|on)\b', text, flags=re.IGNORECASE)
                    location = ", ".join(clean_value(part.strip().title(), "site_name") for part in parts[2::2] if part.strip())
                    activity = clean_value(parts[0].strip(), "activities")
                    data = {"site_name": location, "activities": [activity]}
                    logger.info({"event": "fallback_activity_site", "data": data})
                else:
                    data = {"comments": clean_value(text.strip(), "comments")}
                    logger.info({"event": "fallback_comments", "data": data})
            return data
        except Exception as e:
            logger.error({"event": "gpt_extract_error", "input": text, "error": str(e)})
            if text.strip():
                issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error|injury)\b'
                if re.search(issue_keywords, text.lower()):
                    cleaned_text = clean_value(text.strip(), "issues")
                    data = {"issues": [{"description": cleaned_text}]}
                    logger.info({"event": "fallback_issue_error", "data": data})
                    return data
                logger.info({"event": "fallback_comments_error", "input": text})
                return {"comments": clean_value(text.strip(), "comments")}
            return {}
    except Exception as e:
        logger.error({"event": "extract_single_command_error", "input": text, "error": str(e)})
        raise

def string_similarity(a, b):
    try:
        similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
        logger.info({"event": "string_similarity", "a": a, "b": b, "similarity": similarity})
        return similarity
    except Exception as e:
        logger.error({"event": "string_similarity_error", "error": str(e)})
        raise

def merge_structured_data(existing, new):
    try:
        merged = existing.copy()
        for key, value in new.items():
            if key in ["reset", "undo", "status", "export_pdf", "correct_prompt"]:
                continue
            if key in ["company", "roles", "tools", "service", "issues"]:
                if value == []:
                    merged[key] = []
                    logger.info({"event": "cleared_list", "field": key})
                    continue
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else []
                for new_item in new_items:
                    if not isinstance(new_item, dict):
                        continue
                    if key == "company" and "name" in new_item:
                        new_name = new_item.get("name", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("name", ""), new_name) > 0.6):
                                existing_list[i] = new_item
                                replaced = True
                                logger.info({"event": "replaced_company", "old": existing_item.get("name"), "new": new_name})
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            logger.info({"event": "added_company", "name": new_name})
                    elif key == "roles" and "name" in new_item:
                        new_name = new_item.get("name", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("name", ""), new_name) > 0.6):
                                existing_list[i] = new_item
                                replaced = True
                                logger.info({"event": "replaced_role", "name": new_name})
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            logger.info({"event": "added_role", "name": new_name})
                    elif key == "issues" and "description" in new_item:
                        new_desc = new_item.get("description", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("description", ""), new_desc) > 0.6):
                                existing_list[i] = new_item
                                replaced = True
                                logger.info({"event": "replaced_issue", "old": existing_item.get("description"), "new": new_desc})
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            logger.info({"event": "added_issue", "description": new_desc})
                    elif key == "tools" and "item" in new_item:
                        new_item_name = new_item.get("item", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("item", ""), new_item_name) > 0.6):
                                existing_list[i] = new_item
                                replaced = True
                                logger.info({"event": "replaced_tool", "old": existing_item.get("item"), "new": new_item_name})
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            logger.info({"event": "added_tool", "item": new_item_name})
                    elif key == "service" and "task" in new_item:
                        new_task = new_item.get("task", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("task", ""), new_task) > 0.6):
                                existing_list[i] = new_item
                                replaced = True
                                logger.info({"event": "replaced_service", "old": existing_item.get("task"), "new": new_task})
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            logger.info({"event": "added_service", "task": new_task})
                merged[key] = existing_list
            elif key in ["activities", "people"]:
                if value == []:
                    merged[key] = []
                    logger.info({"event": "cleared_list", "field": key})
                    continue
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else []
                for item in new_items:
                    if isinstance(item, str) and item not in existing_list:
                        existing_list.append(item)
                        logger.info({"event": f"added_{key}", "value": item})
                merged[key] = existing_list
            else:
                if value == "" and key in ["comments"]:
                    merged[key] = ""
                    logger.info({"event": "cleared_field", "field": key})
                elif value:
                    merged[key] = value
                    logger.info({"event": "updated_field", "field": key, "value": value})
        logger.info({"event": "merged_data", "data": json.dumps(merged, indent=2)})
        return merged
    except Exception as e:
        logger.error({"event": "merge_structured_data_error", "error": str(e)})
        raise

def delete_entry(data, field, value=None):
    try:
        logger.info({"event": "delete_entry", "field": field, "value": value})
        if field in ["company", "roles", "tools", "service", "issues"]:
            if value:
                data[field] = [item for item in data[field]
                              if not (isinstance(item, dict) and
                                      (item.get("name", "").lower() == value.lower() or
                                       item.get("description", "").lower() == value.lower() or
                                       item.get("item", "").lower() == value.lower() or
                                       item.get("task", "").lower() == value.lower()))]
            else:
                data[field] = []
        elif field in ["people"]:
            if value:
                data[field] = [item for item in data[field] if item.lower() != value.lower()]
                data["roles"] = [role for role in data.get("roles", []) if role.get("name", "").lower() != value.lower()]
                logger.info({"event": "people_deleted", "value": value})
            else:
                data[field] = []
                data["roles"] = []
                logger.info({"event": "people_cleared"})
        elif field in ["activities"]:
            if value:
                data[field] = [item for item in data[field] if item.lower() != value.lower()]
                logger.info({"event": "activities_deleted", "value": value})
            else:
                data[field] = []
                logger.info({"event": "activities_cleared"})
        elif field in ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]:
            data[field] = ""
            logger.info({"event": f"{field}_cleared"})
        logger.info({"event": "data_after_deletion", "data": json.dumps(data, indent=2)})
        return data
    except Exception as e:
        logger.error({"event": "delete_entry_error", "field": field, "error": str(e)})
        raise

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        logger.info({"event": "webhook_hit", "request_data": request.get_data(as_text=True)})
        data = request.get_json(force=True)
        logger.info({"event": "webhook_json", "data": data})
        if not data or "message" not in data:
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
                "command_history": deque(maxlen=MAX_HISTORY),
                "awaiting_spelling_correction": None
            }
            logger.info({"event": "new_session_created", "chat_id": chat_id})
        sess = session_data[chat_id]

        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id,
                    "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'add site Downtown Project' or 'add people Tobias').")
                return "ok", 200
            logger.info({"event": "transcribed_voice", "text": text})

        current_time = time()

        # Handle spelling correction response
        if sess.get("awaiting_spelling_correction"):
            field, old_value = sess["awaiting_spelling_correction"]
            new_value = text.strip()
            logger.info({"event": "spelling_correction_response", "field": field, "old_value": old_value, "new_value": new_value})
            sess["awaiting_spelling_correction"] = None
            sess["command_history"].append(sess["structured_data"].copy())
            if field == "people":
                sess["structured_data"]["people"] = [new_value if i.lower() == old_value.lower() else i for i in sess["structured_data"].get("people", [])]
                sess["structured_data"]["roles"] = [
                    {"name": new_value, "role": role["role"]} if role.get("name", "").lower() == old_value.lower() else role
                    for role in sess["structured_data"].get("roles", [])
                ]
            elif field == "roles":
                sess["structured_data"]["roles"] = [
                    {"name": new_value, "role": role["role"]} if role.get("name", "").lower() == old_value.lower() else role
                    for role in sess["structured_data"].get("roles", [])
                ]
                if new_value not in sess["structured_data"].get("people", []):
                    sess["structured_data"]["people"].append(new_value)
            elif field == "activities":
                sess["structured_data"]["activities"] = [new_value if i.lower() == old_value.lower() else i for i in sess["structured_data"].get("activities", [])]
            else:
                sess["structured_data"][field] = [
                    {"name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description": new_value}
                    if item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description", "").lower() == old_value.lower()
                    else item
                    for item in sess["structured_data"].get(field, [])
                ]
            save_session_data(session_data)
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Corrected {field} from '{old_value}' to '{new_value}'.\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Handle reset confirmation
        if sess.get("awaiting_reset_confirmation", False):
            normalized_text = re.sub(r'[.!?]\s*$', '', text.strip()).lower()
            logger.info({"event": "reset_confirmation", "text": normalized_text, "pending_input": sess["pending_input"]})
            if normalized_text in ("yes", "new", "new report"):
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
            elif normalized_text in ("no", "existing", "continue"):
                text = sess["pending_input"]
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["last_interaction"] = current_time
            else:
                send_telegram_message(chat_id,
                    "Please clarify: Reset the report? Reply 'yes' or 'no'.")
                return "ok", 200

        # Check for reset based on pause
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip()) if text else ""
        if normalized_text:
            normalized_text_lower = normalized_text.lower()
        else:
            normalized_text_lower = ""
            send_telegram_message(chat_id,
                "⚠️ Empty input received. Please provide a valid command (e.g., 'add site Downtown Project' or 'add issue Power outage').")
            return "ok", 200

        if (current_time - sess.get("last_interaction", 0) > PAUSE_THRESHOLD and
                normalized_text_lower not in ("yes", "no", "new", "new report", "reset", "reset report", "/new", "existing", "continue")):
            sess["pending_input"] = text
            sess["awaiting_reset_confirmation"] = True
            sess["last_interaction"] = current_time
            save_session_data(session_data)
            logger.info({"event": "reset_prompt", "pending_input": text})
            send_telegram_message(chat_id,
                "It’s been a while! Reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        sess["last_interaction"] = current_time

        # Handle explicit reset commands
        if normalized_text_lower in ("new", "new report", "reset", "reset report", "/new"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            logger.info({"event": "reset_initiated"})
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
            raw_field = clear_match.group(1).lower()
            field = field_mapping.get(raw_field, raw_field)
            sess["command_history"].append(sess["structured_data"].copy())
            sess["structured_data"][field] = [] if field in ["issues", "activities", "tools", "service", "company", "people", "roles"] else ""
            save_session_data(session_data)
            logger.info({"event": "cleared_field", "field": field})
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Cleared {field}\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Handle deletion commands
        delete_match = re.match(FIELD_PATTERNS["delete"], text, re.IGNORECASE)
        if delete_match:
            groups = [delete_match.group(i) for i in range(len(delete_match.groups()) + 1)]
            logger.info({"event": "delete_command_detected", "groups": groups})
            raw_field = delete_match.group(1).lower()
            value = delete_match.group(2).strip() if delete_match.group(2) else None
            field = field_mapping.get(raw_field, raw_field)
            if not field:
                logger.error({"event": "delete_command_error", "text": text, "error": "No field captured"})
                send_telegram_message(chat_id,
                    f"⚠️ Invalid delete command: '{text}'. Try formats like 'delete company Taekwondo Agi', 'delete Jonas from people', or 'activities delete tone'.")
                return "ok", 200
            sess["command_history"].append(sess["structured_data"].copy())
            sess["structured_data"] = delete_entry(sess["structured_data"], field, value)
            save_session_data(session_data)
            logger.info({"event": "deleted", "field": field, "value": value})
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Removed {field}" + (f": {value}" if value else "") + f"\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Process new data or corrections
        extracted = extract_site_report(text)
        logger.info({"event": "extracted_data", "extracted": extracted})
        if extracted.get("reset"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            logger.info({"event": "reset_initiated_extracted"})
            send_telegram_message(chat_id,
                "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200
        if extracted.get("correct_prompt"):
            field = extracted["correct_prompt"]["field"]
            value = extracted["correct_prompt"]["value"]
            sess["awaiting_spelling_correction"] = (field, value)
            save_session_data(session_data)
            logger.info({"event": "awaiting_spelling_correction", "field": field, "value": value})
            send_telegram_message(chat_id,
                f"Please provide the correct spelling for '{value}' in {field}.")
            return "ok", 200
        if not any(k in extracted for k in ["company", "people", "roles", "tools", "service", "activities", "issues", "time", "weather", "impression", "comments", "segment", "category", "site_name"]):
            logger.warning({"event": "unrecognized_input", "input": text})
            send_telegram_message(chat_id,
                f"⚠️ Unrecognized input: '{text}'. Try formats like 'add site Downtown Project', 'add issue power outage',
