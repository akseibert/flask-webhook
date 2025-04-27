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
- delete <category> [value]: Remove a value or clear the category (e.g., "delete activities Laying foundation" or "delete companies").
- correct|adjust <category> <old> to <new>: Update a value (e.g., "correct site Downtown to Uptown" or "adjust company Techmont to Techmond AG").
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
    "site_name": r'^(?:(?:add|insert)\s+sites?\s+|sites?\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)',
    "segment": r'^(?:(?:add|insert)\s+segments?\s+|segments?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "category": r'^(?:(?:add|insert)\s+categories?\s+|categories?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|segment|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "impression": r'^(?:(?:add|insert)\s+impressions?\s+|impressions?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|comments)\s*:)|$)',
    "people": r'^(?:(?:add|insert)\s+(?:peoples?|persons?)\s+|(?:peoples?|persons?)\s*[:,]?\s*|(?:add|insert)\s+[^,]+?\s+as\s+(?:peoples?|persons?)\s*)([^,\s]+)(?:\s+as\s+[^,\s]+)?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)',
    "role": r'^(?:(?:add|insert)\s+|(?:peoples?|persons?)\s+)?(\w+\s+\w+)\s*[:,]?\s*as\s+([^,\s]+)(?:\s+to\s+(?:peoples?|persons?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)|^(?:persons?|peoples?)\s*[:,]?\s*(\w+\s+\w+)\s*,\s*roles?\s*[:,]?\s*([^,\s]+)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)',
    "supervisor": r'^(?:supervisors?\s*(?:were|are)\s+|i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised|(?:add|insert)\s+roles?\s*[:,]?\s*supervisor\s*|roles?\s*[:,]?\s*supervisor\s*$)([^,]+?)?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)',
    "company": r'^(?:(?:add|insert)\s+compan(?:y|ies)\s+|compan(?:y|ies)\s*[:,]?\s*|(?:add|insert)\s+([^,]+?)\s+as\s+compan(?:y|ies)\s*)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)',
    "service": r'^(?:(?:add|insert)\s+services?\s+|services?\s*[:,]?\s*|services?\s*(?:were|provided)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)',
    "tool": r'^(?:(?:add|insert)\s+tools?\s+|tools?\s*[:,]?\s*|tools?\s*used\s*(?:included|were)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$)',
    "activity": r'^(?:(?:add|insert)\s+activit(?:y|ies)\s+|activit(?:y|ies)\s*[:,]?\s*|activit(?:y|ies)\s*(?:covered|included)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|issues?|time|weather|impression|comments)\s*:)|$)',
    "issue": r'^(?:(?:add|insert)\s+issues?\s+|issues?\s*[:,]?\s*|issues?\s*(?:encountered|included)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|time|weather|impression|comments)\s*:)|$)',
    "weather": r'^(?:(?:add|insert)\s+weathers?\s+|weathers?\s*[:,]?\s*|weather\s+was\s+|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|impression|comments)\s*:)|$)',
    "time": r'^(?:(?:add|insert)\s+times?\s+|times?\s*[:,]?\s*|time\s+spent\s+|morning\s+time\s*|afternoon\s+time\s*|evening\s+time\s*)(morning|afternoon|evening|full day)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|weather|impression|comments)\s*:)|$)',
    "comments": r'^(?:(?:add|insert)\s+comments?\s+|comments?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression)\s*:)|$)',
    "clear": r'^(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?)\s*[:,]?\s*none$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": r'^(?:delete|remove)\s+(sites?|segments?|categories?|companies?|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?)(?:\s+(.+))?$',
    "correct": r'^(?:correct|adjust|update)\s+(sites?|segments?|categories?|companies?|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?)\s+([^,\s]+(?:\s+[^,\s]+)*)\s+to\s+([^,]+?)(?=(?:\s*,\s*(?:sites?|segments?|categories?|companies?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?)\s*:)|$)'
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
            ("Date", report_data.get("date
