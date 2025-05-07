you deleted half the code. please use this one as base 
import os
import sys
import io
import json
import re
import requests
import logging
import signal
from datetime import datetime
from time import time
from typing import Dict, Any, List, Optional, Callable
from flask import Flask, request
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from difflib import SequenceMatcher
from collections import deque
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from decouple import config

# --- Configuration ---
CONFIG = {
    "SESSION_FILE": config("SESSION_FILE", default="/opt/render/project/src/session_data.json"),
    "PAUSE_THRESHOLD": config("PAUSE_THRESHOLD", default=300, cast=int),
    "MAX_HISTORY": config("MAX_HISTORY", default=10, cast=int),
    "OPENAI_MODEL": config("OPENAI_MODEL", default="gpt-3.5-turbo"),
    "OPENAI_TEMPERATURE": config("OPENAI_TEMPERATURE", default=0.2, cast=float),
}

REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"]

for var in REQUIRED_ENV_VARS:
    if not config(var, default=None):
        raise EnvironmentError(f"Missing required environment variable: {var}")

TELEGRAM_TOKEN = config("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = config("OPENAI_API_KEY")

# --- Logger Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ConstructionBot")

def log_event(event: str, **kwargs) -> None:
    logger.info({"event": event, **kwargs})

# --- Field Mapping ---
FIELD_MAPPING = {
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
    'comment': 'comments', 'comments': 'comments',
    'architect': 'roles', 'engineer': 'roles', 'supervisor': 'roles',
    'manager': 'roles', 'worker': 'roles', 'window installer': 'roles'
}

# --- Regex Patterns ---
categories = [
    "site", "segment", "category", "company", "companies", "person", "people",
    "role", "roles", "tool", "tools", "service", "services", "activity",
    "activities", "issue", "issues", "time", "weather", "impression", "comments",
    "architect", "engineer", "supervisor", "manager", "worker", "window installer"
]
list_categories = ["people", "company", "roles", "tools", "service", "activities", "issues"]

categories_pattern = '|'.join(re.escape(cat) for cat in categories)
list_categories_pattern = '|'.join(re.escape(cat) for cat in list_categories)

FIELD_PATTERNS = {
    "site_name": r'^(?:(?:add|insert)\s+sites?\s+|sites?\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "segment": r'^(?:(?:add|insert)\s+segments?\s+|segments?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "category": r'^(?:(?:add|insert)\s+categories?\s+|categories?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|segment|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "impression": r'^(?:(?:add|insert)\s+impressions?\s+|impressions?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|comments)\s*:)|$|\s*$)',
    "people": r'^(?:(?:add|insert)\s+(?:peoples?|persons?)\s+|(?:peoples?|persons?)\s*[:,]?\s*)([^,]+?)(?:\s+as\s+([^,]+?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "role": r'^(?:(?:add|insert)\s+|(?:peoples?|persons?)\s+)?(\w+\s+\w+|\w+)\s*[:,]?\s*as\s+([^,\s]+)(?:\s+to\s+(?:peoples?|persons?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)|^(?:persons?|peoples?)\s*[:,]?\s*(\w+\s+\w+|\w+)\s*,\s*roles?\s*[:,]?\s*([^,\s]+)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "supervisor": r'^(?:i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised|(?:add|insert)\s+roles?\s*[:,]?\s*supervisor\s*|roles?\s*[:,]?\s*supervisor\s*$)(?:\s+by\s+([^,]+?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "company": r'^(?:(?:add|insert)\s+compan(?:y|ies)\s+|compan(?:y|ies)\s*[:,]?\s*|(?:add|insert)\s+([^,]+?)\s+as\s+compan(?:y|ies)\s*)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "service": r'^(?:(?:add|insert)\s+services?\s+|services?\s*[:,]?\s*|services?\s*(?:were|provided)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "tool": r'^(?:(?:add|insert)\s+tools?\s+|tools?\s*[:,]?\s*|tools?\s*used\s*(?:included|were)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "activity": r'^(?:(?:add|insert)\s+activit(?:y|ies)\s+|activit(?:y|ies)\s*[:,]?\s*|activit(?:y|ies)\s*(?:covered|included)?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|issues?|time|weather|impression|comments)\s*:|\s+issues?\s*:|\s+times?\s*:|$|\s*$))',
    "issue": r'^(?:(?:add|insert)\s+issues?\s+|issues?\s*[:,]?\s*|issues?\s*(?:encountered|included)?\s*|problem\s*:?\s*|delay\s*:?\s*|injury\s*:?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|times?|weather|impression|comments)\s*:|\s+times?\s*:|$|\s*$))',
    "weather": r'^(?:(?:add|insert)\s+weathers?\s+|weathers?\s*[:,]?\s*|weather\s+was\s+|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|impression|comments)\s*:)|$|\s*$)',
    "time": r'^(?:(?:add|insert)\s+times?\s+|times?\s*[:,]?\s*|time\s+spent\s+|morning\s+time\s*|afternoon\s+time\s*|evening\s+time\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|weather|impression|comments)\s*:)|$|\s*$)',
    "comments": r'^(?:(?:add|insert)\s+comments?\s+|comments?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression)\s*:)|$|\s*$)',
    "clear": r'^(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?|site_name|segment|category|time|weather|impression)\s*[:,]?\s*none$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": rf'^(?:delete|remove)\s+({categories_pattern})\s*(.+)?$|^({categories_pattern})\s+(?:delete|remove)\s*(.+)?$',
    "delete_entire": rf'^delete\s+entire\s+category\s+({list_categories_pattern})$',
    "correct": r'^(?:correct|adjust|update|spell)(?:\s+spelling)?\s+((?:sites?|segments?|categories?|compan(?:y|ies)|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?))\s+([^,]+?)(?:\s+to\s+([^,]+?))?\s*(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)'
}

# --- Session Management ---
def load_session() -> Dict[str, Any]:
    try:
        if os.path.exists(CONFIG["SESSION_FILE"]):
            with open(CONFIG["SESSION_FILE"], "r") as f:
                data = json.load(f)
            for chat_id, session in data.items():
                if "command_history" in session:
                    session["command_history"] = deque(
                        session["command_history"], maxlen=CONFIG["MAX_HISTORY"]
                    )
            log_event("session_loaded", file=CONFIG["SESSION_FILE"])
            return data
        log_event("session_file_not_found", file=CONFIG["SESSION_FILE"])
        return {}
    except Exception as e:
        log_event("load_session_error", error=str(e))
        return {}

def save_session(session_data: Dict[str, Any]) -> None:
    try:
        serializable_data = {}
        for chat_id, session in session_data.items():
            serializable_session = session.copy()
            if "command_history" in serializable_session:
                serializable_session["command_history"] = list(
                    serializable_session["command_history"]
                )
            serializable_data[chat_id] = serializable_session
        os.makedirs(os.path.dirname(CONFIG["SESSION_FILE"]), exist_ok=True)
        with open(CONFIG["SESSION_FILE"], "w") as f:
            json.dump(serializable_data, f)
        log_event("session_saved", file=CONFIG["SESSION_FILE"])
    except Exception as e:
        log_event("save_session_error", error=str(e))

session_data = load_session()

def blank_report() -> Dict[str, Any]:
    return {
        "site_name": "", "segment": "", "category": "",
        "company": [], "people": [], "roles": [], "tools": [], "service": [],
        "activities": [], "issues": [], "time": "", "weather": "",
        "impression": "", "comments": "", "date": datetime.now().strftime("%d-%m-%Y")
    }

# --- OpenAI Initialization ---
client = OpenAI(api_key=OPENAI_API_KEY)

# --- GPT Prompt ---
GPT_PROMPT = """
You are an AI assistant extracting a construction site report from user input. Extract all explicitly mentioned fields and return them in JSON format. Process the entire input as a single unit, splitting on commas or periods only when fields are clearly separated by keywords. Map natural language phrases and standardized commands (add, insert, delete, correct, adjust, spell, remove) to fields accurately, prioritizing specific fields over comments or site_name. Do not treat reset commands ("new", "new report", "reset", "reset report", "/new") as comments or fields; return {} for these. Handle "none" inputs (e.g., "Tools: none") as clearing the respective field, and vague inputs (e.g., "Activities: many") by adding them and noting clarification needed.

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
- delete|remove <category> [value|from <category> <value>]: Remove a value or clear the category (e.g., "delete activities Laying foundation", "delete Jonas from people", or "delete companies").
- correct|adjust|spell <category> <old> to <new>|correct spelling <category> <value>|spell <category> <value>: Update a value or correct spelling (e.g., "correct site Downtown to Uptown", "spell companies Orient Corp").
- <category>: <value>: Add a value (e.g., "Services: abc" -> "service": [{"task": "abc"}]).
- <category>: none: Clear the category (e.g., "Tools: none" -> "tools": []).

Rules:
- Accept both singular and plural category names (e.g., "issue" or "issues", "company" or "companies").
- Extract fields from colon-separated inputs (e.g., "Services: abc"), natural language (e.g., "weather was cloudy" -> "weather": "cloudy"), or commands (e.g., "add people Anna").
- For segment and category: Extract only the value (e.g., "Segment: 5" -> "segment": "5").
- For issues: Recognize keywords: "Issue", "Issues", "Problem", "Delay", "Injury". "Issues: none" clears the issues list.
- For activities: Recognize keywords: "Activity", "Activities", "Task", "Progress", "Construction", or action-oriented phrases. "Activities: none" clears the activities list. Handle vague inputs like "Activities: many" by adding them and noting clarification needed.
- For site_name: Recognize location-like phrases following "at", "in", "on" (e.g., "Work was done at East Wing" -> "site_name": "East Wing", "activities": ["Work was done"]).
- For people and roles: Recognize "add [name] as [role]" (e.g., "add Anna as engineer" -> "people": ["Anna"], "roles": [{"name": "Anna", "role": "Engineer"}]). "Roles supervisor" assigns "Supervisor" to the user.
- For tools and service: Recognize "Tool: [item]", "Service: [task]", or commands like "add service abc".
- For companies: Recognize "add company <name>", "company: <name>", or "add <name> as company". Handle "delete company <name>" to remove the company. Handle "correct company <old> to <new>" to update the company name.
- Comments should only include non-field-specific notes.
- Return {} for reset commands or irrelevant inputs.
- Case-insensitive matching.
- Handle natural language inputs flexibly, allowing variations like "Activities: laying foundation", "Add issue power outage", "Delete Jonas from people", or "spell companies Orient Corp".
"""

# --- Signal Handlers ---
def handle_shutdown(signum: int, frame: Any) -> None:
    log_event("shutdown_signal", signal=signum)
    save_session(session_data)
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# --- Telegram API ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def send_message(chat_id: str, text: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        response.raise_for_status()
        log_event("message_sent", chat_id=chat_id, text=text[:50])
    except requests.RequestException as e:
        log_event("send_message_error", chat_id=chat_id, error=str(e))
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def get_telegram_file_path(file_id: str) -> str:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        response = requests.get(url)
        response.raise_for_status()
        file_path = response.json()["result"]["file_path"]
        log_event("get_telegram_file_path", file_id=file_id)
        return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    except requests.RequestException as e:
        log_event("get_telegram_file_path_error", file_id=file_id, error=str(e))
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def transcribe_voice(file_id: str) -> str:
    try:
        audio_url = get_telegram_file_path(file_id)
        audio_response = requests.get(audio_url)
        audio_response.raise_for_status()
        audio = audio_response.content
        log_event("audio_fetched", size_bytes=len(audio))
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio, "audio/ogg")
        )
        text = response.text.strip()
        if not text:
            log_event("transcription_empty")
            return ""
        log_event("transcription_success", text=text)
        return text
    except (requests.RequestException, Exception) as e:
        log_event("transcription_failed", error=str(e))
        return ""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def send_pdf(chat_id: str, pdf_buffer: io.BytesIO) -> bool:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        files = {'document': ('report.pdf', pdf_buffer, 'application/pdf')}
        data = {'chat_id': chat_id, 'caption': 'Here is your construction site report.'}
        response = requests.post(url, files=files, data=data)
        response.raise_for_status()
        log_event("pdf_sent", chat_id=chat_id)
        return True
    except requests.RequestException as e:
        log_event("send_pdf_error", chat_id=chat_id, error=str(e))
        return False

# --- Report Generation ---
def generate_pdf(report_data: Dict[str, Any]) -> Optional[io.BytesIO]:
    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        story = [Paragraph("Construction Site Report", styles['Title']), Spacer(1, 12)]

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
        log_event("pdf_generated", size_bytes=buffer.getbuffer().nbytes)
        return buffer
    except Exception as e:
        log_event("pdf_generation_error", error=str(e))
        return None

def summarize_report(data: Dict[str, Any]) -> str:
    try:
        roles_str = ", ".join(f"{r.get('name', '')} ({r.get('role', '')})" for r in data.get("roles", []) if r.get("role"))
        lines = [
            f"🏗️ **Site**: {data.get('site_name', '') or ''}",
            f"🛠️ **Segment**: {data.get('segment', '') or ''}",
            f"📋 **Category**: {data.get('category', '') or ''}",
            f"🏢 **Companies**: {', '.join(c.get('name', '') for c in data.get('company', []) if c.get('name')) or ''}",
            f"👷 **People**: {', '.join(data.get('people', []) or [])}",
            f"🎭 **Roles**: {roles_str}",
            f"🔧 **Services**: {', '.join(s.get('task', '') for s in data.get('service', []) if s.get('task')) or ''}",
            f"🛠️ **Tools**: {', '.join(t.get('item', '') for t in data.get('tools', []) if t.get('item')) or ''}",
            f"📅 **Activities**: {', '.join(data.get('activities', []) or [])}",
            "⚠️ **Issues**:"
        ]
        valid_issues = [i for i in data.get("issues", []) if isinstance(i, dict) and i.get("description", "").strip()]
        if valid_issues:
            for i in valid_issues:
                desc = i["description"]
                by = i.get("caused_by", "")
                photo = " 📸" if i.get("has_photo") else ""
                extra = f" (by {by})" if by else ""
                lines.append(f"  • {desc}{extra}{photo}")
        else:
            lines.append("")
        lines.extend([
            f"⏰ **Time**: {data.get('time', '') or ''}",
            f"🌦️ **Weather**: {data.get('weather', '') or ''}",
            f"😊 **Impression**: {data.get('impression', '') or ''}",
            f"💬 **Comments**: {data.get('comments', '') or ''}",
            f"📆 **Date**: {data.get('date', '') or ''}"
        ])
        summary = "\n".join(line for line in lines if line.strip())
        log_event("summarize_report", summary=summary)
        return summary
    except Exception as e:
        log_event("summarize_report_error", error=str(e))
        raise

# --- Data Processing ---
def clean_value(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return value
    cleaned = re.sub(r'^(?:add\s+|insert\s+|from\s+|correct\s+spelling\s+|spell\s+|delete\s+|remove\s+)', '', value.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.replace('tone', 'stone') if 'tone' in cleaned.lower() and field == 'activities' else cleaned
    log_event("cleaned_value", field=field, raw=value, cleaned=cleaned)
    return cleaned

def enrich_date(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        today = datetime.now().strftime("%d-%m-%Y")
        if not data.get("date"):
            data["date"] = today
        else:
            try:
                input_date = datetime.strptime(data["date"], "%d-%m-%Y")
                if input_date > datetime.now():
                    data["date"] = today
            except ValueError:
                data["date"] = today
        log_event("date_enriched", date=data["date"])
        return data
    except Exception as e:
        log_event("enrich_date_error", error=str(e))
        raise

# --- Field Extraction ---
def validate_patterns() -> None:
    try:
        for field, pattern in FIELD_PATTERNS.items():
            re.compile(pattern, re.IGNORECASE)
        log_event("patterns_validated")
    except Exception as e:
        log_event("pattern_validation_error", field=field, error=str(e))
        raise

validate_patterns()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def extract_fields(text: str) -> Dict[str, Any]:
    try:
        log_event("extract_fields", input=text)
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())

        reset_match = re.match(FIELD_PATTERNS["reset"], normalized_text, re.IGNORECASE)
        if reset_match:
            log_event("reset_detected")
            return {"reset": True}

        if normalized_text.lower() in ("undo", "/undo"):
            log_event("undo_detected")
            return {"undo": True}

        if normalized_text.lower() in ("status", "/status"):
            log_event("status_detected")
            return {"status": True}

        if normalized_text.lower() in ("export pdf", "/export pdf"):
            log_event("export_pdf_detected")
            return {"export_pdf": True}

        commands = [cmd.strip() for cmd in re.split(r',\s*(?=(?:[^:]*:)|(?:add|insert)\s+(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments))|(?<!\w)\.\s*(?=[A-Z])', text) if cmd.strip()]
        log_event("commands_split", commands=commands)
        processed_result = {
            "company": [], "roles": [], "tools": [], "service": [],
            "activities": [], "issues": [], "people": []
        }
        seen_fields = set()

        for cmd in commands:
            delete_match = re.match(FIELD_PATTERNS["delete"], cmd, re.IGNORECASE)
            if delete_match:
                raw_field = delete_match.group(1) if delete_match.group(1) else delete_match.group(2)
                value = delete_match.group(3) if delete_match.group(3) else delete_match.group(4)
                raw_field = raw_field.lower() if raw_field else None
                value = value.strip() if value else None
                field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
                log_event("delete_command", field=field, value=value)
                if field:
                    result.setdefault("delete", []).append({"field": field, "value": value})
                continue

            correct_match = re.match(FIELD_PATTERNS["correct"], cmd, re.IGNORECASE)
            if correct_match:
                raw_field = correct_match.group(1).lower() if correct_match.group(1) else None
                old_value = correct_match.group(2).strip() if correct_match.group(2) else None
                new_value = correct_match.group(3).strip() if correct_match.group(3) else None
                field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
                log_event("correct_command", field=field, old=old_value, new=new_value)
                if field and old_value:
                    if new_value:
                        result.setdefault("correct", []).append({"field": field, "old": clean_value(old_value, field), "new": clean_value(new_value, field)})
                    else:
                        result["correct_prompt"] = {"field": field, "value": clean_value(old_value, field)}
                continue

            cmd_result = extract_single_command(cmd)
            if cmd_result.get("reset"):
                return {"reset": True}
            for key, value in cmd_result.items():
                if key in seen_fields and key not in ["people", "company", "roles", "tools", "service", "activities", "issues"]:
                    continue
                seen_fields.add(key)
                if key in processed_result:
                    if isinstance(value, list):
                        processed_result[key].extend(value)
                    else:
                        processed_result[key].append(value)
                else:
                    result[key] = value

        for field in processed_result:
            if processed_result[field]:
                existing_items = (
                    [item["name"] for item in result.get(field, []) if isinstance(item, dict) and "name" in item] if field == "company" else
                    [item["description"] for item in result.get(field, []) if isinstance(item, dict) and "description" in item] if field == "issues" else
                    [item["task"] for item in result.get(field, []) if isinstance(item, dict) and "task" in item] if field == "service" else
                    [item["item"] for item in result.get(field, []) if isinstance(item, dict) and "item" in item] if field == "tools" else
                    [f"{item['name']} ({item['role']})" for item in result.get(field, []) if isinstance(item, dict) and "name" in item and "role" in item] if field == "roles" else
                    result.get(field, []) if field in ["people", "activities"] else
                    []
                )
                result[field] = processed_result[field] + (
                    [{"name": i} for i in existing_items if isinstance(i, str)] if field == "company" else
                    [{"description": i} for i in existing_items if isinstance(i, str)] if field == "issues" else
                    [{"task": i} for i in existing_items if isinstance(i, str)] if field == "service" else
                    [{"item": i} for i in existing_items if isinstance(i, str)] if field == "tools" else
                    [{"name": i.split(' (')[0], "role": i.split(' (')[1].rstrip(')')} for i in existing_items if isinstance(i, str) and ' (' in i] if field == "roles" else
                    existing_items if field in ["people", "activities"] else []
                )

        log_event("fields_extracted", result=result)
        return result
    except Exception as e:
        log_event("extract_fields_error", input=text, error=str(e))
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def extract_single_command(text: str) -> Dict[str, Any]:
    try:
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())
        log_event("extract_single_command", input=normalized_text)

        # Handle deletion commands
        delete_match = re.match(FIELD_PATTERNS["delete"], normalized_text, re.IGNORECASE)
        if delete_match:
            field = delete_match.group(1) if delete_match.group(1) else delete_match.group(2)
            value = delete_match.group(3) if delete_match.group(3) else delete_match.group(4)
            field = field.lower() if field else ""
            value = value.strip() if value else None
            mapped_field = FIELD_MAPPING.get(field, field)
            log_event("delete_command", raw_field=field, mapped_field=mapped_field, value=value)

            if field in ["architect", "engineer", "supervisor", "manager", "worker", "window installer"]:
                result.setdefault("delete", []).append({"field": "roles", "value": field})
                log_event("delete_role_command", field="roles", value=field)
            elif mapped_field == "people":
                result.setdefault(“delete”, []).append({“field”: mapped_field, “value”: value}) if value else {"delete": True}
                log_event("delete_people_command", field=mapped_field, value=value)
            elif mapped_field == "person":
                result["people"] = {"delete": value}
                log_event("delete_person_command", field="people", value=value)
            elif mapped_field in ["company", "roles", "tools", "service", "activities", "issues"]:
                result.setdefault(“delete”, []).append({“field”: mapped_field, “value”: value}) if value else {"delete": True}
                log_event("delete_list_command", field=mapped_field, value=value)
            elif mapped_field in ["site_name", "segment", "category", "time", "weather", "impression", "comments"]:
                result.setdefault(“delete”, []).append({“field”: mapped_field, “value”: value}) if value else {"delete": True}
                log_event("delete_scalar_command", field=mapped_field, value=value)
            else:
                log_event("unrecognized_delete_field", field=field)
                return {}
            return result

        # Handle entire category deletion
        delete_entire_match = re.match(FIELD_PATTERNS["delete_entire"], normalized_text, re.IGNORECASE)
        if delete_entire_match:
            field = delete_entire_match.group(1).lower()
            mapped_field = FIELD_MAPPING.get(field, field)
            result[mapped_field] = {"delete": True}
            log_event("delete_entire_category", field=mapped_field)
            return result

        # Handle correction commands
        correct_match = re.match(FIELD_PATTERNS["correct"], normalized_text, re.IGNORECASE)
        if correct_match:
            raw_field = correct_match.group(1).lower() if correct_match.group(1) else None
            old_value = correct_match.group(2).strip() if correct_match.group(2) else None
            new_value = correct_match.group(3).strip() if correct_match.group(3) else None
            field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
            log_event("correct_command", field=field, old=old_value, new=new_value)
            if field and old_value:
                if new_value:
                    result.setdefault("correct", []).append({"field": field, "old": clean_value(old_value, field), "new": clean_value(new_value, field)})
                else:
                    result["correct_prompt"] = {"field": field, "value": clean_value(old_value, field)}
            return result

        # Handle other field extractions
        for raw_field, pattern in FIELD_PATTERNS.items():
            if raw_field in ["reset", "delete", "correct", "clear"]:
                continue
            match = re.match(pattern, normalized_text, re.IGNORECASE)
            if match:
                field = FIELD_MAPPING.get(raw_field, raw_field)
                log_event("field_matched", raw_field=raw_field, mapped_field=field, input=normalized_text)
                if field == "site_name" and re.search(r'\b(add|insert|delete|remove|correct|adjust|update|spell|none|as|role|new|reset)\b', normalized_text.lower()):
                    log_event("skipped_site_name", reason="command-like input")
                    continue
                if field == "people":
                    name = clean_value(match.group(1), field)
                    role = clean_value(match.group(2), field) if match.group(2) else None
                    if name.lower() == "supervisor":
                        log_event("skipped_people_supervisor", reason="supervisor is a role")
                        continue
                    result["people"] = [name]
                    if role:
                        result["roles"] = [{"name": name, "role": role.title()}]
                        log_event("extracted_field", field="roles", name=name, role=role)
                    log_event("extracted_field", field="people", value=name)
                elif field == "role":
                    name = clean_value(match.group(1) or match.group(3), field)
                    role = (match.group(2) or match.group(4)).title()
                    if name.lower() == "supervisor":
                        log_event("skipped_role_supervisor", reason="supervisor is a role")
                        continue
                    result["people"] = [name.strip()]
                    result["roles"] = [{"name": name.strip(), "role": role}]
                    log_event("extracted_field", field="roles", name=name, role=role)
                elif field == "supervisor":
                    name = clean_value(match.group(1), field) if match.group(1) else "User"
                    result["people"] = [name]
                    result["roles"] = [{"name": name, "role": "Supervisor"}]
                    log_event("extracted_field", field="roles", value=name)
                elif field == "company":
                    name = clean_value(match.group(2) if match.group(2) else match.group(1), field)
                    if re.match(r'^(?:delete|remove|add|insert|correct|adjust|update|spell)\b', name.lower()):
                        log_event("skipped_company", reason="command-like name", value=name)
                        continue
                    result["company"] = [{"name": name}]
                    log_event("extracted_field", field="company", value=name)
                elif field == "clear":
                    field_name = FIELD_MAPPING.get(match.group(1).lower(), match.group(1).lower())
                    result[field_name] = [] if field_name in ["issues", "activities", "tools", "service", "company", "people", "roles"] else ""
                    log_event("extracted_field", field=field_name, value="none")
                elif field in ["service"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"task": value.strip()}]
                    log_event("extracted_field", field=field, value=value)
                elif field in ["tool"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"item": value.strip()}]
                    log_event("extracted_field", field=field, value=value)
                elif field == "issue":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"description": value.strip()}]
                    log_event("extracted_field", field=field, value=value)
                elif field == "activity":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [value.strip()]
                    log_event("extracted_field", field=field, value=value)
                else:
                    value = clean_value(match.group(1), field)
                    result[field] = value
                    log_event("extracted_field", field=field, value=value)
                return result

        # Fallback to GPT for complex inputs
        messages = [
            {"role": "system", "content": "Extract explicitly stated fields from construction site report input. Return JSON with extracted fields."},
            {"role": "user", "content": GPT_PROMPT + "\nInput text: " + normalized_text}
        ]
        try:
            response = client.chat.completions.create(
                model=CONFIG["OPENAI_MODEL"], messages=messages, temperature=CONFIG["OPENAI_TEMPERATURE"]
            )
            raw_response = response.choices[0].message.content
            log_event("gpt_response", raw_response=raw_response)
            data = json.loads(raw_response)
            log_event("gpt_extracted", data=data)
            for field in ["category", "segment", "site_name", "time", "weather", "impression", "comments"]:
                if field in data and isinstance(data[field], str):
                    data[field] = clean_value(data[field], field)
            for field in ["tools", "service", "issues", "company", "roles"]:
                if field in data:
                    for item in data[field]:
                        if isinstance(item, dict):
                            if field == "tools" and "item" in item:
                                item["item"] = clean_value(item["item"], field)
                            elif field == "service" and "task" in item:
                                item["task"] = clean_value(item["task"], field)
                            elif field == "issues" and "description" in item:
                                item["description"] = clean_value(item["description"], field)
                            elif field == "company" and "name" in item:
                                item["name"] = clean_value(item["name"], field)
                            elif field == "roles" and "name" in item:
                                item["name"] = clean_value(item["name"], field)
                                item["role"] = clean_value(item["role"], field) if item.get("role") else item["role"]
            if "activities" in data:
                data["activities"] = [clean_value(item, "activities") for item in data["activities"] if isinstance(item, str)]
            if "roles" in data:
                for role in data["roles"]:
                    if isinstance(role, dict) and "name" in role and role["name"] not in data.get("people", []):
                        data.setdefault("people", []).append(clean_value(role["name"], "people"))
            if not data and normalized_text.strip():
                issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error|injury)\b'
                activity_keywords = r'\b(work\s+was\s+done|activity|activities|task|progress|construction|building|laying|setting|wiring|installation|scaffolding)\b'
                location_keywords = r'\b(at|in|on)\b'
                if re.search(issue_keywords, normalized_text.lower()):
                    cleaned_text = clean_value(normalized_text.strip(), "issues")
                    data = {"issues": [{"description": cleaned_text}]}
                    log_event("fallback_issue", data=data)
                elif re.search(activity_keywords, normalized_text.lower()) and re.search(location_keywords, normalized_text.lower()):
                    parts = re.split(r'\b(at|in|on)\b', normalized_text, flags=re.IGNORECASE)
                    location = ", ".join(clean_value(part.strip().title(), "site_name") for part in parts[2::2] if part.strip())
                    activity = clean_value(parts[0].strip(), "activities")
                    data = {"site_name": location, "activities": [activity]}
                    log_event("fallback_activity_site", data=data)
                else:
                    data = {"comments": clean_value(normalized_text.strip(), "comments")}
                    log_event("fallback_comments", data=data)
            return data
        except (json.JSONDecodeError, Exception) as e:
            log_event("gpt_extract_error", input=normalized_text, error=str(e))
            if normalized_text.strip():
                issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error|injury)\b'
                if re.search(issue_keywords, normalized_text.lower()):
                    cleaned_text = clean_value(normalized_text.strip(), "issues")
                    data = {"issues": [{"description": cleaned_text}]}
                    log_event("fallback_issue_error", data=data)
                    return data
                log_event("fallback_comments_error", input=normalized_text)
                return {"comments": clean_value(normalized_text.strip(), "comments")}
            return {}
    except Exception as e:
        log_event("extract_single_command_error", input=text, error=str(e))
        raise

def string_similarity(a: str, b: str) -> float:
    try:
        similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
        log_event("string_similarity", a=a, b=b, similarity=similarity)
        return similarity
    except Exception as e:
        log_event("string_similarity_error", error=str(e))
        raise

def merge_data(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    try:
        merged = existing.copy()
        for key, value in new.items():
            if key in ["reset", "undo", "status", "export_pdf", "correct_prompt", "delete", "correct"]:
                continue
            if key in ["company", "roles", "tools", "service", "issues"]:
                if value == []:
                    merged[key] = []
                    log_event("cleared_list", field=key)
                    continue
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else [value]
                for new_item in new_items:
                    if isinstance(new_item, str):
                        # Convert string issues to dictionary format
                        if key == "issues":
                            new_item = {"description": new_item}
                        else:
                            continue  # Skip non-dict items for other fields
                    if not isinstance(new_item, dict):
                        continue
                    if key == "company" and "name" in new_item:
                        new_name = new_item.get("name", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("name", ""), new_name) > 0.6:
                                existing_list[i] = new_item
                                replaced = True
                                log_event("replaced_company", old=existing_item.get("name"), new=new_name)
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            log_event("added_company", name=new_name)
                    elif key == "roles" and "name" in new_item:
                        new_name = new_item.get("name", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("name", ""), new_name) > 0.6:
                                existing_list[i] = new_item
                                replaced = True
                                log_event("replaced_role", name=new_name)
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            log_event("added_role", name=new_name)
                    elif key == "issues" and "description" in new_item:
                        new_desc = new_item.get("description", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("description", ""), new_desc) > 0.6:
                                existing_list[i] = new_item
                                replaced = True
                                log_event("replaced_issue", old=existing_item.get("description"), new=new_desc)
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            log_event("added_issue", description=new_desc)
                    elif key == "tools" and "item" in new_item:
                        new_item_name = new_item.get("item", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("item", ""), new_item_name) > 0.6:
                                existing_list[i] = new_item
                                replaced = True
                                log_event("replaced_tool", old=existing_item.get("item"), new=new_item_name)
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            log_event("added_tool", item=new_item_name)
                    elif key == "service" and "task" in new_item:
                        new_task = new_item.get("task", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("task", ""), new_task) > 0.6:
                                existing_list[i] = new_item
                                replaced = True
                                log_event("replaced_service", old=existing_item.get("task"), new=new_task)
                                break
                        if not replaced:
                            existing_list.append(new_item)
                            log_event("added_service", task=new_task)
                merged[key] = existing_list
            elif key in ["activities", "people"]:
                if value == []:
                    merged[key] = []
                    log_event("cleared_list", field=key)
                    continue
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else [value]
                for item in new_items:
                    if isinstance(item, str) and item not in existing_list and item.lower() != "supervisor":
                        existing_list.append(item)
                        log_event(f"added_{key}", value=item)
                merged[key] = existing_list
            else:
                if value == "" and key in ["comments", "site_name", "segment", "category", "time", "weather", "impression"]:
                    merged[key] = ""
                    log_event("cleared_field", field=key)
                elif value:
                    merged[key] = value
                    log_event("updated_field", field=key, value=value)
        log_event("data_merged", merged=json.dumps(merged, indent=2))
        return merged
    except Exception as e:
        log_event("merge_data_error", error=str(e))
        raise

def delete_entry(data: Dict[str, Any], field: str, value: Optional[str] = None) -> Dict[str, Any]:
    try:
        log_event("delete_entry", field=field, value=value)
        if field in ["company", "tools", "service", "issues"]:
            field_key = "name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description"
            if value:
                data[field] = [item for item in data[field] if not (isinstance(item, dict) and string_similarity(item.get(field_key, ""), value) > 0.7)]
                log_event(f"{field}_deleted", value=value)
            else:
                data[field] = []
                log_event(f"{field}_cleared")
        elif field == "roles":
            if value:
                data[field] = [item for item in data[field] if not (isinstance(item, dict) and string_similarity(item.get("name", ""), value) > 0.7)]
                data["people"] = [p for p in data.get("people", []) if any(item.get("name", "") == p for item in data[field])]
                log_event(f"{field}_deleted", value=value)
            else:
                data[field] = []
                data["people"] = []
                log_event(f"{field}_cleared")
        elif field == "people":
            if value:
                data[field] = [item for item in data[field] if string_similarity(item, value) <= 0.7]
                data["roles"] = [role for role in data.get("roles", []) if string_similarity(role.get("name", ""), value) <= 0.7]
                log_event("people_deleted", value=value)
            else:
                data[field] = []
                data["roles"] = []
                log_event("people_cleared")
        elif field == "activities":
            if value:
                data[field] = [item for item in data[field] if string_similarity(item, value) <= 0.7]
                log_event("activities_deleted", value=value)
            else:
                data[field] = []
                log_event("activities_cleared")
        elif field in ["site_name", "segment", "category", "time", "weather", "impression", "comments"]:
            if value:
                if string_similarity(data.get(field, ""), value) > 0.7:
                    data[field] = ""
                    log_event(f"{field}_cleared")
            else:
                data[field] = ""
                log_event(f"{field}_cleared")
        log_event("data_after_deletion", data=json.dumps(data, indent=2))
        return data
    except Exception as e:
        log_event("delete_entry_error", field=field, error=str(e))
        raise

# --- Command Handlers ---
COMMAND_HANDLERS: Dict[str, Callable[[str, Dict[str, Any]], None]] = {}

def command(name: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        COMMAND_HANDLERS[name] = func
        return func
    return decorator

@command("reset")
def handle_reset(chat_id: str, session: Dict[str, Any]) -> None:
    session["structured_data"] = blank_report()
    session["command_history"].clear()
    save_session(session_data)
    summary = summarize_report(session["structured_data"])
    send_message(chat_id, f"**Report reset**\n\n{summary}")

@command("undo")
def handle_undo(chat_id: str, session: Dict[str, Any]) -> None:
    if session["command_history"]:
        session["structured_data"] = session["command_history"].pop()
        save_session(session_data)
        summary = summarize_report(session["structured_data"])
        send_message(chat_id, f"**Undo successful**\n\n{summary}")
    else:
        send_message(chat_id, "Nothing to undo.")

@command("status")
def handle_status(chat_id: str, session: Dict[str, Any]) -> None:
    summary = summarize_report(session["structured_data"])
    send_message(chat_id, f"**Current report status**\n\n{summary}")

@command("export")
def handle_export(chat_id: str, session: Dict[str, Any]) -> None:
    pdf_buffer = generate_pdf(session["structured_data"])
    if pdf_buffer:
        if send_pdf(chat_id, pdf_buffer):
            send_message(chat_id, "PDF report sent successfully!")
        else:
            send_message(chat_id, "⚠️ Failed to send PDF report.")
    else:
        send_message(chat_id, "⚠️ Failed to generate PDF report.")

# --- Flask App ---
app = Flask(__name__)

def handle_command(chat_id: str, text: str, sess: Dict[str, Any]) -> tuple[str, int]:
    try:
        normalized_text = text.strip().lower() if text else ""
        if not normalized_text:
            send_message(chat_id, "⚠️ Empty input. Please provide a command (e.g., 'add site Downtown Project').")
            return "ok", 200

        current_time = time()
        if (current_time - sess.get("last_interaction", 0) > CONFIG["PAUSE_THRESHOLD"] and
                normalized_text not in ("yes", "no", "new", "new report", "reset", "reset report", "/new", "existing", "continue")):
            sess["pending_input"] = text
            sess["awaiting_reset_confirmation"] = True
            sess["last_interaction"] = current_time
            save_session(session_data)
            send_message(chat_id, "It’s been a while! Reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        sess["last_interaction"] = current_time

        if normalized_text in COMMAND_HANDLERS:
            COMMAND_HANDLERS[normalized_text](chat_id, sess)
            return "ok", 200

        if normalized_text in ("new", "new report", "reset", "reset report", "/new"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session(session_data)
            send_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        clear_match = re.match(FIELD_PATTERNS["clear"], text, re.IGNORECASE)
        if clear_match:
            raw_field = clear_match.group(1).lower() if clear_match.group(1) else None
            field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
            if not field:
                log_event("clear_command_error", text=text, error="Invalid field")
                send_message(chat_id, f"⚠️ Invalid clear command: '{text}'. Try 'tools: none' or 'issues: none'.")
                return "ok", 200
            sess["command_history"].append(sess["structured_data"].copy())
            sess["structured_data"] = delete_entry(sess["structured_data"], field)
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Cleared {field}\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        extracted = extract_fields(text)
        if extracted.get("reset"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session(session_data)
            send_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200
        if extracted.get("correct_prompt"):
            field = extracted["correct_prompt"]["field"]
            value = extracted["correct_prompt"]["value"]
            sess["awaiting_spelling_correction"] = (field, value)
            save_session(session_data)
            send_message(chat_id, f"Please provide the correct spelling for '{value}' in {field}.")
            return "ok", 200
        if extracted.get("delete"):
            sess["command_history"].append(sess["structured_data"].copy())
            for delete_cmd in extracted["delete"]:
                field = delete_cmd["field"]
                value = delete_cmd["value"]
                sess["structured_data"] = delete_entry(sess["structured_data"], field, value)
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Removed {field}" + (f": {value}" if value else "") + f"\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200
        if extracted.get("correct"):
            sess["command_history"].append(sess["structured_data"].copy())
            for correct_cmd in extracted["correct"]:
                field = correct_cmd["field"]
                old_value = correct_cmd["old"]
                new_value = correct_cmd["new"]
                if field in ["company", "roles", "tools", "service", "issues"]:
                    data_field = (
                        "name" if field == "company" else
                        "description" if field == "issues" else
                        "item" if field == "tools" else
                        "task" if field == "service" else
                        "name" if field == "roles" else None
                    )
                    sess["structured_data"][field] = [
                        {data_field: new_value if string_similarity(item.get(data_field, ""), old_value) > 0.7 else item[data_field],
                         **({} if field != "roles" else {"role": item["role"]})}
                        for item in sess["structured_data"].get(field, [])
                        if isinstance(item, dict)
                    ]
                    if field == "roles" and new_value not in sess["structured_data"].get("people", []):
                        sess["structured_data"]["people"].append(new_value)
                elif field in ["people"]:
                    sess["structured_data"]["people"] = [new_value if string_similarity(item, old_value) > 0.7 else item for item in sess["structured_data"].get("people", [])]
                    sess["structured_data"]["roles"] = [
                        {"name": new_value, "role": role["role"]} if string_similarity(role.get("name", ""), old_value) > 0.7 else role
                        for role in sess["structured_data"].get("roles", [])
                    ]
                elif field in ["activities"]:
                    sess["structured_data"]["activities"] = [new_value if string_similarity(item, old_value) > 0.7 else item for item in sess["structured_data"].get("activities", [])]
                else:
                    sess["structured_data"][field] = new_value
                log_event(f"{field}_corrected", old=old_value, new=new_value)
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Corrected {field} from '{old_value}' to '{new_value}'.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200
        if not any(k in extracted for k in ["company", "people", "roles", "tools", "service", "activities", "issues", "time", "weather", "impression", "comments", "segment", "category", "site_name"]):
            log_event("unrecognized_input", input=text)
            send_message(chat_id, f"⚠️ Unrecognized input: '{text}'. Try 'add site Downtown Project', 'add issue power outage', or 'spell companies Orient Corp'.")
            return "ok", 200

        sess["command_history"].append(sess["structured_data"].copy())
        sess["structured_data"] = merge_data(sess["structured_data"], enrich_date(extracted))
        save_session(session_data)
        tpl = summarize_report(sess["structured_data"])
        send_message(chat_id, f"✅ Updated report:\n\n{tpl}\n\nAnything else to add or correct?")
        return "ok", 200
    except Exception as e:
        log_event("handle_command_error", error=str(e))
        send_message(chat_id, "⚠️ An error occurred. Please try again.")
        return "error", 500

@app.route("/webhook", methods=["POST"])
def webhook() -> tuple[str, int]:
    try:
        data = request.get_json(force=True)
        log_event("webhook_received", data=data)
        if not data or "message" not in data:
            log_event("no_message")
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        log_event("message_received", chat_id=chat_id, text=text)

        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False,
                "last_interaction": time(),
                "pending_input": None,
                "awaiting_reset_confirmation": False,
                "command_history": deque(maxlen=CONFIG["MAX_HISTORY"]),
                "awaiting_spelling_correction": None
            }
            log_event("session_created", chat_id=chat_id)

        sess = session_data[chat_id]

        if "Supervisor" in sess["structured_data"].get("people", []):
            sess["structured_data"]["people"] = [p for p in sess["structured_data"].get("people", []) if p != "Supervisor"]
            sess["structured_data"]["roles"] = [r for r in sess["structured_data"].get("roles", []) if r.get("name") != "Supervisor"]
            log_event("cleaned_supervisor_entries", chat_id=chat_id)

        if "voice" in msg:
            text = transcribe_voice(msg["voice"]["file_id"])
            if not text:
                send_message(chat_id, "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'add site Downtown Project').")
                return "ok", 200
            log_event("transcribed_voice", text=text)

        if sess.get("awaiting_reset_confirmation", False):
            normalized_text = re.sub(r'[.!?]\s*$', '', text.strip()).lower()
            log_event("reset_confirmation", text=normalized_text, pending_input=sess["pending_input"])
            if normalized_text in ("yes", "new", "new report"):
                sess["structured_data"] = blank_report()
                sess["awaiting_correction"] = False
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["command_history"].clear()
                save_session(session_data)
                tpl = summarize_report(sess["structured_data"])
                send_message(chat_id, f"**Starting a fresh report**\n\n{tpl}\n\nSpeak or type your first field (e.g., 'add site Downtown Project').")
                return "ok", 200
            elif normalized_text in ("no", "existing", "continue"):
                text = sess["pending_input"]
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["last_interaction"] = time()
            else:
                send_message(chat_id, "Please clarify: Reset the report? Reply 'yes' or 'no'.")
                return "ok", 200

        if sess.get("awaiting_spelling_correction"):
            field, old_value = sess["awaiting_spelling_correction"]
            new_value = text.strip()
            log_event("spelling_correction_response", field=field, old_value=old_value, new_value=new_value)
            if new_value.lower() == old_value.lower():
                sess["awaiting_spelling_correction"] = None
                save_session(session_data)
                send_message(chat_id, f"⚠️ New value '{new_value}' is the same as the old value '{old_value}'. Please provide a different spelling for '{old_value}' in {field}.")
                return "ok", 200
            sess["awaiting_spelling_correction"] = None
            sess["command_history"].append(sess["structured_data"].copy())
            if field in ["company", "roles", "tools", "service", "issues"]:
                data_field = (
                    "name" if field == "company" else
                    "description" if field == "issues" else
                    "item" if field == "tools" else
                    "task" if field == "service" else
                    "name" if field == "roles" else None
                )
                sess["structured_data"][field] = [
                    {data_field: new_value if string_similarity(item.get(data_field, ""), old_value) > 0.7 else item[data_field],
                     **({} if field != "roles" else {"role": item["role"]})}
                    for item in sess["structured_data"].get(field, [])
                    if isinstance(item, dict)
                ]
                if field == "roles" and new_value not in sess["structured_data"].get("people", []):
                    sess["structured_data"]["people"].append(new_value)
            elif field in ["people"]:
                sess["structured_data"]["people"] = [new_value if string_similarity(item, old_value) > 0.7 else item for item in sess["structured_data"].get("people", [])]
                sess["structured_data"]["roles"] = [
                    {"name": new_value, "role": role["role"]} if string_similarity(role.get("name", ""), old_value) > 0.7 else role
                    for role in sess["structured_data"].get("roles", [])
                ]
            elif field in ["activities"]:
                sess["structured_data"]["activities"] = [new_value if string_similarity(item, old_value) > 0.7 else item for item in sess["structured_data"].get("activities", [])]
            else:
                sess["structured_data"][field] = new_value
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Corrected {field} from '{old_value}' to '{new_value}'.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        return handle_command(chat_id, text, sess)
    except Exception as e:
        log_event("webhook_error", error=str(e))
        return "error", 500

