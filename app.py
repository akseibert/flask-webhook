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
from typing import Dict, Any, List, Optional, Callable, Tuple, Set, Union
from flask import Flask, request
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from difflib import SequenceMatcher
from collections import deque
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, cm
from reportlab.lib import colors
from decouple import config

# --- Configuration ---
CONFIG = {
    "SESSION_FILE": config("SESSION_FILE", default="/opt/render/project/src/session_data.json"),
    "PAUSE_THRESHOLD": config("PAUSE_THRESHOLD", default=300, cast=int),
    "MAX_HISTORY": config("MAX_HISTORY", default=10, cast=int),
    "OPENAI_MODEL": config("OPENAI_MODEL", default="gpt-3.5-turbo"),
    "OPENAI_TEMPERATURE": config("OPENAI_TEMPERATURE", default=0.2, cast=float),
    "NAME_SIMILARITY_THRESHOLD": config("NAME_SIMILARITY_THRESHOLD", default=0.7, cast=float),
    "COMMAND_SIMILARITY_THRESHOLD": config("COMMAND_SIMILARITY_THRESHOLD", default=0.85, cast=float),
    "REPORT_FORMAT": config("REPORT_FORMAT", default="detailed"),
    "MAX_SUGGESTIONS": config("MAX_SUGGESTIONS", default=3, cast=int),
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
    """Enhanced logging with standardized format and additional context"""
    logger.info({"event": event, "timestamp": datetime.now().isoformat(), **kwargs})

# --- Field Mapping ---
FIELD_MAPPING = {
    'site': 'site_name', 'sites': 'site_name',
    'segment': 'segment', 'segments': 'segment',
    'category': 'category', 'categories': 'category',
    'company': 'companies', 'companies': 'companies',
    'person': 'people', 'people': 'people', 'persons': 'people', 'peoples': 'people',
    'role': 'roles', 'roles': 'roles',
    'tool': 'tools', 'tools': 'tools',
    'service': 'services', 'services': 'services',
    'activity': 'activities', 'activities': 'activities',
    'issue': 'issues', 'issues': 'issues',
    'time': 'time', 'times': 'time',
    'weather': 'weather', 'weathers': 'weather',
    'impression': 'impression', 'impressions': 'impression',
    'comment': 'comments', 'comments': 'comments',
    'architect': 'roles', 'engineer': 'roles', 'supervisor': 'roles',
    'manager': 'roles', 'worker': 'roles', 'window installer': 'roles',
    'contractor': 'roles', 'inspector': 'roles', 'electrician': 'roles',
    'plumber': 'roles', 'foreman': 'roles', 'designer': 'roles'
}

# Reverse mapping to help with validation and suggestions
INVERSE_FIELD_MAPPING = {}
for k, v in FIELD_MAPPING.items():
    INVERSE_FIELD_MAPPING.setdefault(v, []).append(k)

# Lists of field types for validation and helper functions
SCALAR_FIELDS = ["site_name", "segment", "category", "time", "weather", "impression", "comments"]
LIST_FIELDS = ["people", "companies", "roles", "tools", "services", "activities", "issues"]
DICT_LIST_FIELDS = ["companies", "roles", "tools", "services", "issues"]
SIMPLE_LIST_FIELDS = ["people", "activities"]

# Map fields to their suggested value lists for common terms
FIELD_SUGGESTIONS = {
    "weather": ["sunny", "cloudy", "rainy", "windy", "foggy", "snowy", "clear", "overcast"],
    "time": ["morning", "afternoon", "evening", "full day", "half day", "8 hours", "4 hours"],
    "impression": ["productive", "satisfactory", "challenging", "efficient", "delayed", "excellent", "on schedule"],
    "roles": ["supervisor", "manager", "worker", "engineer", "architect", "contractor", "inspector", "electrician", "foreman"],
}

# --- Regex Patterns ---
categories = [
    "site", "segment", "category", "company", "companies", "person", "people",
    "role", "roles", "tool", "tools", "service", "services", "activity",
    "activities", "issue", "issues", "time", "weather", "impression", "comments",
    "architect", "engineer", "supervisor", "manager", "worker", "window installer",
    "contractor", "inspector", "electrician", "plumber", "foreman", "designer"
]
list_categories = ["people", "companies", "roles", "tools", "services", "activities", "issues"]

categories_pattern = '|'.join(re.escape(cat) for cat in categories)
list_categories_pattern = '|'.join(re.escape(cat) for cat in list_categories)

FIELD_PATTERNS = {
    "site_name": r'^(?:(?:add|insert)\s+sites?\s+|sites?\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "segment": r'^(?:(?:add|insert)\s+segments?\s+|segments?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "category": r'^(?:(?:add|insert)\s+categories?\s+|categories?\s*[:,]?\s*)([^,.\s]+)(?=(?:\s*,\s*(?:site|segment|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*\.)',
    "impression": r'^(?:(?:add|insert)\s+impressions?\s+|impressions?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|comments)\s*:)|$|\s*$)',
    "people": r'^(?:(?:add|insert)\s+(?:peoples?|persons?)\s+|(?:peoples?|persons?)\s*[:,]?\s*)([^,]+?)(?:\s+as\s+([^,]+?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "role": r'^(?:(?:add|insert)\s+|(?:peoples?|persons?)\s+)?(\w+\s+\w+|\w+)\s*[:,]?\s*as\s+([^,\s]+)(?:\s+to\s+(?:peoples?|persons?))?(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)|^(?:persons?|peoples?)\s*[:,]?\s*(\w+\s+\w+|\w+)\s*,\s*roles?\s*[:,]?\s*([^,\s]+)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "supervisor": r'^(?:supervisors?\s+were\s+|(?:add|insert)\s+roles?\s*[:,]?\s*supervisor\s*|roles?\s*[:,]?\s*supervisor\s*)([^,]+?)(?=\s*(?:\.|\s+tools?|services?|activit(?:y|ies)|issues?|$))',
    "company": r'^(?:(?:add|insert)\s+compan(?:y|ies)\s+|compan(?:y|ies)\s*[:,]?\s*|(?:add|insert)\s+([^,]+?)\s+as\s+compan(?:y|ies)\s*)[:,]?\s*((?:[^,.]+?(?:\s+and\s+[^,.]+?)*?))(?=\s*(?:\.|\s+supervisors?|tools?|services?|activit(?:y|ies)|issues?|$))',
    "service": r'^(?:(?:add|insert)\s+services?\s+|services?\s*[:,]?\s*|services?\s*(?:were|provided)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "tool": r'^(?:(?:add|insert)\s+tools?\s+|tools?\s*[:,]?\s*|tools?\s*used\s*(?:included|were)\s+)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "activity": r'^(?:(?:add|insert)\s+activit(?:y|ies)\s+|activit(?:y|ies)\s*[:,]?\s*|activit(?:y|ies)\s*(?:covered|included)?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|issues?|time|weather|impression|comments)\s*:|\s+issues?\s*:|\s+times?\s*:|$|\s*$))',
    "issue": r'^(?:(?:add|insert)\s+issues?\s+|issues?\s*[:,]?\s*|issues?\s*(?:encountered|included)?\s*|problem\s*:?\s*|delay\s*:?\s*|injury\s*:?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|times?|weather|impression|comments)\s*:|\s+times?\s*:|$|\s*$))',
    "weather": r'^(?:(?:add|insert)\s+weathers?\s+|weathers?\s*[:,]?\s*|weather\s+was\s+|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|impression|comments)\s*:)|$|\s*$)',
    "time": r'^(?:(?:add|insert)\s+times?\s+|times?\s*[:,]?\s*|time\s+spent\s+|morning\s+time\s*|afternoon\s+time\s*|evening\s+time\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|weather|impression|comments)\s*:)|$|\s*$)',
    "comments": r'^(?:(?:add|insert)\s+comments?\s+|comments?\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression)\s*:)|$|\s*$)',
    "clear": r'^(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?|site_name|segment|category|time|weather|impression)\s*[:,]?\s*(?:none|delete|clear|remove|reset)$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": rf'^(?:delete|remove|none)\s+({categories_pattern})\s*(.+)?$|^(?:delete|remove)\s+(.+)\s+(?:from|in|of|at)\s+({categories_pattern})$|^({categories_pattern})\s+(?:delete|remove|none)\s*(.+)?$',
    "delete_entire": rf'^(?:delete|remove|clear)\s+(?:entire|all)\s+(?:category|field|entries|list)?\s*[:]?\s*({list_categories_pattern})\s*[.!]?$',
    "correct": r'^(?:correct|adjust|update|spell|fix)(?:\s+spelling)?\s+((?:sites?|segments?|categories?|compan(?:y|ies)|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?))\s+([^,]+?)(?:\s+to\s+([^,]+?))?\s*(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "help": r'^help(?:\s+on\s+([a-z_]+))?$|^\/help(?:\s+([a-z_]+))?$',
    "undo_last": r'^undo\s+last\s*[.!]?$|^undo\s+last\s+(?:change|modification|edit)\s*[.!]?$',
    "context_add": r'^(?:add|include|include|insert)\s+(?:it|this|that|him|her|them)\s+(?:to|in|into|as)\s+(.+?)\s*[.!]?$',
    "summary": r'^(summarize|summary|short report|brief report|overview|compact report)\s*[.!]?$',
    "detailed": r'^(detailed|full|complete|comprehensive)\s+report\s*[.!]?$',
}

# Extended regex patterns for more nuanced commands
CONTEXTUAL_PATTERNS = {
    "reference_person": r'(he|she|they|him|her|them)\b',
    "reference_thing": r'(it|this|that|these|those)\b',
    "similarity_check": r'(similar|like|same as|close to)\s+([^,.]+)',
    "last_mentioned": r'(last|previous|earlier|before)\s+(mentioned|added|discussed|noted)',
}

# --- Session Management ---
def load_session() -> Dict[str, Any]:
    """Load session data from the session file with enhanced error handling"""
    try:
        if os.path.exists(CONFIG["SESSION_FILE"]):
            with open(CONFIG["SESSION_FILE"], "r") as f:
                data = json.load(f)
            
            for chat_id, session in data.items():
                # Convert command history to deque for efficiency
                if "command_history" in session:
                    session["command_history"] = deque(
                        session["command_history"], maxlen=CONFIG["MAX_HISTORY"]
                    )
                
                # Add last_change_history if not present (for undo last change)
                if "last_change_history" not in session:
                    session["last_change_history"] = []
                
                # Normalize field names in structured_data
                if "structured_data" in session:
                    _normalize_field_names(session["structured_data"])
                
                # Ensure context tracking is present
                if "context" not in session:
                    session["context"] = {
                        "last_mentioned_person": None,
                        "last_mentioned_item": None,
                        "last_field": None,
                    }
            
            log_event("session_loaded", file=CONFIG["SESSION_FILE"])
            return data
        
        log_event("session_file_not_found", file=CONFIG["SESSION_FILE"])
        return {}
    except json.JSONDecodeError as e:
        log_event("session_json_error", error=str(e))
        # Try to recover from corrupt JSON
        backup_file = f"{CONFIG['SESSION_FILE']}.bak"
        if os.path.exists(backup_file):
            try:
                with open(backup_file, "r") as f:
                    data = json.load(f)
                log_event("session_loaded_from_backup", file=backup_file)
                return data
            except Exception:
                pass
        return {}
    except Exception as e:
        log_event("load_session_error", error=str(e))
        return {}

def save_session(session_data: Dict[str, Any]) -> None:
    """Save session data with backup creation and sanitization"""
    try:
        # First create a backup of the current file if it exists
        if os.path.exists(CONFIG["SESSION_FILE"]):
            backup_file = f"{CONFIG['SESSION_FILE']}.bak"
            try:
                with open(CONFIG["SESSION_FILE"], "r") as src, open(backup_file, "w") as dst:
                    dst.write(src.read())
            except Exception as e:
                log_event("backup_creation_error", error=str(e))
        
        # Prepare serializable data
        serializable_data = {}
        for chat_id, session in session_data.items():
            serializable_session = session.copy()
            
            # Convert deque to list for serialization
            if "command_history" in serializable_session:
                serializable_session["command_history"] = list(serializable_session["command_history"])
            
            # Ensure structured data has consistent field names
            if "structured_data" in serializable_session:
                _normalize_field_names(serializable_session["structured_data"])
            
            serializable_data[chat_id] = serializable_session
        
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(CONFIG["SESSION_FILE"]), exist_ok=True)
        
        # Write data to file
        with open(CONFIG["SESSION_FILE"], "w") as f:
            json.dump(serializable_data, f, indent=2)
        
        log_event("session_saved", file=CONFIG["SESSION_FILE"])
    except Exception as e:
        log_event("save_session_error", error=str(e))

def _normalize_field_names(data: Dict[str, Any]) -> None:
    """Ensure all field names in the structured data are standardized"""
    changes = []
    
    # Fix scalar fields
    for field in SCALAR_FIELDS:
        if field in data and not isinstance(data[field], str):
            data[field] = str(data[field]) if data[field] is not None else ""
            changes.append(field)
    
    # Fix company/companies
    if "company" in data and "companies" not in data:
        data["companies"] = data.pop("company")
        changes.append("company -> companies")
    
    # Fix service/services
    if "service" in data and "services" not in data:
        data["services"] = data.pop("service")
        changes.append("service -> services")
    
    # Fix tool/tools
    if "tool" in data and "tools" not in data:
        data["tools"] = data.pop("tool")
        changes.append("tool -> tools")
    
    # Ensure all list fields exist
    for field in LIST_FIELDS:
        if field not in data:
            data[field] = []
            changes.append(f"added empty {field}")
    
    if changes:
        log_event("normalized_field_names", changes=changes)

session_data = load_session()

def blank_report() -> Dict[str, Any]:
    """Create a blank report template with all required fields"""
    return {
        "site_name": "", "segment": "", "category": "",
        "companies": [], "people": [], "roles": [], "tools": [], "services": [],
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
- companies: list of objects with "name" (e.g., [{"name": "Acme Corp"}])
- people: list of strings (e.g., ["Anna", "Tobias"])
- roles: list of objects with "name" and "role" (e.g., [{"name": "Anna", "role": "Supervisor"}])
- tools: list of objects with "item" (e.g., [{"item": "Crane"}])
- services: list of objects with "task" (e.g., [{"task": "Excavation"}])
- activities: list of strings (e.g., ["Concrete pouring"])
- issues: list of objects with "description" (required), "caused_by" (optional), "has_photo" (optional, default false)
- time: string (e.g., "morning", "full day")
- weather: string (e.g., "cloudy")
- impression: string (e.g., "productive")
- comments: string (e.g., "Ensure safety protocols")
- date: string (format dd-mm-yyyy)

Example Input:
"Goodmorning, at the Central Plaza site, segment 5, companies involved were BuildRight AG and ElectricFlow GmbH. Supervisors were Anna Keller and MarkusSchmidt. Tools used included a mobile crane and welding equipment. Services provided were electrical wiring and HVAC installation. Activities covered laying foundations and setting up scaffolding. Issues encountered: a power outage at 10 AM caused a 2-hour delay, and a minor injury occurred when a worker slipped—no photo taken. Weather was cloudy with intermittent rain. Time spent: full day. Impression: productive despite setbacks. Comments: ensure safety protocols are reinforced"

Expected Output:
{
  "site_name": "Central Plaza",
  "segment": "5",
  "companies": [{"name": "BuildRight AG"}, {"name": "ElectricFlow GmbH"}],
  "roles": [{"name": "Anna Keller", "role": "Supervisor"}, {"name": "MarkusSchmidt", "role": "Supervisor"}],
  "tools": [{"item": "mobile crane"}, {"item": "welding equipment"}],
  "services": [{"task": "electrical wiring"}, {"task": "HVAC installation"}],
  "activities": ["laying foundations", "setting up scaffolding"],
  "issues": [
    {"description": "power outage at 10 AM caused a 2-hour delay"},
    {"description": "minor injury occurred when a worker slipped", "has_photo": false}
  ],
  "weather": "cloudy with intermittent rain",
  "time": "full day",
  "impression": "productive despite setbacks",
  "comments": "ensure safety protocols are reinforced"
}
"""

# --- Signal Handlers ---
def handle_shutdown(signum: int, frame: Any) -> None:
    """Handle shutdown signals by saving session data"""
    log_event("shutdown_signal", signal=signum)
    save_session(session_data)
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# --- Telegram API ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def send_message(chat_id: str, text: str) -> None:
    """Send message to Telegram with enhanced error handling"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        response.raise_for_status()
        log_event("message_sent", chat_id=chat_id, text=text[:50])
    except requests.RequestException as e:
        log_event("send_message_error", chat_id=chat_id, error=str(e))
        # Try with simpler formatting if Markdown fails
        if "parse_mode" in str(e):
            try:
                response = requests.post(url, json={"chat_id": chat_id, "text": text})
                response.raise_for_status()
                log_event("message_sent_without_markdown", chat_id=chat_id)
                return
            except Exception:
                pass
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def get_telegram_file_path(file_id: str) -> str:
    """Get file path from Telegram"""
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
def transcribe_voice(file_id: str) -> Tuple[str, float]:
    """Transcribe voice message with confidence score"""
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
            return "", 0.0
        
        # Extract and return confidence (approximate calculation)
        # Longer texts generally indicate higher confidence
        confidence = min(0.95, 0.5 + (len(text) / 200))
        
        log_event("transcription_success", text=text, confidence=confidence)
        return text, confidence
    except (requests.RequestException, Exception) as e:
        log_event("transcription_failed", error=str(e))
        return "", 0.0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def send_pdf(chat_id: str, pdf_buffer: io.BytesIO, report_type: str = "standard") -> bool:
    """Send PDF report to user"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        caption = "Here is your construction site report."
        if report_type == "summary":
            caption = "Here is your summarized construction site report."
        elif report_type == "detailed":
            caption = "Here is your detailed construction site report."
            
        files = {'document': ('report.pdf', pdf_buffer, 'application/pdf')}
        data = {'chat_id': chat_id, 'caption': caption}
        response = requests.post(url, files=files, data=data)
        response.raise_for_status()
        log_event("pdf_sent", chat_id=chat_id, report_type=report_type)
        return True
    except requests.RequestException as e:
        log_event("send_pdf_error", chat_id=chat_id, error=str(e))
        return False

# --- Report Generation ---
def generate_pdf(report_data: Dict[str, Any], report_type: str = "detailed") -> Optional[io.BytesIO]:
    """Generate PDF report with enhanced formatting and layout"""
    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        
        # Create custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Title'],
            fontSize=16,
            spaceAfter=12
        )
        
        heading_style = ParagraphStyle(
            'Heading',
            parent=styles['Heading2'],
            fontSize=12,
            spaceAfter=6
        )
        
        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=3
        )
        
        # Start building the document
        story = []
        
        # Add title and date
        title = f"Construction Site Report - {report_data.get('site_name') or 'Unknown Site'}"
        story.append(Paragraph(title, title_style))
        story.append(Paragraph(f"Date: {report_data.get('date', datetime.now().strftime('%d-%m-%Y'))}", normal_style))
        story.append(Spacer(1, 12))
        
        # Basic site information section
        story.append(Paragraph("Site Information", heading_style))
        site_info = [
            ("Site", report_data.get("site_name", "")),
            ("Segment", report_data.get("segment", "")),
            ("Category", report_data.get("category", ""))
        ]
        
        # Only show non-empty fields in summary mode
        site_info = [(label, value) for label, value in site_info if value or report_type == "detailed"]
        
        for label, value in site_info:
            if value:
                story.append(Paragraph(f"<b>{label}:</b> {value}", normal_style))
        
        if site_info:
            story.append(Spacer(1, 6))
        
        # Personnel section
        if report_data.get("people") or report_data.get("companies") or report_data.get("roles"):
            story.append(Paragraph("Personnel & Companies", heading_style))
            
            if report_data.get("companies"):
                companies_str = ", ".join(c.get("name", "") for c in report_data.get("companies", []) if c.get("name"))
                if companies_str:
                    story.append(Paragraph(f"<b>Companies:</b> {companies_str}", normal_style))
            
            if report_data.get("people"):
                people_str = ", ".join(report_data.get("people", []))
                if people_str:
                    story.append(Paragraph(f"<b>People:</b> {people_str}", normal_style))
            
            if report_data.get("roles"):
                roles_list = []
                for r in report_data.get("roles", []):
                    if isinstance(r, dict) and r.get("name") and r.get("role"):
                        roles_list.append(f"{r['name']} ({r['role']})")
                
                if roles_list:
                    story.append(Paragraph(f"<b>Roles:</b> {', '.join(roles_list)}", normal_style))
            
            story.append(Spacer(1, 6))
        
        # Equipment and services section
        if report_data.get("tools") or report_data.get("services"):
            story.append(Paragraph("Equipment & Services", heading_style))
            
            if report_data.get("tools"):
                tools_str = ", ".join(t.get("item", "") for t in report_data.get("tools", []) if t.get("item"))
                if tools_str:
                    story.append(Paragraph(f"<b>Tools:</b> {tools_str}", normal_style))
            
            if report_data.get("services"):
                services_str = ", ".join(s.get("task", "") for s in report_data.get("services", []) if s.get("task"))
                if services_str:
                    story.append(Paragraph(f"<b>Services:</b> {services_str}", normal_style))
            
            story.append(Spacer(1, 6))
        
        # Activities section
        if report_data.get("activities"):
            story.append(Paragraph("Activities", heading_style))
            activities = report_data.get("activities", [])
            
            if report_type == "detailed":
                # In detailed mode, list each activity with a bullet
                for activity in activities:
                    story.append(Paragraph(f"• {activity}", normal_style))
            else:
                # In summary mode, just list them with commas
                activities_str = ", ".join(activities)
                story.append(Paragraph(activities_str, normal_style))
            
            story.append(Spacer(1, 6))
        
        # Issues section
        if report_data.get("issues"):
            story.append(Paragraph("Issues & Problems", heading_style))
            issues = report_data.get("issues", [])
            
            if issues:
                if report_type == "detailed":
                    # In detailed mode, list each issue separately
                    for issue in issues:
                        if isinstance(issue, dict):
                            desc = issue.get("description", "")
                            by = issue.get("caused_by", "")
                            photo = " (Photo Available)" if issue.get("has_photo") else ""
                            extra = f" (by {by})" if by else ""
                            story.append(Paragraph(f"• {desc}{extra}{photo}", normal_style))
                else:
                    # In summary mode, just list them with semicolons
                    issues_str = "; ".join(i.get("description", "") for i in issues if isinstance(i, dict) and i.get("description"))
                    story.append(Paragraph(issues_str, normal_style))
            
            story.append(Spacer(1, 6))
        
        # Conditions section
        if report_data.get("time") or report_data.get("weather") or report_data.get("impression"):
            story.append(Paragraph("Conditions", heading_style))
            
            if report_data.get("time"):
                story.append(Paragraph(f"<b>Time:</b> {report_data.get('time', '')}", normal_style))
            
            if report_data.get("weather"):
                story.append(Paragraph(f"<b>Weather:</b> {report_data.get('weather', '')}", normal_style))
            
            if report_data.get("impression"):
                story.append(Paragraph(f"<b>Impression:</b> {report_data.get('impression', '')}", normal_style))
            
            story.append(Spacer(1, 6))
        
        # Comments section
        if report_data.get("comments"):
            story.append(Paragraph("Additional Comments", heading_style))
            story.append(Paragraph(report_data.get("comments", ""), normal_style))
        
        # Build the document
        doc.build(story)
        buffer.seek(0)
        
        log_event("pdf_generated", 
                  size_bytes=buffer.getbuffer().nbytes, 
                  report_type=report_type, 
                  site=report_data.get("site_name", "Unknown"))
        return buffer
    except Exception as e:
        log_event("pdf_generation_error", error=str(e))
        return None

def summarize_report(data: Dict[str, Any]) -> str:
    """Generate a formatted text summary of the report data"""
    try:
        roles_str = ", ".join(f"{r.get('name', '')} ({r.get('role', '')})" for r in data.get("roles", []) if r.get("role"))
        
        lines = [
            f"🏗️ **Site**: {data.get('site_name', '') or ''}",
            f"🛠️ **Segment**: {data.get('segment', '') or ''}",
            f"📋 **Category**: {data.get('category', '') or ''}",
            f"🏢 **Companies**: {', '.join(c.get('name', '') for c in data.get('companies', []) if c.get('name')) or ''}",
            f"👷 **People**: {', '.join(data.get('people', []) or [])}",
            f"🎭 **Roles**: {roles_str}",
            f"🔧 **Services**: {', '.join(s.get('task', '') for s in data.get('services', []) if s.get('task')) or ''}",
            f"🛠️ **Tools**: {', '.join(t.get('item', '') for t in data.get('tools', []) if t.get('item')) or ''}",
            f"📅 **Activities**: {', '.join(data.get('activities', []) or [])}",
            "⚠️ **Issues**:"
        ]
        
        # Process issues for display
        valid_issues = [i for i in data.get("issues", []) if isinstance(i, dict) and i.get("description", "").strip()]
        if valid_issues:
            for i in valid_issues:
                desc = i["description"]
                by = i.get("caused_by", "")
                photo = " 📸" if i.get("has_photo") else ""
                extra = f" (by {by})" if by else ""
                lines.append(f"  • {desc}{extra}{photo}")
        else:
            lines.append("  • None reported")
        
        lines.extend([
            f"⏰ **Time**: {data.get('time', '') or ''}",
            f"🌦️ **Weather**: {data.get('weather', '') or ''}",
            f"😊 **Impression**: {data.get('impression', '') or ''}",
            f"💬 **Comments**: {data.get('comments', '') or ''}",
            f"📆 **Date**: {data.get('date', '') or ''}"
        ])
        
        # Filter out empty lines to make the report cleaner
        summary = "\n".join(line for line in lines if line.strip() and not line.endswith(": "))
        log_event("summarize_report", summary_length=len(summary))
        return summary
    except Exception as e:
        log_event("summarize_report_error", error=str(e))
        # Fallback to a simpler summary in case of error
        return "**Construction Site Report**\n\nSite: " + (data.get("site_name", "Unknown") or "Unknown") + "\nDate: " + data.get("date", datetime.now().strftime("%d-%m-%Y"))

# --- Data Processing ---
def clean_value(value: Optional[str], field: str) -> Optional[str]:
    """Clean and normalize a field value"""
    if value is None:
        return value
    
    # First remove common command prefixes
    cleaned = re.sub(r'^(?:add\s+|insert\s+|from\s+|correct\s+spelling\s+|spell\s+|delete\s+|remove\s+|clear\s+)', '', value.strip(), flags=re.IGNORECASE)
    
    # Standardize some common terms
    if field == "activities":
        # Fix common typos and terminology
        cleaned = cleaned.replace('tone', 'stone')
    elif field == "weather":
        # Standardize weather terms
        for term, replacement in [
            (r'\bsun\b', 'sunny'),
            (r'\brain\b', 'rainy'),
            (r'\bcloud\b', 'cloudy'),
            (r'\bfog\b', 'foggy'),
            (r'\bsnow\b', 'snowy'),
            (r'\bwind\b', 'windy')
        ]:
            cleaned = re.sub(term, replacement, cleaned, flags=re.IGNORECASE)
    
    # Trim excess whitespace and capitalization
    cleaned = " ".join(cleaned.split())
    
    # For names and roles, ensure proper capitalization
    if field in ["people", "companies", "roles"]:
        cleaned = " ".join(word.capitalize() for word in cleaned.split())
    
    log_event("cleaned_value", field=field, raw=value, cleaned=cleaned)
    return cleaned

def enrich_date(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and standardize date format in report data"""
    try:
        today = datetime.now().strftime("%d-%m-%Y")
        
        if not data.get("date"):
            data["date"] = today
        else:
            # Try to handle various date formats
            try:
                # Check if it's already in the right format
                input_date = datetime.strptime(data["date"], "%d-%m-%Y")
                if input_date > datetime.now():
                    data["date"] = today
            except ValueError:
                # Try alternative date formats
                for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%m.%d.%Y"]:
                    try:
                        input_date = datetime.strptime(data["date"], fmt)
                        data["date"] = input_date.strftime("%d-%m-%Y")
                        break
                    except ValueError:
                        continue
                else:
                    # If no format matched, use today's date
                    data["date"] = today
        
        log_event("date_enriched", date=data["date"])
        return data
    except Exception as e:
        log_event("enrich_date_error", error=str(e))
        # Ensure we always have a valid date
        data["date"] = datetime.now().strftime("%d-%m-%Y")
        return data

# --- Field Extraction ---
def validate_patterns() -> None:
    """Validate all regex patterns on startup"""
    try:
        for field, pattern in FIELD_PATTERNS.items():
            re.compile(pattern, re.IGNORECASE)
        
        for context, pattern in CONTEXTUAL_PATTERNS.items():
            re.compile(pattern, re.IGNORECASE)
            
        log_event("patterns_validated", count=len(FIELD_PATTERNS) + len(CONTEXTUAL_PATTERNS))
    except Exception as e:
        log_event("pattern_validation_error", field=field, error=str(e))
        raise

validate_patterns()

def string_similarity(a: str, b: str) -> float:
    """Calculate string similarity ratio between two strings"""
    try:
        if not a or not b:
            return 0.0
            
        a_lower = a.lower()
        b_lower = b.lower()
        
        # Check for direct substring match first (for partial name matching)
        if a_lower in b_lower or b_lower in a_lower:
            # Calculate the ratio of the shorter string to the longer one
            shorter = min(len(a_lower), len(b_lower))
            longer = max(len(a_lower), len(b_lower))
            return min(0.95, shorter / longer + 0.3)  # Add 0.3 to favor substring matches
        
        # Otherwise use SequenceMatcher
        similarity = SequenceMatcher(None, a_lower, b_lower).ratio()
        
        # Boost single-word matches in multi-word strings (for partial name matching)
        if " " in a_lower or " " in b_lower:
            a_words = set(a_lower.split())
            b_words = set(b_lower.split())
            # If any word matches exactly, boost similarity
            if any(word in b_words for word in a_words):
                similarity = min(0.95, similarity + 0.15)
                
        log_event("string_similarity", a=a, b=b, similarity=similarity)
        return similarity
    except Exception as e:
        log_event("string_similarity_error", error=str(e))
        return 0.0

def find_name_match(name: str, name_list: List[str]) -> Optional[str]:
    """Find the best match for a name in a list of names"""
    if not name or not name_list:
        return None
        
    # First try exact match
    for full_name in name_list:
        if full_name.lower() == name.lower():
            return full_name
            
    # Then try to find names containing the search term as a word
    search_words = set(name.lower().split())
    for full_name in name_list:
        full_name_words = set(full_name.lower().split())
        # If the search name is a first name or last name
        if search_words.issubset(full_name_words) or search_words & full_name_words:
            return full_name
    
    # Finally try similarity matching
    best_match = None
    best_score = CONFIG["NAME_SIMILARITY_THRESHOLD"]
    
    for full_name in name_list:
        score = string_similarity(name, full_name)
        if score > best_score:
            best_score = score
            best_match = full_name
    
    if best_match:
        log_event("name_match_found", search=name, match=best_match, score=best_score)
        
    return best_match

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def extract_fields(text: str) -> Dict[str, Any]:
    """Extract fields from text input with enhanced error handling and field validation"""
    try:
        log_event("extract_fields", input=text)
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())

        # Check for system commands first
        reset_match = re.match(FIELD_PATTERNS["reset"], normalized_text, re.IGNORECASE)
        if reset_match:
            log_event("reset_detected")
            return {"reset": True}

        if normalized_text.lower() in ("undo", "/undo"):
            log_event("undo_detected")
            return {"undo": True}
            
        if re.match(FIELD_PATTERNS["undo_last"], normalized_text, re.IGNORECASE):
            log_event("undo_last_detected")
            return {"undo_last": True}

        if normalized_text.lower() in ("status", "/status"):
            log_event("status_detected")
            return {"status": True}

        if normalized_text.lower() in ("export pdf", "/export pdf", "export", "/export"):
            log_event("export_pdf_detected")
            return {"export_pdf": True}
            
        # Check for help command
        help_match = re.match(FIELD_PATTERNS["help"], normalized_text, re.IGNORECASE)
        if help_match:
            topic = help_match.group(1) or help_match.group(2) or "general"
            log_event("help_requested", topic=topic)
            return {"help": topic.lower()}
            
        # Check for report type commands
        if re.match(FIELD_PATTERNS["summary"], normalized_text, re.IGNORECASE):
            log_event("summary_requested")
            return {"summary": True}
            
        if re.match(FIELD_PATTERNS["detailed"], normalized_text, re.IGNORECASE):
            log_event("detailed_requested")
            return {"detailed": True}

        # Split the text into commands
        commands = [cmd.strip() for cmd in re.split(r',\s*(?=(?:[^:]*:)|(?:add|insert)\s+(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments))|(?<!\w)\.\s*(?=[A-Z])', text) if cmd.strip()]
        log_event("commands_split", command_count=len(commands))
        
        processed_result = {
            "companies": [], "roles": [], "tools": [], "services": [],
            "activities": [], "issues": [], "people": []
        }
        seen_fields = set()

        for cmd in commands:
            # Process each command individually
            delete_match = re.match(FIELD_PATTERNS["delete"], cmd, re.IGNORECASE)
            if delete_match:
                # Handle deletion commands
                groups = delete_match.groups()
                
                # Different patterns for different delete syntaxes
                if groups[0]:  # First pattern: "delete category value"
                    raw_field = groups[0]
                    value = groups[1]
                elif groups[2] and groups[3]:  # Second pattern: "delete value from category"
                    raw_field = groups[3]
                    value = groups[2]
                else:  # Third pattern: "category delete value"
                    raw_field = groups[4]
                    value = groups[5]
                
                raw_field = raw_field.lower() if raw_field else None
                value = value.strip() if value else None
                field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
                
                log_event("delete_command", field=field, value=value)
                
                if field:
                    result.setdefault("delete", []).append({"field": field, "value": value})
                continue

            delete_entire_match = re.match(FIELD_PATTERNS["delete_entire"], cmd, re.IGNORECASE)
            if delete_entire_match:
                field = delete_entire_match.group(1).lower()
                mapped_field = FIELD_MAPPING.get(field, field)
                
                # Fix service/services mapping
                if mapped_field == "service":
                    mapped_field = "services"
                    
                result[mapped_field] = {"delete": True}
                log_event("delete_entire_category", field=mapped_field)
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
                        result.setdefault("correct", []).append({
                            "field": field, 
                            "old": clean_value(old_value, field), 
                            "new": clean_value(new_value, field)
                        })
                    else:
                        result["correct_prompt"] = {
                            "field": field, 
                            "value": clean_value(old_value, field)
                        }
                continue

            # Handle contextual references
            context_add_match = re.match(FIELD_PATTERNS["context_add"], cmd, re.IGNORECASE)
            if context_add_match:
                target_field = context_add_match.group(1).lower()
                field = FIELD_MAPPING.get(target_field, target_field)
                
                log_event("context_add_command", field=field)
                
                if field:
                    result["context_add"] = {"field": field}
                continue
                
            # Extract other fields using the command parser
            cmd_result = extract_single_command(cmd)
            if cmd_result.get("reset"):
                return {"reset": True}
                
            for key, value in cmd_result.items():
                # Skip fields we've already seen (except for list fields)
                if key in seen_fields and key not in LIST_FIELDS:
                    continue
                    
                seen_fields.add(key)
                
                # If this is a list field, add to the processed result
                if key in processed_result:
                    if isinstance(value, list):
                        processed_result[key].extend(value)
                    else:
                        processed_result[key].append(value)
                else:
                    result[key] = value

        # Process the collected list fields
        for field in processed_result:
            if processed_result[field]:
                # Get existing items (if any) from the result
                if field == "companies":
                    existing_items = [item["name"] for item in result.get(field, []) 
                                     if isinstance(item, dict) and "name" in item]
                elif field == "issues":
                    existing_items = [item["description"] for item in result.get(field, []) 
                                     if isinstance(item, dict) and "description" in item]
                elif field == "services":
                    existing_items = [item["task"] for item in result.get(field, []) 
                                     if isinstance(item, dict) and "task" in item]
                elif field == "tools":
                    existing_items = [item["item"] for item in result.get(field, []) 
                                     if isinstance(item, dict) and "item" in item]
                elif field == "roles":
                    existing_items = [f"{item['name']} ({item['role']})" for item in result.get(field, []) 
                                     if isinstance(item, dict) and "name" in item and "role" in item]
                elif field in ["people", "activities"]:
                    existing_items = result.get(field, [])
                else:
                    existing_items = []
                
                # Combine processed items with existing items
                if field == "companies":
                    result[field] = processed_result[field] + [{"name": i} for i in existing_items 
                                                             if isinstance(i, str)]
                elif field == "issues":
                    result[field] = processed_result[field] + [{"description": i} for i in existing_items 
                                                              if isinstance(i, str)]
                elif field == "services":
                    result[field] = processed_result[field] + [{"task": i} for i in existing_items 
                                                              if isinstance(i, str)]
                elif field == "tools":
                    result[field] = processed_result[field] + [{"item": i} for i in existing_items 
                                                              if isinstance(i, str)]
                elif field == "roles":
                    result[field] = processed_result[field] + [
                        {"name": i.split(' (')[0], "role": i.split(' (')[1].rstrip(')')} 
                        for i in existing_items if isinstance(i, str) and ' (' in i
                    ]
                elif field in ["people", "activities"]:
                    result[field] = processed_result[field] + existing_items
                else:
                    result[field] = processed_result[field]

        # Fix field naming consistency
        if "company" in result:
            result["companies"] = result.pop("company")
        if "service" in result:
            result["services"] = result.pop("service")
        if "tool" in result:
            result["tools"] = result.pop("tool")
            
        log_event("fields_extracted", result_fields=len(result))
        return result
    except Exception as e:
        log_event("extract_fields_error", input=text, error=str(e))
        # Return a minimal result to avoid breaking the app
        return {"error": str(e)}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def extract_single_command(text: str) -> Dict[str, Any]:
    """Extract a single command with enhanced parsing and error handling"""
    try:
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())
        log_event("extract_single_command", input=normalized_text)

        # Handle deletion commands
        delete_match = re.match(FIELD_PATTERNS["delete"], normalized_text, re.IGNORECASE)
        if delete_match:
            groups = delete_match.groups()
            
            # Different patterns for different delete syntaxes
            if groups[0]:  # First pattern: "delete category value"
                raw_field = groups[0]
                value = groups[1]
            elif groups[2] and groups[3]:  # Second pattern: "delete value from category"
                raw_field = groups[3]
                value = groups[2]
            else:  # Third pattern: "category delete value"
                raw_field = groups[4]
                value = groups[5]
                
            field = raw_field.lower() if raw_field else ""
            value = value.strip() if value else None
            mapped_field = FIELD_MAPPING.get(field, field)
            
            log_event("delete_command", raw_field=field, mapped_field=mapped_field, value=value)

            if field in ["architect", "engineer", "supervisor", "manager", "worker", 
                        "window installer", "contractor", "inspector", "electrician", 
                        "plumber", "foreman", "designer"]:
                # Handle deleting by role
                result.setdefault("delete", []).append({"field": "roles", "value": field})
                log_event("delete_role_command", field="roles", value=field)
            elif mapped_field == "people":
                # Handle deleting people
                result.setdefault("delete", []).append({"field": mapped_field, "value": value}) if value else result.setdefault("delete", []).append({"field": mapped_field, "value": None})
                log_event("delete_people_command", field=mapped_field, value=value)
            elif mapped_field == "person":
                # Handle deleting a person
                result.setdefault("delete", []).append({"field": "people", "value": value})
                log_event("delete_person_command", field="people", value=value)
            elif mapped_field in ["companies", "roles", "tools", "services", "service", "activities", "issues"]:
                # Fix service/services mapping
                if mapped_field == "service":
                    mapped_field = "services"
                    
                # Handle deleting list items
                result.setdefault("delete", []).append({"field": mapped_field, "value": value}) if value else result.setdefault("delete", []).append({"field": mapped_field, "value": None})
                log_event("delete_list_command", field=mapped_field, value=value)
            elif mapped_field in ["site_name", "segment", "category", "time", "weather", "impression", "comments"]:
                # Handle deleting scalar fields
                result.setdefault("delete", []).append({"field": mapped_field, "value": value}) if value else result.setdefault("delete", []).append({"field": mapped_field, "value": None})
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
            
            # Fix service/services mapping
            if mapped_field == "service":
                mapped_field = "services"
                
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
                    result.setdefault("correct", []).append({
                        "field": field, 
                        "old": clean_value(old_value, field), 
                        "new": clean_value(new_value, field)
                    })
                else:
                    result["correct_prompt"] = {
                        "field": field, 
                        "value": clean_value(old_value, field)
                    }
            return result

        # Handle other field extractions using regex patterns
        for raw_field, pattern in FIELD_PATTERNS.items():
            # Skip non-field patterns
            if raw_field in ["reset", "delete", "correct", "clear", "help", 
                           "undo_last", "context_add", "summary", "detailed", 
                           "delete_entire"]:
                continue
                
            match = re.match(pattern, normalized_text, re.IGNORECASE)
            if match:
                field = FIELD_MAPPING.get(raw_field, raw_field)
                log_event("field_matched", raw_field=raw_field, mapped_field=field)
                
                # Skip site_name matches that look like commands
                if field == "site_name" and re.search(r'\b(add|insert|delete|remove|correct|adjust|update|spell|none|as|role|new|reset)\b', normalized_text.lower()):
                    log_event("skipped_site_name", reason="command-like input")
                    continue
                    
                # Handle people field
                if field == "people":
                    name = clean_value(match.group(1), field)
                    role = clean_value(match.group(2), field) if match.group(2) else None
                    
                    # Skip if it's just "supervisor"
                    if name.lower() == "supervisor":
                        log_event("skipped_people_supervisor", reason="supervisor is a role")
                        continue
                        
                    result["people"] = [name]
                    
                    # If a role is specified, add it to roles as well
                    if role:
                        result["roles"] = [{"name": name, "role": role.title()}]
                        log_event("extracted_field", field="roles", name=name, role=role)
                        
                    log_event("extracted_field", field="people", value=name)
                
                # Handle role field
                elif field == "roles":
                    name = clean_value(match.group(1) or match.group(3), field)
                    role = (match.group(2) or match.group(4)).title()
                    
                    # Skip if it's just "supervisor"
                    if name.lower() == "supervisor":
                        log_event("skipped_role_supervisor", reason="supervisor is a role")
                        continue
                        
                    result["people"] = [name.strip()]
                    result["roles"] = [{"name": name.strip(), "role": role}]
                    log_event("extracted_field", field="roles", name=name, role=role)
                
                # Handle supervisor field
                elif field == "supervisor":
                    value = clean_value(match.group(1), field)
                    supervisor_names = [name.strip() for name in re.split(r'\s+and\s+|,', value) if name.strip()]
                    result["roles"] = [{"name": name, "role": "Supervisor"} for name in supervisor_names]
                    result["people"] = supervisor_names
                    log_event("extracted_field", field="roles", value=supervisor_names)
                
                # Handle company field
                elif field == "companies":
                    captured = clean_value(match.group(2) if match.group(2) else match.group(1), field)
                    company_names = [name.strip() for name in re.split(r'\s+and\s+|,', captured) if name.strip()]
                    result["companies"] = [{"name": name} for name in company_names]
                    log_event("extracted_field", field="companies", value=company_names)
                
                # Handle clear command
                elif field == "clear":
                    field_name = FIELD_MAPPING.get(match.group(1).lower(), match.group(1).lower())
                    result[field_name] = [] if field_name in LIST_FIELDS else ""
                    log_event("extracted_field", field=field_name, value="none")
                
                # Handle service/services field
                elif field in ["services", "service"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["services"] = []
                    else:
                        result["services"] = [{"task": value.strip()}]
                    log_event("extracted_field", field="services", value=value)
                
                # Handle tool/tools field
                elif field in ["tools", "tool"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["tools"] = []
                    else:
                        result["tools"] = [{"item": value.strip()}]
                    log_event("extracted_field", field="tools", value=value)
                
                # Handle issue field
                elif field == "issues":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["issues"] = []
                    else:
                        result["issues"] = [{"description": value.strip()}]
                    log_event("extracted_field", field="issues", value=value)
                
                # Handle activity field
                elif field == "activities":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["activities"] = []
                    else:
                        result["activities"] = [value.strip()]
                    log_event("extracted_field", field="activities", value=value)
                
                # Handle other fields
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
                model=CONFIG["OPENAI_MODEL"], 
                messages=messages, 
                temperature=CONFIG["OPENAI_TEMPERATURE"]
            )
            
            raw_response = response.choices[0].message.content
            log_event("gpt_response", response_length=len(raw_response))
            
            data = json.loads(raw_response)
            log_event("gpt_extracted", fields=list(data.keys()))
            
            # Clean the extracted data
            for field in SCALAR_FIELDS:
                if field in data and isinstance(data[field], str):
                    data[field] = clean_value(data[field], field)
                    
            for field in DICT_LIST_FIELDS:
                if field in data:
                    for item in data[field]:
                        if isinstance(item, dict):
                            if field == "tools" and "item" in item:
                                item["item"] = clean_value(item["item"], field)
                            elif field == "services" and "task" in item:
                                item["task"] = clean_value(item["task"], field)
                            elif field == "issues" and "description" in item:
                                item["description"] = clean_value(item["description"], field)
                            elif field == "companies" and "name" in item:
                                item["name"] = clean_value(item["name"], field)
                            elif field == "roles" and "name" in item:
                                item["name"] = clean_value(item["name"], field)
                                if "role" in item:
                                    item["role"] = clean_value(item["role"], field)
                                    
            if "activities" in data:
                data["activities"] = [clean_value(item, "activities") for item in data["activities"] 
                                     if isinstance(item, str)]
                
            # Ensure people list includes all role names
            if "roles" in data:
                for role in data["roles"]:
                    if isinstance(role, dict) and "name" in role:
                        person = clean_value(role["name"], "people")
                        if person and "people" in data:
                            if person not in data["people"]:
                                data["people"].append(person)
                        elif person:
                            data["people"] = [person]
            
            # If GPT couldn't extract anything, try some fallbacks
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
                    location = ", ".join(clean_value(part.strip().title(), "site_name") 
                                       for part in parts[2::2] if part.strip())
                    activity = clean_value(parts[0].strip(), "activities")
                    data = {"site_name": location, "activities": [activity]}
                    log_event("fallback_activity_site", data=data)
                else:
                    data = {"comments": clean_value(normalized_text.strip(), "comments")}
                    log_event("fallback_comments", data=data)
                    
            # Normalize field names
            if "company" in data:
                data["companies"] = data.pop("company")
            if "service" in data:
                data["services"] = data.pop("service")
            if "tool" in data:
                data["tools"] = data.pop("tool")
                
            return data
            
        except (json.JSONDecodeError, Exception) as e:
            log_event("gpt_extract_error", input=normalized_text, error=str(e))
            
            # Simple fallback when GPT fails
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
        return {}

def merge_data(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Merge new data into existing data with robust consistency checks"""
    try:
        merged = existing.copy()
        
        # Skip special command fields
        skipped_fields = ["reset", "undo", "undo_last", "status", "export_pdf", 
                        "correct_prompt", "delete", "correct", "help", "summary", 
                        "detailed", "context_add", "error"]
        
        for key, value in new.items():
            if key in skipped_fields:
                continue
                
            # Normalize field names
            if key == "company":
                key = "companies"
            elif key == "service":
                key = "services"
            elif key == "tool":
                key = "tools"
                
            # Handle list fields
            if key in LIST_FIELDS:
                # Clear the list if empty value provided
                if value == []:
                    merged[key] = []
                    log_event("cleared_list", field=key)
                    continue
                    
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else [value]
                
                # Handle different types of list fields
                if key in DICT_LIST_FIELDS:
                    # For companies, roles, tools, services, issues
                    for new_item in new_items:
                        # Skip if not a valid item
                        if isinstance(new_item, str):
                            if key == "issues":
                                new_item = {"description": new_item}
                            else:
                                continue
                                
                        if not isinstance(new_item, dict):
                            continue
                            
                        # Determine field to match on
                        if key == "companies" and "name" in new_item:
                            field_key = "name"
                            item_value = new_item.get("name", "")
                        elif key == "roles" and "name" in new_item:
                            field_key = "name"
                            item_value = new_item.get("name", "")
                        elif key == "issues" and "description" in new_item:
                            field_key = "description"
                            item_value = new_item.get("description", "")
                        elif key == "tools" and "item" in new_item:
                            field_key = "item"
                            item_value = new_item.get("item", "")
                        elif key == "services" and "task" in new_item:
                            field_key = "task"
                            item_value = new_item.get("task", "")
                        else:
                            continue
                            
                        # Check if we should replace an existing item
                        replaced = False
                        for i, existing_item in enumerate(existing_list):
                            if isinstance(existing_item, dict) and string_similarity(
                                existing_item.get(field_key, ""), item_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                existing_list[i] = new_item
                                replaced = True
                                log_event(f"replaced_{key}", old=existing_item.get(field_key), new=item_value)
                                break
                                
                        # Add as new item if not replaced
                        if not replaced:
                            existing_list.append(new_item)
                            log_event(f"added_{key}", **{field_key: item_value})
                            
                else:  # For simple list fields (people, activities)
                    for item in new_items:
                        if isinstance(item, str) and item not in existing_list and item.lower() != "supervisor":
                            existing_list.append(item)
                            log_event(f"added_{key}", value=item)
                            
                merged[key] = existing_list
                
            # Handle scalar fields
            else:
                if value == "" and key in SCALAR_FIELDS:
                    merged[key] = ""
                    log_event("cleared_field", field=key)
                elif value:
                    merged[key] = value
                    log_event("updated_field", field=key, value=value)
        
        # Data validation and cross-field consistency
        
        # Ensure role names are in people list
        if "roles" in merged:
            for role in merged["roles"]:
                if isinstance(role, dict) and "name" in role:
                    person_name = role["name"]
                    if person_name and person_name not in merged.get("people", []):
                        if "people" not in merged:
                            merged["people"] = []
                        merged["people"].append(person_name)
                        log_event("added_implied_person", name=person_name, role=role.get("role", "Unknown"))
        
        # Remove duplicates from list fields
        for field in LIST_FIELDS:
            if field in merged:
                if field in DICT_LIST_FIELDS:
                    # For dictionary lists, identify duplicates by the main field
                    field_key = (
                        "name" if field == "companies" else
                        "name" if field == "roles" else
                        "item" if field == "tools" else
                        "task" if field == "services" else
                        "description" if field == "issues" else
                        None
                    )
                    
                    if field_key:
                        # Create a new list without duplicates
                        unique_items = []
                        seen_values = set()
                        
                        for item in merged[field]:
                            if not isinstance(item, dict) or field_key not in item:
                                continue
                                
                            value = item[field_key].lower()
                            if value not in seen_values:
                                seen_values.add(value)
                                unique_items.append(item)
                                
                        if len(unique_items) < len(merged[field]):
                            log_event(f"removed_duplicates_{field}", 
                                     removed=len(merged[field]) - len(unique_items))
                            merged[field] = unique_items
                else:
                    # For simple lists, just remove duplicates
                    original_len = len(merged[field])
                    merged[field] = list(dict.fromkeys(merged[field]))  # Preserves order
                    if len(merged[field]) < original_len:
                        log_event(f"removed_duplicates_{field}", 
                                 removed=original_len - len(merged[field]))
        
        # Remove any "Supervisor" entries from people list
        if "people" in merged:
            original_len = len(merged["people"])
            merged["people"] = [p for p in merged["people"] if p.lower() != "supervisor"]
            if len(merged["people"]) < original_len:
                log_event("removed_supervisor_from_people", 
                         count=original_len - len(merged["people"]))
                
        # Remove any role entries without valid names
        if "roles" in merged:
            original_len = len(merged["roles"])
            merged["roles"] = [r for r in merged["roles"] 
                              if isinstance(r, dict) and r.get("name") and r.get("name").lower() != "supervisor"]
            if len(merged["roles"]) < original_len:
                log_event("removed_invalid_roles", 
                         count=original_len - len(merged["roles"]))
                         
        log_event("data_merged", fields=list(merged.keys()))
        return merged
    except Exception as e:
        log_event("merge_data_error", error=str(e))
        # Return the original data on error to avoid data loss
        return existing

def delete_entry(data: Dict[str, Any], field: str, value: Optional[str] = None) -> Dict[str, Any]:
    """Delete entries with improved partial matching for names"""
    try:
        log_event("delete_entry", field=field, value=value)
        
        # Fix service/services mapping
        if field == "service":
            field = "services"
            
        # Dictionary-based fields (companies, tools, services, issues)
        if field in ["companies", "tools", "services", "issues"]:
            field_key = (
                "name" if field == "companies" else 
                "item" if field == "tools" else 
                "task" if field == "services" else 
                "description" if field == "issues" else 
                None
            )
            
            if value:
                # If a specific value is provided, try to find and delete matching item
                before_count = len(data[field])
                
                # For each item, check if the field value matches the provided value
                data[field] = [
                    item for item in data[field] 
                    if not (isinstance(item, dict) and 
                           string_similarity(item.get(field_key, ""), value) > CONFIG["NAME_SIMILARITY_THRESHOLD"])
                ]
                
                deleted_count = before_count - len(data[field])
                log_event(f"{field}_deleted", value=value, count=deleted_count)
            else:
                # If no value is provided, clear the entire list
                data[field] = []
                log_event(f"{field}_cleared")
                
        # Special handling for roles
        elif field == "roles":
            if value:
                before_count = len(data[field])
                
                # Find the people associated with these roles to also remove from people list
                people_to_remove = set()
                for item in data[field]:
                    if (isinstance(item, dict) and 
                        string_similarity(item.get("name", ""), value) > CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                        people_to_remove.add(item.get("name", ""))
                
                # Remove matching roles
                data[field] = [
                    item for item in data[field] 
                    if not (isinstance(item, dict) and 
                           string_similarity(item.get("name", ""), value) > CONFIG["NAME_SIMILARITY_THRESHOLD"])
                ]
                
                # Also remove the people from the people list
                if "people" in data and people_to_remove:
                    original_people = len(data["people"])
                    data["people"] = [
                        p for p in data.get("people", []) 
                        if not any(string_similarity(p, name) > CONFIG["NAME_SIMILARITY_THRESHOLD"] for name in people_to_remove)
                    ]
                    log_event("associated_people_removed", 
                             count=original_people - len(data["people"]), 
                             names=list(people_to_remove))
                
                deleted_count = before_count - len(data[field])
                log_event(f"{field}_deleted", value=value, count=deleted_count)
            else:
                # Clear both roles and people lists
                data[field] = []
                data["people"] = []
                log_event(f"{field}_and_people_cleared")
                
        # Special handling for people
        elif field == "people":
            if value:
                # Handle partial name matching for people
                name_match = None
                for person in data.get("people", []):
                    if string_similarity(person, value) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                        name_match = person
                        break
                
                if name_match:
                    # Remove the matched person
                    data["people"] = [p for p in data.get("people", []) if p != name_match]
                    log_event("person_deleted", name=name_match)
                    
                    # Also remove any roles assigned to this person
                    if "roles" in data:
                        original_roles = len(data["roles"])
                        data["roles"] = [
                            r for r in data.get("roles", []) 
                            if not (isinstance(r, dict) and 
                                   string_similarity(r.get("name", ""), name_match) > CONFIG["NAME_SIMILARITY_THRESHOLD"])
                        ]
                        removed_roles = original_roles - len(data["roles"])
                        if removed_roles > 0:
                            log_event("associated_roles_removed", count=removed_roles, person=name_match)
                else:
                    # If no exact match, try to filter by similarity
                    before_count = len(data.get("people", []))
                    data["people"] = [
                        p for p in data.get("people", []) 
                        if string_similarity(p, value) <= CONFIG["NAME_SIMILARITY_THRESHOLD"]
                    ]
                    deleted_count = before_count - len(data.get("people", []))
                    
                    # Also remove any matching roles
                    if "roles" in data:
                        original_roles = len(data["roles"])
                        data["roles"] = [
                            r for r in data.get("roles", []) 
                            if not (isinstance(r, dict) and 
                                   string_similarity(r.get("name", ""), value) > CONFIG["NAME_SIMILARITY_THRESHOLD"])
                        ]
                        removed_roles = original_roles - len(data["roles"])
                        if removed_roles > 0:
                            log_event("associated_roles_removed", count=removed_roles, search=value)
                            
                    log_event("people_deleted_by_similarity", value=value, count=deleted_count)
            else:
                # Clear both people and roles lists
                data["people"] = []
                data["roles"] = []
                log_event("people_and_roles_cleared")
                
        # Special handling for activities
        elif field == "activities":
            if value:
                # Handle partial activity matching
                before_count = len(data.get("activities", []))
                data["activities"] = [
                    activity for activity in data.get("activities", []) 
                    if string_similarity(activity, value) <= CONFIG["NAME_SIMILARITY_THRESHOLD"]
                ]
                deleted_count = before_count - len(data.get("activities", []))
                log_event("activities_deleted", value=value, count=deleted_count)
            else:
                # Clear the entire activities list
                data["activities"] = []
                log_event("activities_cleared")
                
        # Scalar fields
        elif field in SCALAR_FIELDS:
            if value:
                # Only clear if the value matches closely
                if string_similarity(data.get(field, ""), value) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                    data[field] = ""
                    log_event(f"{field}_cleared_matching", value=value)
                else:
                    log_event(f"{field}_no_match", value=value, current=data.get(field, ""))
            else:
                # Clear the field
                data[field] = ""
                log_event(f"{field}_cleared")
        
        # Final data cleanup and validation
        
        # Ensure people in roles are also in people list
        if "roles" in data:
            for role in data.get("roles", []):
                if isinstance(role, dict) and "name" in role:
                    person_name = role["name"]
                    if person_name and "people" in data and person_name not in data["people"]:
                        data["people"].append(person_name)
                        log_event("re_added_person_from_role", name=person_name)
        
        # Validate people list against roles
        if "people" in data and "roles" in data:
            role_names = {r.get("name") for r in data["roles"] if isinstance(r, dict) and "name" in r}
            people_not_in_roles = [p for p in data["people"] if p not in role_names]
            
            # No need to log, this is normal - not everyone needs a role
            
        # Remove any "Supervisor" entries from people list
        if "people" in data:
            original_len = len(data["people"])
            data["people"] = [p for p in data["people"] if p.lower() != "supervisor"]
            if len(data["people"]) < original_len:
                log_event("removed_supervisor_from_people", 
                         count=original_len - len(data["people"]))
        
        # Convert service to services for consistency
        if "service" in data and "services" not in data:
            data["services"] = data.pop("service")
            log_event("normalized_service_field")
        
        log_event("data_after_deletion", fields=list(data.keys()))
        return data
    except Exception as e:
        log_event("delete_entry_error", field=field, error=str(e))
        # Return original data on error to avoid data loss
        return data

# --- Command Handlers ---
COMMAND_HANDLERS: Dict[str, Callable[[str, Dict[str, Any]], None]] = {}

def command(name: str) -> Callable:
    """Decorator for registering command handlers"""
    def decorator(func: Callable) -> Callable:
        COMMAND_HANDLERS[name] = func
        return func
    return decorator

@command("reset")
def handle_reset(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle reset command to start a new report"""
    session["structured_data"] = blank_report()
    session["command_history"].clear()
    session["last_change_history"].clear()
    session["context"] = {
        "last_mentioned_person": None,
        "last_mentioned_item": None,
        "last_field": None,
    }
    save_session(session_data)
    summary = summarize_report(session["structured_data"])
    send_message(chat_id, f"**Report reset**\n\n{summary}\n\nSpeak or type your first field (e.g., 'add site Downtown Project').")

@command("undo")
def handle_undo(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle undo command to revert to previous state"""
    if session["command_history"]:
        session["structured_data"] = session["command_history"].pop()
        save_session(session_data)
        summary = summarize_report(session["structured_data"])
        send_message(chat_id, f"**Undo successful**\n\n{summary}")
    else:
        send_message(chat_id, "Nothing to undo. Your report is at its initial state.")

@command("undo last")
def handle_undo_last(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle undo last change command"""
    if session["last_change_history"]:
        field, original_value = session["last_change_history"].pop()
        
        if field in LIST_FIELDS:
            session["structured_data"][field] = original_value
            log_event("undo_last_change", field=field)
        elif field in SCALAR_FIELDS:
            session["structured_data"][field] = original_value
            log_event("undo_last_change", field=field)
        
        save_session(session_data)
        summary = summarize_report(session["structured_data"])
        send_message(chat_id, f"**Last change undone for {field}**\n\n{summary}")
    else:
        send_message(chat_id, "No recent changes found to undo.")

@command("status")
def handle_status(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle status command to show current report state"""
    summary = summarize_report(session["structured_data"])
    send_message(chat_id, f"**Current report status**\n\n{summary}")

@command("export")
@command("export pdf")
def handle_export(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle PDF export command"""
    # Use detailed format by default
    report_type = session.get("report_format", "detailed")
    
    pdf_buffer = generate_pdf(session["structured_data"], report_type)
    if pdf_buffer:
        if send_pdf(chat_id, pdf_buffer, report_type):
            send_message(chat_id, "PDF report sent successfully!")
        else:
            send_message(chat_id, "⚠️ Failed to send PDF report. Please try again.")
    else:
        send_message(chat_id, "⚠️ Failed to generate PDF report. Please check your report data.")

@command("summary")
def handle_summary(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle summary report command"""
    session["report_format"] = "summary"
    save_session(session_data)
    
    # Generate and send summary report
    pdf_buffer = generate_pdf(session["structured_data"], "summary")
    if pdf_buffer:
        if send_pdf(chat_id, pdf_buffer, "summary"):
            send_message(chat_id, "Summary report format set. PDF summary report sent successfully!")
        else:
            send_message(chat_id, "⚠️ Summary report format set, but failed to send PDF. Type 'export' to try again.")
    else:
        send_message(chat_id, "Summary report format set for future exports.")

@command("detailed")
def handle_detailed(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle detailed report command"""
    session["report_format"] = "detailed"
    save_session(session_data)
    
    # Generate and send detailed report
    pdf_buffer = generate_pdf(session["structured_data"], "detailed")
    if pdf_buffer:
        if send_pdf(chat_id, pdf_buffer, "detailed"):
            send_message(chat_id, "Detailed report format set. PDF detailed report sent successfully!")
        else:
            send_message(chat_id, "⚠️ Detailed report format set, but failed to send PDF. Type 'export' to try again.")
    else:
        send_message(chat_id, "Detailed report format set for future exports.")

@command("help")
def handle_help(chat_id: str, session: Dict[str, Any], topic: str = "general") -> None:
    """Handle help command with optional topic"""
    help_text = {
        "general": (
            "**Construction Site Report Bot Help**\n\n"
            "This bot helps you create structured construction site reports.\n\n"
            "**Basic Commands:**\n"
            "• Add information: 'add site Central Plaza'\n"
            "• Delete information: 'delete John from people' or 'tools: none'\n"
            "• Correct information: 'correct site Central Plaza to Downtown Project'\n"
            "• Export report: 'export pdf'\n"
            "• Reset report: 'reset' or 'new report'\n"
            "• Undo changes: 'undo' or 'undo last'\n"
            "• Get status: 'status'\n\n"
            "For help on specific topics, type 'help [topic]' where topic can be: fields, commands, adding, deleting, examples"
        ),
        "fields": (
            "**Available Fields**\n\n"
            "• site_name - Project location (e.g., 'Downtown Project')\n"
            "• segment - Section number/identifier\n"
            "• category - Project category\n"
            "• companies - Companies involved\n"
            "• people - People on site\n"
            "• roles - Person's roles on site\n"
            "• tools - Equipment used\n"
            "• services - Services provided\n"
            "• activities - Work performed\n"
            "• issues - Problems encountered\n"
            "• time - Duration spent\n"
            "• weather - Weather conditions\n"
            "• impression - Overall impression\n"
            "• comments - Additional notes"
        ),
        "commands": (
            "**Available Commands**\n\n"
            "• status - View current report\n"
            "• reset/new report - Start over\n"
            "• undo - Revert last major change\n"
            "• undo last - Revert last field change\n"
            "• export/export pdf - Generate PDF report\n"
            "• summary - Generate summary report\n"
            "• detailed - Generate detailed report\n"
            "• help - Show this help\n"
            "• help [topic] - Show topic-specific help"
        ),
        "adding": (
            "**Adding Information**\n\n"
            "Add field information using these formats:\n\n"
            "• 'add site Downtown Project'\n"
            "• 'site: Downtown Project'\n"
            "• 'companies: BuildRight AG, ElectricFlow GmbH'\n"
            "• 'people: Anna Keller, John Smith'\n"
            "• 'Anna Keller as Supervisor'\n"
            "• 'tools: mobile crane, welding equipment'\n"
            "• 'activities: laying foundations, setting up scaffolding'\n"
            "• 'issues: power outage at 10 AM'\n"
            "• 'weather: cloudy with intermittent rain'\n"
            "• 'comments: ensure safety protocols are reinforced'"
        ),
        "deleting": (
            "**Deleting Information**\n\n"
            "Delete field information using these formats:\n\n"
            "• Clear a field entirely: 'tools: none'\n"
            "• Delete specific item: 'delete mobile crane from tools'\n"
            "• Remove a person: 'delete Anna from people'\n"
            "• Alternative syntax: 'delete tools mobile crane'\n"
            "• Alternative syntax: 'tools delete mobile crane'\n"
            "• Clear entire category: 'delete entire category tools'\n\n"
            "When removing a person, their role will also be removed automatically."
        ),
        "examples": (
            "**Example Report Creation**\n\n"
            "1. 'site: Central Plaza'\n"
            "2. 'segment: 5'\n"
            "3. 'companies: BuildRight AG, ElectricFlow GmbH'\n"
            "4. 'Anna Keller as Supervisor'\n"
            "5. 'John Smith as Worker'\n"
            "6. 'tools: mobile crane, welding equipment'\n"
            "7. 'services: electrical wiring, HVAC installation'\n"
            "8. 'activities: laying foundations, setting up scaffolding'\n"
            "9. 'issues: power outage at 10 AM caused a 2-hour delay'\n"
            "10. 'weather: cloudy with rain'\n"
            "11. 'time: full day'\n"
            "12. 'impression: productive despite setbacks'\n"
            "13. 'comments: ensure safety protocols are reinforced'\n"
            "14. 'export pdf' to generate the report"
        )
    }
    
    # Get the appropriate help text
    message = help_text.get(topic.lower(), help_text["general"])
    send_message(chat_id, message)

# Add aliases for existing commands
COMMAND_HANDLERS["new"] = handle_reset
COMMAND_HANDLERS["new report"] = handle_reset
COMMAND_HANDLERS["/new"] = handle_reset
COMMAND_HANDLERS["/reset"] = handle_reset
COMMAND_HANDLERS["/status"] = handle_status
COMMAND_HANDLERS["/undo"] = handle_undo
COMMAND_HANDLERS["/export"] = handle_export
COMMAND_HANDLERS["/help"] = handle_help
COMMAND_HANDLERS["undo last change"] = handle_undo_last
COMMAND_HANDLERS["summarize"] = handle_summary

# --- Flask App ---
app = Flask(__name__)

def suggest_corrections(input_text: str) -> Optional[str]:
    """Suggest corrections for common errors in commands"""
    # Common command suggestions
    command_suggestions = {
        r'\b(delet|remov|dlt)\b': "delete",
        r'\b(statu|stat)\b': "status",
        r'\b(undoe|undo last)\b': "undo",
        r'\badd (peopl|peole|persons)\b': "add people",
        r'\badd (compan|comp)\b': "add companies",
        r'\b(rest|rst|clear all)\b': "reset",
        r'\b(exprt|export pdf|make pdf|generate pdf)\b': "export",
        r'\b(site nam|site name|location)\b': "site",
    }
    
    # Check if input closely matches any known command
    for pattern, suggestion in command_suggestions.items():
        if re.search(pattern, input_text, re.IGNORECASE):
            return f"Did you mean '{suggestion}'?"
    
    # Check if this looks like a field command but is malformed
    field_pattern = r'\b(add|site|segment|category|company|people|role|tool|service|activity|issue|time|weather|impression|comment)\b'
    if re.search(field_pattern, input_text, re.IGNORECASE):
        # Check if the syntax is likely incorrect
        if ":" not in input_text and not re.search(r'\s+as\s+', input_text, re.IGNORECASE):
            field_match = re.search(field_pattern, input_text, re.IGNORECASE)
            if field_match:
                field = field_match.group(1).lower()
                if field == "company":
                    field = "companies"
                elif field == "comment":
                    field = "comments"
                elif field == "tool":
                    field = "tools"
                elif field == "service":
                    field = "services"
                elif field == "activity":
                    field = "activities"
                elif field == "issue":
                    field = "issues"
                
                example = f"{field}: " if not input_text.startswith("add") else f"add {field} "
                return f"Try using '{example}[value]' format"
    
    # If nothing matches, return None
    return None

def process_context_reference(text: str, session: Dict[str, Any]) -> str:
    """Process contextual references in text"""
    context = session.get("context", {})
    
    # Replace person references
    if re.search(CONTEXTUAL_PATTERNS["reference_person"], text, re.IGNORECASE):
        if context.get("last_mentioned_person"):
            text = re.sub(CONTEXTUAL_PATTERNS["reference_person"], 
                         context["last_mentioned_person"], 
                         text, flags=re.IGNORECASE)
    
    # Replace thing references
    if re.search(CONTEXTUAL_PATTERNS["reference_thing"], text, re.IGNORECASE):
        if context.get("last_mentioned_item"):
            text = re.sub(CONTEXTUAL_PATTERNS["reference_thing"], 
                         context["last_mentioned_item"], 
                         text, flags=re.IGNORECASE)
    
    # Handle "last mentioned" references
    if re.search(CONTEXTUAL_PATTERNS["last_mentioned"], text, re.IGNORECASE):
        if context.get("last_field"):
            text = re.sub(CONTEXTUAL_PATTERNS["last_mentioned"], 
                         context["last_field"], 
                         text, flags=re.IGNORECASE)
    
    return text

def update_context(session: Dict[str, Any], field: Optional[str], value: Optional[str]) -> None:
    """Update the conversation context"""
    if not field or not value:
        return
        
    context = session.get("context", {
        "last_mentioned_person": None,
        "last_mentioned_item": None,
        "last_field": None,
    })
    
    # Update last field
    context["last_field"] = field
    
    # Update person context if relevant
    if field == "people" or field == "roles":
        context["last_mentioned_person"] = value
    
    # Update item context if relevant
    if field in ["tools", "services", "activities", "issues"]:
        context["last_mentioned_item"] = value
    
    session["context"] = context

def handle_command(chat_id: str, text: str, sess: Dict[str, Any]) -> tuple[str, int]:
    """Main command handler with enhanced error handling and context awareness"""
    try:
        normalized_text = text.strip().lower() if text else ""
        if not normalized_text:
            send_message(chat_id, "⚠️ Empty input. Please provide a command (e.g., 'add site Downtown Project').")
            return "ok", 200

        # Process contextual references
        if any(re.search(pattern, normalized_text, re.IGNORECASE) for pattern in CONTEXTUAL_PATTERNS.values()):
            text = process_context_reference(text, sess)
            log_event("processed_context_reference", original=normalized_text, processed=text)

        current_time = time()
        
        # Check for session timeout
        if (current_time - sess.get("last_interaction", 0) > CONFIG["PAUSE_THRESHOLD"] and
                normalized_text not in ("yes", "no", "new", "new report", "reset", "reset report", "/new", "existing", "continue")):
            sess["pending_input"] = text
            sess["awaiting_reset_confirmation"] = True
            sess["last_interaction"] = current_time
            save_session(session_data)
            send_message(chat_id, "It's been a while! Would you like to reset the report or continue with the existing one? Reply 'yes' to reset or 'no' to continue.")
            return "ok", 200

        sess["last_interaction"] = current_time

        # Handle direct commands
        if normalized_text in COMMAND_HANDLERS:
            COMMAND_HANDLERS[normalized_text](chat_id, sess)
            return "ok", 200

        # Handle help command with topic
        help_match = re.match(FIELD_PATTERNS["help"], normalized_text, re.IGNORECASE)
        if help_match:
            topic = (help_match.group(1) or help_match.group(2) or "general").lower()
            handle_help(chat_id, sess, topic)
            return "ok", 200

        # Handle reset command
        if normalized_text in ("new", "new report", "reset", "reset report", "/new"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session(session_data)
            send_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        # Handle clear command (field: none)
        clear_match = re.match(FIELD_PATTERNS["clear"], text, re.IGNORECASE)
        if clear_match:
            raw_field = clear_match.group(1).lower() if clear_match.group(1) else None
            field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
            
            if not field:
                log_event("clear_command_error", text=text, error="Invalid field")
                send_message(chat_id, f"⚠️ Invalid clear command: '{text}'. Try 'tools: none' or 'issues: none'.")
                return "ok", 200
                
            # Save the original value for undo last
            if field in sess["structured_data"]:
                sess["last_change_history"].append((field, sess["structured_data"][field]))
                
            # Save current state for undo
            sess["command_history"].append(sess["structured_data"].copy())
            
            # Clear the field
            sess["structured_data"] = delete_entry(sess["structured_data"], field)
            save_session(session_data)
            
            # Update context
            update_context(sess, field, "none")
            
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Cleared {field}\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Extract fields from the input
        extracted = extract_fields(text)
        
        # Handle reset command
        if extracted.get("reset"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session(session_data)
            send_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200
            
        # Handle help command
        if "help" in extracted:
            topic = extracted["help"]
            handle_help(chat_id, sess, topic)
            return "ok", 200
            
        # Handle special report commands
        if extracted.get("summary"):
            handle_summary(chat_id, sess)
            return "ok", 200
            
        if extracted.get("detailed"):
            handle_detailed(chat_id, sess)
            return "ok", 200
            
        # Handle undo last command
        if extracted.get("undo_last"):
            handle_undo_last(chat_id, sess)
            return "ok", 200
            
        # Handle spelling correction prompts
        if extracted.get("correct_prompt"):
            field = extracted["correct_prompt"]["field"]
            value = extracted["correct_prompt"]["value"]
            sess["awaiting_spelling_correction"] = (field, value)
            save_session(session_data)
            send_message(chat_id, f"Please provide the correct spelling for '{value}' in {field}.")
            return "ok", 200
            
        # Handle delete commands
        if extracted.get("delete"):
            sess["command_history"].append(sess["structured_data"].copy())
            
            for delete_cmd in extracted["delete"]:
                field = delete_cmd["field"]
                value = delete_cmd["value"]
                
                # Save the original value for undo last
                if field in sess["structured_data"]:
                    sess["last_change_history"].append((field, sess["structured_data"][field]))
                
                # Fix service/services mapping
                if field == "service":
                    field = "services"
                    
                # Delete the entry
                sess["structured_data"] = delete_entry(sess["structured_data"], field, value)
                
                # Update context
                if value:
                    update_context(sess, field, f"deleted {value}")
                else:
                    update_context(sess, field, "deleted")
            
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            
            # Use the last delete command for the confirmation message
            last_field = extracted["delete"][-1]["field"]
            last_value = extracted["delete"][-1]["value"]
            
            send_message(chat_id, f"Removed {last_field}" + (f": {last_value}" if last_value else "") + f"\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200
            
        # Handle correction commands
        if extracted.get("correct"):
            sess["command_history"].append(sess["structured_data"].copy())
            
            for correct_cmd in extracted["correct"]:
                field = correct_cmd["field"]
                old_value = correct_cmd["old"]
                new_value = correct_cmd["new"]
                
                # Save the original value for undo last
                if field in sess["structured_data"]:
                    sess["last_change_history"].append((field, sess["structured_data"][field]))
                
                # Handle different field types
                if field in ["companies", "roles", "tools", "services", "issues"]:
                    data_field = (
                        "name" if field == "companies" else
                        "description" if field == "issues" else
                        "item" if field == "tools" else
                        "task" if field == "services" else
                        "name" if field == "roles" else None
                    )
                    
                    # Handle service/services correctly
                    if field == "service":
                        field = "services"
                        data_field = "task"
                        
                    # Make sure we're working with the services field if needed
                    if field == "services" and "service" in sess["structured_data"] and "services" not in sess["structured_data"]:
                        sess["structured_data"]["services"] = sess["structured_data"].pop("service")
                    
                    sess["structured_data"][field] = [
                        {data_field: new_value if string_similarity(item.get(data_field, ""), old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] else item[data_field],
                         **({} if field != "roles" else {"role": item["role"]})}
                        for item in sess["structured_data"].get(field, [])
                        if isinstance(item, dict)
                    ]
                    
                    # Ensure people list includes roles
                    if field == "roles" and new_value not in sess["structured_data"].get("people", []):
                        sess["structured_data"].setdefault("people", []).append(new_value)
                        
                elif field in ["people"]:
                    # Update people and associated roles
                    sess["structured_data"]["people"] = [
                        new_value if string_similarity(item, old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] else item 
                        for item in sess["structured_data"].get("people", [])
                    ]
                    
                    # Also update roles to maintain consistency
                    sess["structured_data"]["roles"] = [
                        {"name": new_value, "role": role["role"]} 
                        if string_similarity(role.get("name", ""), old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] 
                        else role
                        for role in sess["structured_data"].get("roles", [])
                    ]
                    
                elif field in ["activities"]:
                    # Update activities
                    sess["structured_data"]["activities"] = [
                        new_value if string_similarity(item, old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] else item 
                        for item in sess["structured_data"].get("activities", [])
                    ]
                    
                else:
                    # Update scalar field
                    sess["structured_data"][field] = new_value
                    
                # Update context
                update_context(sess, field, new_value)
                    
                log_event(f"{field}_corrected", old=old_value, new=new_value)
                
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            
            # Use the last correction for the confirmation message
            last_field = extracted["correct"][-1]["field"]
            last_old = extracted["correct"][-1]["old"]
            last_new = extracted["correct"][-1]["new"]
            
            send_message(chat_id, f"Corrected {last_field} from '{last_old}' to '{last_new}'.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200
            
        # Handle contextual add commands
        if extracted.get("context_add"):
            target_field = extracted["context_add"]["field"]
            
            # Try to extract the relevant context item
            context = sess.get("context", {})
            value_to_add = None
            
            if target_field in ["people", "roles"] and context.get("last_mentioned_person"):
                value_to_add = context["last_mentioned_person"]
            elif target_field in ["tools", "services", "activities", "issues"] and context.get("last_mentioned_item"):
                value_to_add = context["last_mentioned_item"]
                
            if value_to_add:
                # Save current state for undo
                sess["command_history"].append(sess["structured_data"].copy())
                
                # Create a new data dictionary with just this field
                field_data = {}
                
                if target_field == "people":
                    field_data["people"] = [value_to_add]
                elif target_field == "roles" and context.get("last_mentioned_person"):
                    # Default role is "Worker" if not specified
                    field_data["roles"] = [{"name": context["last_mentioned_person"], "role": "Worker"}]
                    field_data["people"] = [context["last_mentioned_person"]]
                elif target_field == "activities":
                    field_data["activities"] = [value_to_add]
                elif target_field == "tools":
                    field_data["tools"] = [{"item": value_to_add}]
                elif target_field == "services":
                    field_data["services"] = [{"task": value_to_add}]
                elif target_field == "issues":
                    field_data["issues"] = [{"description": value_to_add}]
                elif target_field in SCALAR_FIELDS:
                    field_data[target_field] = value_to_add
                
                # Merge the new data
                sess["structured_data"] = merge_data(sess["structured_data"], field_data)
                save_session(session_data)
                
                tpl = summarize_report(sess["structured_data"])
                send_message(chat_id, f"Added '{value_to_add}' to {target_field}.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
                return "ok", 200
            else:
                send_message(chat_id, f"I couldn't find a relevant item to add to {target_field}. Please specify what you want to add.")
                return "ok", 200
                
        # Check if any fields were extracted
        if not any(k in extracted for k in LIST_FIELDS + SCALAR_FIELDS + ["export_pdf"]):
            log_event("unrecognized_input", input=text)
            
            # Try to suggest corrections
            suggestion = suggest_corrections(text)
            if suggestion:
                send_message(chat_id, f"⚠️ Unrecognized input: '{text}'. {suggestion}")
            else:
                send_message(chat_id, f"⚠️ Unrecognized input: '{text}'. Try 'add site Downtown Project', 'add issue power outage', or 'help' for assistance.")
                
            return "ok", 200

        # Handle normal field updates
        sess["command_history"].append(sess["structured_data"].copy())
        
        # Save original values for all fields that will be modified
        for field in (set(extracted.keys()) & set(LIST_FIELDS + SCALAR_FIELDS)):
            if field in sess["structured_data"]:
                sess["last_change_history"].append((field, sess["structured_data"][field]))
                
        # Update context from extracted fields
        for field in extracted:
            if field in LIST_FIELDS + SCALAR_FIELDS:
                value = extracted[field]
                if isinstance(value, list) and value:
                    update_context(sess, field, str(value[0]))
                elif not isinstance(value, list) and value:
                    update_context(sess, field, str(value))
        
        # Merge the extracted data
        sess["structured_data"] = merge_data(sess["structured_data"], enrich_date(extracted))
        save_session(session_data)
        
        # Handle export_pdf command
        if extracted.get("export_pdf"):
            handle_export(chat_id, sess)
            return "ok", 200
        
        tpl = summarize_report(sess["structured_data"])
        send_message(chat_id, f"✅ Updated report:\n\n{tpl}\n\nAnything else to add or correct?")
        return "ok", 200
        
    except Exception as e:
        log_event("handle_command_error", error=str(e))
        send_message(chat_id, "⚠️ An error occurred while processing your command. Please try again or type 'help' for assistance.")
        return "error", 500

@app.route("/webhook", methods=["POST"])
def webhook() -> tuple[str, int]:
    """Handle Telegram webhook requests"""
    try:
        data = request.get_json(force=True)
        log_event("webhook_received")
        
        if not data or "message" not in data:
            log_event("no_message")
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        log_event("message_received", chat_id=chat_id, text_length=len(text))

        # Initialize session for new users
        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False,
                "last_interaction": time(),
                "pending_input": None,
                "awaiting_reset_confirmation": False,
                "command_history": deque(maxlen=CONFIG["MAX_HISTORY"]),
                "awaiting_spelling_correction": None,
                "last_change_history": [],
                "context": {
                    "last_mentioned_person": None,
                    "last_mentioned_item": None,
                    "last_field": None,
                },
                "report_format": "detailed"
            }
            log_event("session_created", chat_id=chat_id)
            send_message(chat_id, "👋 Welcome to the Construction Site Report Bot!\n\nI'll help you create detailed construction site reports easily. Start by adding information about your site (e.g., 'add site Downtown Project').\n\nType 'help' for more information on available commands.")
            
        sess = session_data[chat_id]

        # Clean up invalid entries from structured data
        if "Supervisor" in sess["structured_data"].get("people", []):
            sess["structured_data"]["people"] = [p for p in sess["structured_data"].get("people", []) if p != "Supervisor"]
            log_event("removed_supervisor_entry", chat_id=chat_id)
            
        if "roles" in sess["structured_data"]:
            sess["structured_data"]["roles"] = [r for r in sess["structured_data"].get("roles", []) if r.get("name") != "Supervisor"]
            
        # Normalize field names
        _normalize_field_names(sess["structured_data"])

        # Handle voice messages
        if "voice" in msg:
            text, confidence = transcribe_voice(msg["voice"]["file_id"])
            if not text:
                send_message(chat_id, "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'add site Downtown Project') or type your command.")
                return "ok", 200
                
            log_event("transcribed_voice", text=text, confidence=confidence)
            
            # Ask for confirmation if confidence is low
            if confidence < 0.7:
                sess["pending_input"] = text
                sess["awaiting_voice_confirmation"] = True
                sess["last_interaction"] = time()
                save_session(session_data)
                send_message(chat_id, f"I heard:\n\n\"{text}\"\n\nIs this correct? Reply 'yes' to proceed or 'no' to try again.")
                return "ok", 200

        # Handle reset confirmation
        if sess.get("awaiting_reset_confirmation", False):
            normalized_text = re.sub(r’[.!?]\s*$’, ‘‘, text.strip()).lower()
            log_event("reset_confirmation", text=normalized_text)
            
            if normalized_text in ("yes", "new", "new report", "y"):
                # User confirmed reset
                sess["structured_data"] = blank_report()
                sess["awaiting_correction"] = False
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["command_history"].clear()
                sess["last_change_history"].clear()
                sess["context"] = {
                    "last_mentioned_person": None,
                    "last_mentioned_item": None,
                    "last_field": None,
                }
                save_session(session_data)
                
                tpl = summarize_report(sess["structured_data"])
                send_message(chat_id, f"**Starting a fresh report**\n\n{tpl}\n\nSpeak or type your first field (e.g., 'add site Downtown Project').")
                return "ok", 200
                
            elif normalized_text in ("no", "existing", "continue", "n"):
                # User wants to continue with existing report
                preserved_input = sess["pending_input"]
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["last_interaction"] = time()
                save_session(session_data)
                
                # Only process the preserved input if it's not a reset command itself
                preserved_normalized = preserved_input.strip().lower() if preserved_input else ""
                if preserved_normalized and preserved_normalized not in ("new", "new report", "reset", "reset report", "/new"):
                    log_event("proceeding_with_existing_input", input=preserved_input)
                    return handle_command(chat_id, preserved_input, sess)
                else:
                    # Just show the current status if no valid preserved input
                    summary = summarize_report(sess["structured_data"])
                    send_message(chat_id, f"**Continuing with existing report**\n\n{summary}")
                    return "ok", 200
            else:
                # Unclear response
                send_message(chat_id, "Please clearly indicate if you want to reset the report. Reply 'yes' to reset or 'no' to continue with the existing report.")
                return "ok", 200

        # Handle voice transcription confirmation
        if sess.get("awaiting_voice_confirmation", False):
            normalized_text = re.sub(r’[.!?]\s*$’, ‘‘, text.strip()).lower()
            
            if normalized_text in ("yes", "y", "correct", "proceed"):
                # Transcription is correct, process the command
                preserved_input = sess["pending_input"]
                sess["awaiting_voice_confirmation"] = False
                sess["pending_input"] = None
                save_session(session_data)
                
                log_event("voice_transcription_confirmed", input=preserved_input)
                return handle_command(chat_id, preserved_input, sess)
                
            elif normalized_text in ("no", "n", "incorrect", "wrong"):
                # Transcription is incorrect
                sess["awaiting_voice_confirmation"] = False
                sess["pending_input"] = None
                save_session(session_data)
                
                send_message(chat_id, "Please try speaking again more clearly, or type your command instead.")
                return "ok", 200
                
            else:
                # Unclear response
                send_message(chat_id, "Please clearly indicate if the transcription is correct. Reply 'yes' to proceed or 'no' to try again.")
                return "ok", 200

        # Handle spelling correction
        if sess.get("awaiting_spelling_correction"):
            field, old_value = sess["awaiting_spelling_correction"]
            new_value = text.strip()
            
            log_event("spelling_correction", field=field, old=old_value, new=new_value)
            
            # Check if the new value is too similar to the old one
            if string_similarity(new_value.lower(), old_value.lower()) > 0.95:
                sess["awaiting_spelling_correction"] = None
                save_session(session_data)
                send_message(chat_id, f"⚠️ New value '{new_value}' is too similar to the old value '{old_value}'. Please provide a different spelling or type 'cancel' to abort the correction.")
                return "ok", 200
                
            # Cancel the correction if requested
            if new_value.lower() in ("cancel", "abort", "stop", "nevermind", "never mind"):
                sess["awaiting_spelling_correction"] = None
                save_session(session_data)
                send_message(chat_id, "Spelling correction cancelled.")
                return "ok", 200
                
            # Save original value for undo last
            if field in sess["structured_data"]:
                sess["last_change_history"].append((field, sess["structured_data"][field]))
                
            # Save current state for undo
            sess["command_history"].append(sess["structured_data"].copy())
            
            # Apply the correction based on field type
            sess["awaiting_spelling_correction"] = None
            
            if field in ["service", "services"]:
                # Handle service/services mapping
                field = "services"
                data_field = "task"
                
                # Make sure we're working with the services field
                if "service" in sess["structured_data"] and "services" not in sess["structured_data"]:
                    sess["structured_data"]["services"] = sess["structured_data"].pop("service")
                    
                sess["structured_data"][field] = [
                    {data_field: new_value if string_similarity(item.get(data_field, ""), old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] else item[data_field]}
                    for item in sess["structured_data"].get(field, [])
                    if isinstance(item, dict)
                ]
                
            elif field in ["companies", "roles", "tools", "issues"]:
                data_field = (
                    "name" if field == "companies" else
                    "description" if field == "issues" else
                    "item" if field == "tools" else
                    "name" if field == "roles" else None
                )
                
                sess["structured_data"][field] = [
                    {data_field: new_value if string_similarity(item.get(data_field, ""), old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] else item[data_field],
                     **({} if field != "roles" else {"role": item["role"]})}
                    for item in sess["structured_data"].get(field, [])
                    if isinstance(item, dict)
                ]
                
                # Add to people list if it's a role
                if field == "roles" and new_value not in sess["structured_data"].get("people", []):
                    sess["structured_data"].setdefault("people", []).append(new_value)
                    
            elif field in ["people"]:
                # Update people list
                sess["structured_data"]["people"] = [
                    new_value if string_similarity(item, old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] else item 
                    for item in sess["structured_data"].get("people", [])
                ]
                
                # Also update roles with the same person
                sess["structured_data"]["roles"] = [
                    {"name": new_value, "role": role["role"]} 
                    if string_similarity(role.get("name", ""), old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] 
                    else role
                    for role in sess["structured_data"].get("roles", [])
                ]
                
            elif field in ["activities"]:
                # Update activities list
                sess["structured_data"]["activities"] = [
                    new_value if string_similarity(item, old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"] else item 
                    for item in sess["structured_data"].get("activities", [])
                ]
                
            else:
                # Update scalar field
                sess["structured_data"][field] = new_value
                
            # Update context
            update_context(sess, field, new_value)
                
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Corrected {field} from '{old_value}' to '{new_value}'.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Process regular command
        return handle_command(chat_id, text, sess)
        
    except Exception as e:
        log_event("webhook_error", error=str(e))
        try:
            chat_id = str(data["message"]["chat"]["id"]) if data and "message" in data and "chat" in data["message"] else "unknown"
            send_message(chat_id, "⚠️ An unexpected error occurred. Please try again or type 'help' for assistance.")
        except Exception:
            pass
        return "error", 500

if __name__ == "__main__":
    # Use production server when in production environment
    if os.environ.get("ENVIRONMENT") == "production":
        from waitress import serve
        serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        # Use development server for local testing
        app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
