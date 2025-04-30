import os
import sys
import io
import json
import re
import requests
import logging
import signal
from datetime import datetime
from typing import Dict, Any, List, Optional
from flask import Flask, request
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from difflib import SequenceMatcher
from time import time
from collections import deque
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# --- Configuration ---
CONFIG = {
    "SESSION_FILE": "/opt/render/project/src/session_data.json",
    "PAUSE_THRESHOLD": 300,  # 5 minutes in seconds
    "MAX_HISTORY": 10,  # Max commands for undo
    "OPENAI_MODEL": "gpt-3.5-turbo",
    "OPENAI_TEMPERATURE": 0.2,
}
REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"]

# --- Logger Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ConstructionBot")

# --- Environment Validation ---
def validate_environment() -> None:
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        logger.error({"event": "missing_env_vars", "variables": missing})
        raise EnvironmentError(f"Missing required env variables: {', '.join(missing)}")
    logger.info({"event": "env_vars_validated"})

validate_environment()

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
    'comment': 'comments', 'comments': 'comments'
}

# --- Session Management ---
def load_session() -> Dict[str, Any]:
    try:
        if os.path.exists(CONFIG["SESSION_FILE"]):
            with open(CONFIG["SESSION_FILE"], "r") as f:
                data = json.load(f)
            for chat_id, session in data.items():
                if "command_history" in session:
                    session["command_history"] = deque(session["command_history"], maxlen=CONFIG["MAX_HISTORY"])
            logger.info({"event": "session_loaded", "file": CONFIG["SESSION_FILE"]})
            return data
        logger.info({"event": "session_file_not_found", "file": CONFIG["SESSION_FILE"]})
        return {}
    except Exception as e:
        logger.error({"event": "load_session_error", "error": str(e)})
        return {}

def save_session(session_data: Dict[str, Any]) -> None:
    try:
        serializable_data = {}
        for chat_id, session in session_data.items():
            serializable_session = session.copy()
            if "command_history" in serializable_session:
                serializable_session["command_history"] = list(serializable_session["command_history"])
            serializable_data[chat_id] = serializable_session
        os.makedirs(os.path.dirname(CONFIG["SESSION_FILE"]), exist_ok=True)
        with open(CONFIG["SESSION_FILE"], "w") as f:
            json.dump(serializable_data, f)
        logger.info({"event": "session_saved", "file": CONFIG["SESSION_FILE"]})
    except Exception as e:
        logger.error({"event": "save_session_error", "error": str(e)})

session_data = load_session()

def blank_report() -> Dict[str, Any]:
    return {
        "site_name": "", "segment": "", "category": "",
        "company": [], "people": [], "roles": [], "tools": [], "service": [],
        "activities": [], "issues": [], "time": "", "weather": "",
        "impression": "", "comments": "", "date": datetime.now().strftime("%d-%m-%Y")
    }

# --- OpenAI Initialization ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- GPT Prompt ---
GPT_PROMPT = """
You are an AI assistant extracting a construction site report from user input. Extract all explicitly mentioned fields and return them in JSON format. Process the entire input as a single unit, splitting on commas or periods only when fields are clearly separated by keywords. Map natural language phrases and standardized commands (add, insert, delete, correct, adjust, spell) to fields accurately, prioritizing specific fields over comments or site_name. Do not treat reset commands ("new", "new report", "reset", "reset report", "/new") as comments or fields; return {} for these. Handle "none" inputs (e.g., "Tools: none") as clearing the respective field, and vague inputs (e.g., "Activities: many") by adding them and noting clarification needed.

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
    logger.info({"event": "shutdown_signal", "signal": signum})
    save_session(session_data)
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# --- Telegram API ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_message(chat_id: str, text: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        response.raise_for_status()
        logger.info({"event": "send_message", "chat_id": chat_id, "text": text[:50]})
    except requests.RequestException as e:
        logger.error({"event": "send_message_error", "chat_id": chat_id, "error": str(e)})
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id: str) -> str:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        response = requests.get(url)
        response.raise_for_status()
        file_path = response.json()["result"]["file_path"]
        logger.info({"event": "get_telegram_file_path", "file_id": file_id})
        return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    except requests.RequestException as e:
        logger.error({"event": "get_telegram_file_path_error", "file_id": file_id, "error": str(e)})
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_voice(file_id: str) -> str:
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
            logger.warning({"event": "transcription_empty"})
            return ""
        logger.info({"event": "transcription_success", "text": text})
        return text
    except (requests.RequestException, Exception) as e:
        logger.error({"event": "transcription_failed", "error": str(e)})
        return ""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_pdf(chat_id: str, pdf_buffer: io.BytesIO) -> bool:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        files = {'document': ('report.pdf', pdf_buffer, 'application/pdf')}
        data = {'chat_id': chat_id, 'caption': 'Here is your construction site report.'}
        response = requests.post(url, files=files, data=data)
        response.raise_for_status()
        logger.info({"event": "pdf_sent", "chat_id": chat_id})
        return True
    except requests.RequestException as e:
        logger.error({"event": "pdf_send_error", "chat_id": chat_id, "error": str(e)})
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
        logger.info({"event": "pdf_generated", "size_bytes": buffer.getbuffer().nbytes})
        return buffer
    except Exception as e:
        logger.error({"event": "pdf_generation_error", "error": str(e)})
        return None

# --- Data Processing ---
def clean_value(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return value
    cleaned = re.sub(r'^(?:add\s+|insert\s+|from\s+|correct\s+spelling\s+|spell\s+)', '', value.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.replace('tone', 'stone') if 'tone' in cleaned.lower() and field == 'activities' else cleaned
    logger.info({"event": "cleaned_value", "field": field, "raw": value, "cleaned": cleaned})
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
        logger.info({"event": "date_enriched", "date": data["date"]})
        return data
    except Exception as e:
        logger.error({"event": "enrich_date_error", "error": str(e)})
        raise

def summarize_report(data: Dict[str, Any]) -> str:
    try:
        lines = [
            f"🏗️ **Site**: {data.get('site_name', '') or ''}",
            f"🛠️ **Segment**: {data.get('segment', '') or ''}",
            f"📋 **Category**: {data.get('category', '') or ''}",
            f"🏢 **Companies**: {', '.join(c.get('name', '') for c in data.get('company', []) if c.get('name')) or ''}",
            f"👷 **People**: {', '.join(data.get('people', []) or [])}",
            f"🎭 **Roles**: {', '.join(f'{r.get('name', '')} ({r.get('role', '')})' for r in data.get('roles', []) if r.get('role')) or ''}",
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
        logger.info({"event": "summarize_report", "summary": summary})
        return summary
    except Exception as e:
        logger.error({"event": "summarize_report_error", "error": str(e)})
        raise

# --- Field Extraction ---
FIELD_PATTERNS = {
    "site_name": r'^(?:(?:add|insert)\s+sites?\s+|sites?\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "segment": r'^(?:(?:add|insert)\s+segments?\s+|segments?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "category": r'^(?:(?:add|insert)\s+categories?\s+|categories?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|segment|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "impression": r'^(?:(?:add|insert)\s+impressions?\s+|impressions?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|comments)\s*:)|$|\s*$)',
    "people": r'^(?:(?:add|insert)\s+(?:peoples?|persons?)\s+|(?:peoples?|persons?)\s*[:,]?\s*|(?:add|insert)\s+([^,]+?)\s+as\s+(?:peoples?|persons?)\s*)([^,\s]+)(?:\s+as\s+[^,\s]+)?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "role": r'^(?:(?:add|insert)\s+|(?:peoples?|persons?)\s+)?(\w+\s+\w+|\w+)\s*[:,]?\s*as\s+([^,\s]+)(?:\s+to\s+(?:peoples?|persons?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)|^(?:persons?|peoples?)\s*[:,]?\s*(\w+\s+\w+|\w+)\s*,\s*roles?\s*[:,]?\s*([^,\s]+)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "supervisor": r'^(?:i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised|(?:add|insert)\s+roles?\s*[:,]?\s*supervisor\s*|roles?\s*[:,]?\s*supervisor\s*$)(?:\s+by\s+([^,]+?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "company": r'^(?:(?:add|insert)\s+compan(?:y|ies)\s+|compan(?:y|ies)\s*[:,]?\s*|(?:add|insert)\s+([^,]+?)\s+as\s+compan(?:y|ies)\s*)[:,]?\s*([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "service": r'^(?:(?:add|insert)\s+services?\s+|services?\s*[:,]?\s*|services?\s*(?:were|provided)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "tool": r'^(?:(?:add|insert)\s+tools?\s+|tools?\s*[:,]?\s*|tools?\s*used\s*(?:included|were)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "activity": r'^(?:(?:add|insert)\s+activit(?:y|ies)\s+|activit(?:y|ies)\s*[:,]?\s*|activit(?:y|ies)\s*(?:covered|included)?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|issues?|time|weather|impression|comments)\s*:|\s+issues?\s*:|\s+times?\s*:|$|\s*$))',
    "issue": r'^(?:(?:add|insert)\s+issues?\s+|issues?\s*[:,]?\s*|issues?\s*(?:encountered|included)?\s*|problem\s*:?\s*|delay\s*:?\s*|injury\s*:?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|times?|weather|impression|comments)\s*:|\s+times?\s*:|$|\s*$))',
    "weather": r'^(?:(?:add|insert)\s+weathers?\s+|weathers?\s*[:,]?\s*|weather\s+was\s+|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|impression|comments)\s*:)|$|\s*$)',
    "time": r'^(?:(?:add|insert)\s+times?\s+|times?\s*[:,]?\s*|time\s+spent\s+|morning\s+time\s*|afternoon\s+time\s*|evening\s+time\s*)(morning|afternoon|evening|full day)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|weather|impression|comments)\s*:)|$|\s*$)',
    "comments": r'^(?:(?:add|insert)\s+comments?\s+|comments?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression)\s*:)|$|\s*$)',
    "clear": r'^(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?)\s*[:,]?\s*none$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": r'^(?:delete|remove)\s+(?:from\s+)?((?:sites?|segments?|categories?|compan(?:y|ies)|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?))\s*(?:from\s+)?\s*([^\s,]+(?:\s+[^\s,]+)*)?\s*$',
    "correct": r'^(?:correct|adjust|update|spell)(?:\s+spelling)?\s+((?:sites?|segments?|categories?|compan(?:y|ies)|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?))\s+(.+?)(?:\s+to\s+(.+?))?\s*(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)'
}

def validate_patterns() -> None:
    try:
        for field, pattern in FIELD_PATTERNS.items():
            re.compile(pattern, re.IGNORECASE)
        logger.info({"event": "patterns_validated"})
    except Exception as e:
        logger.error({"event": "pattern_validation_error", "field": field, "error": str(e)})
        raise

validate_patterns()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_fields(text: str) -> Dict[str, Any]:
    try:
        logger.info({"event": "extract_fields", "input": text})
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())

        reset_match = re.match(FIELD_PATTERNS["reset"], normalized_text, re.IGNORECASE)
        if reset_match:
            logger.info({"event": "reset_detected"})
            return {"reset": True}

        if normalized_text.lower() in ("undo", "/undo"):
            logger.info({"event": "undo_detected"})
            return {"undo": True}

        if normalized_text.lower() in ("status", "/status"):
            logger.info({"event": "status_detected"})
            return {"status": True}

        if normalized_text.lower() in ("export pdf", "/export pdf"):
            logger.info({"event": "export_pdf_detected"})
            return {"export_pdf": True}

        # Split commands on commas or periods, but only when followed by a new field
        commands = [cmd.strip() for cmd in re.split(r',\s*(?=(?:[^:]*:)|(?:add|insert)\s+(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments))|(?<!\w)\.\s*(?=[A-Z])', text) if cmd.strip()]
        logger.info({"event": "commands_split", "commands": commands})
        processed_result = {
            "company": [], "roles": [], "tools": [], "service": [],
            "activities": [], "issues": [], "people": []
        }
        seen_fields = set()

        for cmd in commands:
            delete_match = re.match(FIELD_PATTERNS["delete"], cmd, re.IGNORECASE)
            if delete_match:
                raw_field = delete_match.group(1).lower() if delete_match.group(1) else None
                value = delete_match.group(2).strip() if delete_match.group(2) else None
                field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
                logger.info({"event": "delete_command", "field": field, "value": value})
                if field in processed_result:
                    processed_result[field].append({"delete": value})
                continue

            correct_match = re.match(FIELD_PATTERNS["correct"], cmd, re.IGNORECASE)
            if correct_match:
                raw_field = correct_match.group(1).lower() if correct_match.group(1) else None
                old_value = correct_match.group(2).strip() if correct_match.group(2) else None
                new_value = correct_match.group(3).strip() if correct_match.group(3) else None
                field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
                logger.info({"event": "correct_command", "field": field, "old": old_value, "new": new_value})
                if field in processed_result and old_value:
                    if new_value:
                        processed_result[field].append({"correct": {"old": clean_value(old_value, field), "new": clean_value(new_value, field)}})
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
                elif key in ["people", "activities"]:
                    result.setdefault(key, []).extend(value if isinstance(value, list) else [value])
                else:
                    result[key] = value

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
                            existing_items = [i for i in existing_items if i.lower() != value.lower()]
                            logger.info({"event": f"{field}_deleted", "value": value})
                        else:
                            existing_items = []
                            logger.info({"event": f"{field}_cleared"})
                    elif "correct" in item:
                        old_value = item["correct"]["old"]
                        new_value = item["correct"]["new"]
                        if new_value and new_value.lower() != old_value.lower():
                            existing_items = [new_value if i.lower() == old_value.lower() else i for i in existing_items]
                            logger.info({"event": f"{field}_corrected", "old": old_value, "new": new_value})
                        else:
                            logger.info({"event": f"{field}_correct_prompt", "old": old_value})
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

        logger.info({"event": "fields_extracted", "result": result})
        return result
    except Exception as e:
        logger.error({"event": "extract.Fields_error", "input": text, "error": str(e)})
        raise

def extract_single_command(text: str) -> Dict[str, Any]:
    try:
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())
        logger.info({"event": "extract_single_command", "input": normalized_text})

        for raw_field, pattern in FIELD_PATTERNS.items():
            if raw_field in ["reset", "delete", "correct", "clear"]:
                continue
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                field = FIELD_MAPPING.get(raw_field, raw_field)
                logger.info({"event": "field_matched", "raw_field": raw_field, "mapped_field": field, "input": text})
                if field == "site_name" and re.search(r'\b(add|insert|delete|remove|correct|adjust|update|spell|none|as|role|new|reset)\b', text.lower()):
                    logger.info({"event": "skipped_site_name", "reason": "command-like input"})
                    continue
                if field == "people":
                    name = clean_value(match.group(1) or match.group(2), field)
                    if name.lower() == "supervisor":
                        logger.info({"event": "skipped_people_supervisor", "reason": "supervisor is a role"})
                        continue
                    result["people"] = [name]
                    logger.info({"event": "extracted_field", "field": "people", "value": name})
                elif field == "role":
                    name = clean_value(match.group(1) or match.group(3), field)
                    role = (match.group(2) or match.group(4)).title()
                    if name.lower() == "supervisor":
                        logger.info({"event": "skipped_role_supervisor", "reason": "supervisor is a role"})
                        continue
                    result["people"] = [name.strip()]
                    result["roles"] = [{"name": name.strip(), "role": role}]
                    logger.info({"event": "extracted_field", "field": "roles", "name": name, "role": role})
                elif field == "supervisor":
                    name = clean_value(match.group(1), field) if match.group(1) else "User"
                    result["people"] = [name]
                    result["roles"] = [{"name": name, "role": "Supervisor"}]
                    logger.info({"event": "extracted_field", "field": "roles", "value": name})
                elif field == "company":
                    name = clean_value(match.group(2) if match.group(2) else match.group(1), field)
                    if re.match(r'^(?:delete|remove|add|insert|correct|adjust|update|spell)\b', name.lower()):
                        logger.info({"event": "skipped_company", "reason": "command-like name", "value": name})
                        continue
                    result["company"] = [{"name": name}]
                    logger.info({"event": "extracted_field", "field": "company", "value": name})
                elif field == "clear":
                    field_name = FIELD_MAPPING.get(match.group(1).lower(), match.group(1).lower())
                    result[field_name] = [] if field_name in ["issues", "activities", "tools", "service", "company", "people", "roles"] else ""
                    logger.info({"event": "extracted_field", "field": field_name, "value": "none"})
                elif field in ["service"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"task": value.strip()}]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                elif field in ["tool"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"item": value.strip()}]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                elif field == "issue":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [{"description": value.strip()}]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                elif field == "activity":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = []
                    else:
                        result[field] = [value.strip()]
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                else:
                    value = clean_value(match.group(1), field)
                    result[field] = value
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                return result

        messages = [
            {"role": "system", "content": "Extract explicitly stated fields from construction site report input. Return JSON with extracted fields."},
            {"role": "user", "content": GPT_PROMPT + "\nInput text: " + text}
        ]
        try:
            response = client.chat.completions.create(
                model=CONFIG["OPENAI_MODEL"], messages=messages, temperature=CONFIG["OPENAI_TEMPERATURE"]
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
                activity_keywords = r'\b(work\s+was\s+done|activity|activities|task|progress Fleißig arbeitenprogress|construction|building|laying|setting|wiring|installation|scaffolding)\b'
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
        except (json.JSONDecodeError, Exception) as e:
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

def string_similarity(a: str, b: str) -> float:
    try:
        similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
        logger.info({"event": "string_similarity", "a": a, "b": b, "similarity": similarity})
        return similarity
    except Exception as e:
        logger.error({"event": "string_similarity_error", "error": str(e)})
        raise

def merge_data(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
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
                new_items = value if isinstance(value, list) else [value]
                for new_item in new_items:
                    if not isinstance(new_item, dict):
                        continue
                    if key == "company" and "name" in new_item:
                        new_name = new_item.get("name", "")
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("name", ""), new_name) > 0.6:
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
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("name", ""), new_name) > 0.6:
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
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("description", ""), new_desc) > 0.6:
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
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("item", ""), new_item_name) > 0.6:
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
                            if isinstance(existing_item, dict) and string_similarity(existing_item.get("task", ""), new_task) > 0.6:
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
                new_items = value if isinstance(value, list) else [value]
                for item in new_items:
                    if isinstance(item, str) and item not in existing_list and item.lower() != "supervisor":
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
        logger.info({"event": "data_merged", "merged": json.dumps(merged, indent=2)})
        return merged
    except Exception as e:
        logger.error({"event": "merge_data_error", "error": str(e)})
        raise

def delete_entry(data: Dict[str, Any], field: str, value: Optional[str] = None) -> Dict[str, Any]:
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

        if normalized_text in ("new", "new report", "reset", "reset report", "/new"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session(session_data)
            send_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        if normalized_text in ("undo", "/undo"):
            if sess["command_history"]:
                prev_state = sess["command_history"].pop()
                sess["structured_data"] = prev_state
                save_session(session_data)
                tpl = summarize_report(sess["structured_data"])
                send_message(chat_id, f"Undone last action.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            else:
                send_message(chat_id, "No actions to undo. Add fields like 'add site X' or 'add people Y'.")
            return "ok", 200

        if normalized_text in ("status", "/status"):
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Current report status:\n\n{tpl}\n\nAdd more fields or use '/export pdf'.")
            return "ok", 200

        if normalized_text in ("export pdf", "/export pdf"):
            pdf_buffer = generate_pdf(sess["structured_data"])
            if pdf_buffer:
                if send_pdf(chat_id, pdf_buffer):
                    send_message(chat_id, "PDF report sent successfully!")
                else:
                    send_message(chat_id, "⚠️ Failed to send PDF report. Please try again later.")
            else:
                send_message(chat_id, "⚠️ Failed to generate PDF report. Please try again later.")
            return "ok", 200

        clear_match = re.match(FIELD_PATTERNS["clear"], text, re.IGNORECASE)
        if clear_match:
            raw_field = clear_match.group(1).lower() if clear_match.group(1) else None
            field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
            if not field:
                logger.error({"event": "clear_command_error", "text": text, "error": "Invalid field"})
                send_message(chat_id, f"⚠️ Invalid clear command: '{text}'. Try 'tools: none' or 'issues: none'.")
                return "ok", 200
            sess["command_history"].append(sess["structured_data"].copy())
            sess["structured_data"][field] = [] if field in ["issues", "activities", "tools", "service", "company", "people", "roles"] else ""
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Cleared {field}\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        delete_match = re.match(FIELD_PATTERNS["delete"], text, re.IGNORECASE)
        if delete_match:
            raw_field = delete_match.group(1).lower() if delete_match.group(1) else None
            value = delete_match.group(2).strip() if delete_match.group(2) else None
            field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
            if not field:
                logger.error({"event": "delete_command_error", "text": text, "error": "Invalid field"})
                send_message(chat_id, f"⚠️ Invalid delete command: '{text}'. Try 'delete company Taekwondo Agi' or 'delete Jonas from people'.")
                return "ok", 200
            sess["command_history"].append(sess["structured_data"].copy())
            sess["structured_data"] = delete_entry(sess["structured_data"], field, value)
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Removed {field}" + (f": {value}" if value else "") + f"\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
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
        if not any(k in extracted for k in ["company", "people", "roles", "tools", "service", "activities", "issues", "time", "weather", "impression", "comments", "segment", "category", "site_name"]):
            logger.warning({"event": "unrecognized_input", "input": text})
            send_message(chat_id, f"⚠️ Unrecognized input: '{text}'. Try 'add site Downtown Project', 'add issue power outage', or 'spell companies Orient Corp'.")
            return "ok", 200

        sess["command_history"].append(sess["structured_data"].copy())
        sess["structured_data"] = merge_data(sess["structured_data"], enrich_date(extracted))
        save_session(session_data)
        tpl = summarize_report(sess["structured_data"])
        send_message(chat_id, f"✅ Updated report:\n\n{tpl}\n\nAnything else to add or correct?")
        return "ok", 200
    except Exception as e:
        logger.error({"event": "handle_command_error", "error": str(e)})
        send_message(chat_id, "⚠️ An error occurred. Please try again.")
        return "error", 500

@app.route("/webhook", methods=["POST"])
def webhook() -> tuple[str, int]:
    try:
        data = request.get_json(force=True)
        logger.info({"event": "webhook_received", "data": data})
        if not data or "message" not in data:
            logger.info({"event": "no_message"})
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        logger.info({"event": "message_received", "chat_id": chat_id, "text": text})

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
            logger.info({"event": "session_created", "chat_id": chat_id})

        sess = session_data[chat_id]

        # Clean up invalid "Supervisor" entries
        if "Supervisor" in sess["structured_data"].get("people", []):
            sess["structured_data"]["people"] = [p for p in sess["structured_data"].get("people", []) if p != "Supervisor"]
            sess["structured_data"]["roles"] = [r for r in sess["structured_data"].get("roles", []) if r.get("name") != "Supervisor"]
            logger.info({"event": "cleaned_supervisor_entries", "chat_id": chat_id})

        if "voice" in msg:
            text = transcribe_voice(msg["voice"]["file_id"])
            if not text:
                send_message(chat_id, "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'add site Downtown Project').")
                return "ok", 200
            logger.info({"event": "transcribed_voice", "text": text})

        if sess.get("awaiting_reset_confirmation", False):
            normalized_text = re.sub(r'[.!?]\s*$', '', text.strip()).lower()
            logger.info({"event": "reset_confirmation", "text": normalized_text, "pending_input": sess["pending_input"]})
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
            logger.info({"event": "spelling_correction_response", "field": field, "old_value": old_value, "new_value": new_value})
            if new_value.lower() == old_value.lower():
                sess["awaiting_spelling_correction"] = None
                save_session(session_data)
                send_message(chat_id, f"⚠️ New value '{new_value}' is the same as the old value '{old_value}'. Please provide a different spelling for '{old_value}' in {field}.")
                return "ok", 200
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
            elif field == "weather":
                sess["structured_data"]["weather"] = new_value
            elif field == "company":
                sess["structured_data"]["company"] = [
                    {"name": new_value} if item.get("name", "").lower() == old_value.lower() else item
                    for item in sess["structured_data"].get("company", [])
                ]
            else:
                sess["structured_data"][field] = [
                    {"name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description": new_value}
                    if item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description", "").lower() == old_value.lower()
                    else item
                    for item in sess["structured_data"].get(field, [])
                ]
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Corrected {field} from '{old_value}' to '{new_value}'.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        return handle_command(chat_id, text, sess)
    except Exception as e:
        logger.error({"event": "webhook_error", "error": str(e)})
        return "error", 500
