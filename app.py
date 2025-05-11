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

app = Flask(__name__)

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
    "delete": rf'^(?:delete|remove|none)\s+({categories_pattern})\s*(.+)?$|^(?:delete|remove)\s+(.+)\s+(?:from|in|of|at)\s+({categories_pattern})$|^({categories_pattern})\s+(?:delete|remove|none)\s*(.+)?$|^(?:delete|remove|none)\s+(.+)$',
    "delete_entire": rf'^(?:delete|remove|clear)\s+(?:entire|all)\s+(?:category|field|entries|list)?\s*[:]?\s*({list_categories_pattern})\s*[.!]?$',
    "correct": r'^(?:correct|adjust|update|spell|fix)(?:\s+spelling)?\s+((?:sites?|segments?|categories?|compan(?:y|ies)|persons?|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|times?|weathers?|impressions?|comments?))\s+([^,]+?)(?:\s+to\s+([^,]+?))?\s*(?=(?:\s*,\s*(?:site|segment|category|compan(?:y|ies)|peoples?|roles?|tools?|services?|activit(?:y|ies)|issues?|time|weather|impression|comments)\s*:)|$|\s*$)',
    "help": r'^help(?:\s+on\s+([a-z_]+))?$|^\/help(?:\s+([a-z_]+))?$',
    "undo_last": r'^undo\s+last\s*[.!]?$|^undo\s+last\s+(?:change|modification|edit)\s*[.!]?$',
    "context_add": r'^(?:add|include|include|insert)\s+(?:it|this|that|him|her|them)\s+(?:to|in|into|as)\s+(.+?)\s*[.!]?$',
    "summary": r'^(summarize|summary|short report|brief report|overview|compact report)\s*[.!]?$',
    "detailed": r'^(detailed|full|complete|comprehensive)\s+report\s*[.!]?$',
    "export_pdf": r'^(export|export pdf|export report|generate pdf|generate report)\s*[.!]?$',
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
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        
        # First try with Markdown
        response = requests.post(url, json=payload)
        
        # If Markdown fails, try again without parse_mode
        if response.status_code == 400 and "can't parse entities" in response.text.lower():
            log_event("markdown_parsing_error", chat_id=chat_id)
            # Try with HTML mode first
            payload["parse_mode"] = "HTML"
            # Convert basic Markdown to HTML
            html_text = text.replace("**", "<b>").replace("**", "</b>")
            html_text = html_text.replace("*", "<i>").replace("*", "</i>")
            html_text = html_text.replace("_", "<i>").replace("_", "</i>")
            html_text = html_text.replace("`", "<code>").replace("`", "</code>")
            payload["text"] = html_text
            
            response = requests.post(url, json=payload)
            
            # If HTML also fails, send without formatting
            if response.status_code == 400:
                log_event("html_parsing_error", chat_id=chat_id)
                payload.pop("parse_mode", None)  # Remove parse_mode
                payload["text"] = text.replace("**", "").replace("*", "").replace("_", "").replace("`", "")
                response = requests.post(url, json=payload)
        
        response.raise_for_status()
        log_event("message_sent", chat_id=chat_id, text=text[:50])
    except requests.RequestException as e:
        log_event("send_message_error", chat_id=chat_id, error=str(e))
        # Try with simpler content if all else fails
        try:
            # Strip all formatting and limit text length
            simple_text = text.replace("**", "").replace("*", "").replace("_", "").replace("`", "")
            simple_text = simple_text[:4000]  # Telegram limit is 4096 chars
            simple_payload = {"chat_id": chat_id, "text": simple_text}
            response = requests.post(url, json=simple_payload)
            response.raise_for_status()
            log_event("message_sent_without_formatting", chat_id=chat_id)
            return
        except Exception as inner_e:
            log_event("simplified_message_failed", chat_id=chat_id, error=str(inner_e))
            # Last resort - try sending a very simple error message
            try:
                error_payload = {"chat_id": chat_id, "text": "Error processing request. Please try again."}
                requests.post(url, json=error_payload)
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
            
        # Get the site name and current date/time for the filename
        report_data = session_data.get(chat_id, {}).get("structured_data", {})
        site_name = report_data.get("site_name", "site").lower().replace(" ", "_")
        # Format current datetime as DDMMYYYY_HHMM
        current_time = datetime.now().strftime("%d%m%Y_%H%M%S")
        filename = f"{current_time}_{site_name}.pdf"
            
        files = {'document': (filename, pdf_buffer, 'application/pdf')}
        data = {'chat_id': chat_id, 'caption': caption}
        response = requests.post(url, files=files, data=data)
        response.raise_for_status()
        log_event("pdf_sent", chat_id=chat_id, report_type=report_type, filename=filename)
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
        title = f"Construction Site Report - {report_data.get('site_name', '') or 'Unknown Site'}"
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
        
        # Always include all fields, even empty ones
        lines = [
            f"🏗️ **Site**: {data.get('site_name', '')}",
            f"🛠️ **Segment**: {data.get('segment', '')}",
            f"📋 **Category**: {data.get('category', '')}",
            f"🏢 **Companies**: {', '.join(c.get('name', '') for c in data.get('companies', []) if c.get('name'))}",
            f"👷 **People**: {', '.join(data.get('people', []))}",
            f"🎭 **Roles**: {roles_str}",
            f"🔧 **Services**: {', '.join(s.get('task', '') for s in data.get('services', []) if s.get('task'))}",
            f"🛠️ **Tools**: {', '.join(t.get('item', '') for t in data.get('tools', []) if t.get('item'))}",
            f"📅 **Activities**: {', '.join(data.get('activities', []))}",
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
            f"⏰ **Time**: {data.get('time', '')}",
            f"🌦️ **Weather**: {data.get('weather', '')}",
            f"😊 **Impression**: {data.get('impression', '')}",
            f"💬 **Comments**: {data.get('comments', '')}",
            f"📆 **Date**: {data.get('date', '')}"
        ])
        
        # Include all lines regardless of emptiness
        summary = "\n".join(lines)
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
def extract_single_command(cmd: str) -> Dict[str, Any]:
    """Extract structured data from a single command with enhanced error handling"""
    try:
        log_event("extract_single_command", input=cmd)
        result = {}
        
        # Check for reset/new commands
        reset_match = re.match(FIELD_PATTERNS["reset"], cmd, re.IGNORECASE)
        if reset_match:
            return {"reset": True}
            
        # Check for field-specific patterns
        for raw_field, pattern in FIELD_PATTERNS.items():
            # Skip non-field patterns
            if raw_field in ["reset", "delete", "correct", "clear", "help", 
                           "undo_last", "context_add", "summary", "detailed", 
                           "delete_entire", "export_pdf"]:
                continue
                
            match = re.match(pattern, cmd, re.IGNORECASE)
            if match:
                field = FIELD_MAPPING.get(raw_field, raw_field)
                log_event("field_matched", raw_field=raw_field, mapped_field=field)
                
                # Skip site_name matches that look like commands
                if field == "site_name" and re.search(r'\b(add|insert|delete|remove|correct|adjust|update|spell|none|as|role|new|reset)\b', cmd.lower()):
                    log_event("skipped_site_name", reason="command-like input")
                    continue
                
                # Handle people field
                if field == "people":
                    name = clean_value(match.group(1), field)
                    role = clean_value(match.group(2), field) if len(match.groups()) > 1 and match.group(2) else None
                    
                    # Skip if it's just "supervisor"
                    if name.lower() == "supervisor":
                        continue
                    
                    result["people"] = [name]
                    
                    # If a role is specified, add it to roles as well
                    if role:
                        result["roles"] = [{"name": name, "role": role.title()}]
                        
                # Handle role field
                elif field == "roles":
                    # Groups vary depending on which pattern matched
                    name = None
                    role = None
                    
                    # Extract the name and role from the correct match groups
                    for i in range(1, len(match.groups()) + 1):
                        if match.group(i):
                            if not name:
                                name = clean_value(match.group(i), field)
                            elif not role:
                                role = clean_value(match.group(i), field).title()
                    
                    if not name or not role or name.lower() == "supervisor":
                        continue
                    
                    result["people"] = [name]
                    result["roles"] = [{"name": name, "role": role}]
                
                # Handle supervisor field
                elif field == "roles" and raw_field == "supervisor":
                    value = clean_value(match.group(1), "roles")
                    supervisor_names = [name.strip() for name in re.split(r'\s+and\s+|,', value) if name.strip()]
                    result["roles"] = [{"name": name, "role": "Supervisor"} for name in supervisor_names]
                    result["people"] = supervisor_names
                
                # Handle company field
                elif field == "companies":
                    captured = clean_value(match.group(1) if len(match.groups()) >= 1 and match.group(1) else "", field)
                    # If the first group is empty, try the second group
                    if not captured and len(match.groups()) >= 2:
                        captured = clean_value(match.group(2), field)
                    
                    company_names = [name.strip() for name in re.split(r'\s+and\s+|,', captured) if name.strip()]
                    result["companies"] = [{"name": name} for name in company_names]
                
                # Handle service/services field
                elif field in ["services", "service"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["services"] = []
                    else:
                        services = [service.strip() for service in re.split(r',|\band\b', value) if service.strip()]
                        result["services"] = [{"task": service} for service in services]
                
                # Handle tool/tools field
                elif field in ["tools", "tool"]:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["tools"] = []
                    else:
                        tools = [tool.strip() for tool in re.split(r',|\band\b', value) if tool.strip()]
                        result["tools"] = [{"item": tool} for tool in tools]
                
                # Handle issue field
                elif field == "issues":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["issues"] = []
                    else:
                        issues = [issue.strip() for issue in re.split(r';', value) if issue.strip()]
                        result["issues"] = [{"description": issue} for issue in issues]
                
                # Handle activity field
                elif field == "activities":
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result["activities"] = []
                    else:
                        activities = [activity.strip() for activity in re.split(r',|\band\b', value) if activity.strip()]
                        result["activities"] = activities
                
                # Handle clear command
                elif raw_field == "clear":
                    field_name = match.group(1).lower() 
                    field_name = FIELD_MAPPING.get(field_name, field_name)
                    result[field_name] = [] if field_name in LIST_FIELDS else ""
                
                # Handle other fields (scalar fields)
                else:
                    value = clean_value(match.group(1), field)
                    if value.lower() == "none":
                        result[field] = "" if field in SCALAR_FIELDS else []
                    else:
                        result[field] = value
                
                return result
        
        # If we get here, no pattern matched for this command
        return {}
    except Exception as e:
        log_event("extract_single_command_error", input=cmd, error=str(e))
        return {}
    
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

        # Check for export command
        if re.match(FIELD_PATTERNS["export_pdf"], normalized_text, re.IGNORECASE):
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
                elif groups[4] and groups[5]:  # Third pattern: "category delete value"
                    raw_field = groups[4]
                    value = groups[5]
                else:  # Fourth pattern: "delete value" (single word delete)
                    raw_field = None
                    value = groups[6]
                
                raw_field = raw_field.lower() if raw_field else None
                value = value.strip() if value else None
                field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
                
                log_event("delete_command", field=field, value=value)
                
                if field or value:  # Allow deletions with just a value for fuzzy matching
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
    

def merge_data(existing: Dict[str, Any], new: Dict[str, Any], chat_id: str) -> Dict[str, Any]:
    """Merge new data into existing report data with field validation and history tracking"""
    try:
        log_event("merging_data", new_fields=list(new.keys()))
        
        # Create a copy to avoid modifying the original
        merged = existing.copy()
        
        # Skip reserved operation fields
        skipped_fields = [
            "reset", "undo", "status", "help", "summary", "detailed", "export_pdf", 
            "undo_last", "context_add", "correct_prompt", "error", "command_history",
            "last_change_history", "context"
        ]
        
        # Handle special commands first
        if new.get("reset"):
            log_event("reset_detected_in_merge")
            return blank_report()
            
        if new.get("undo"):
            log_event("undo_detected_in_merge")
            # This should be handled by the command handler
            return merged
            
        if new.get("status"):
            log_event("status_detected_in_merge")
            # This should be handled by the command handler
            return merged
            
        if new.get("help"):
            log_event("help_detected_in_merge")
            # This should be handled by the command handler
            return merged
        
        # Handle context_add command
        if "context_add" in new:
            target_field = new["context_add"].get("field")
            session = session_data.get(chat_id, {})
            context = session.get("context", {})
            
            # Determine what to add based on context
            if target_field and context:
                if target_field in ["people", "roles"]:
                    person = context.get("last_mentioned_person")
                    if person:
                        if target_field == "people":
                            if "people" not in merged:
                                merged["people"] = []
                            if person not in merged["people"]:
                                merged["people"].append(person)
                                log_event("added_person_from_context", person=person)
                        elif target_field == "roles":
                            role = "Unknown"  # Default role
                            if "roles" not in merged:
                                merged["roles"] = []
                            
                            # Check if this person already has a role
                            for existing_role in merged["roles"]:
                                if isinstance(existing_role, dict) and existing_role.get("name") == person:
                                    log_event("person_already_has_role", person=person)
                                    return merged
                            
                            merged["roles"].append({"name": person, "role": role})
                            
                            # Ensure person is in people list
                            if "people" not in merged:
                                merged["people"] = []
                            if person not in merged["people"]:
                                merged["people"].append(person)
                                
                            log_event("added_role_from_context", person=person, role=role)
                    else:
                        log_event("no_person_in_context")
                elif target_field in ["issues", "activities", "tools", "services", "companies"]:
                    item = context.get("last_mentioned_item")
                    if item:
                        if target_field == "issues":
                            if "issues" not in merged:
                                merged["issues"] = []
                            
                            # Check for duplicates
                            for existing_issue in merged["issues"]:
                                if isinstance(existing_issue, dict) and string_similarity(
                                    existing_issue.get("description", ""), item) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                    log_event("issue_already_exists", description=item)
                                    return merged
                            
                            merged["issues"].append({"description": item})
                            log_event("added_issue_from_context", description=item)
                        elif target_field == "activities":
                            if "activities" not in merged:
                                merged["activities"] = []
                            if item not in merged["activities"]:
                                merged["activities"].append(item)
                                log_event("added_activity_from_context", activity=item)
                        elif target_field == "tools":
                            if "tools" not in merged:
                                merged["tools"] = []
                            
                            # Check for duplicates
                            for existing_tool in merged["tools"]:
                                if isinstance(existing_tool, dict) and string_similarity(
                                    existing_tool.get("item", ""), item) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                    log_event("tool_already_exists", item=item)
                                    return merged
                            
                            merged["tools"].append({"item": item})
                            log_event("added_tool_from_context", item=item)
                        elif target_field == "services":
                            if "services" not in merged:
                                merged["services"] = []
                            
                            # Check for duplicates
                            for existing_service in merged["services"]:
                                if isinstance(existing_service, dict) and string_similarity(
                                    existing_service.get("task", ""), item) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                    log_event("service_already_exists", task=item)
                                    return merged
                            
                            merged["services"].append({"task": item})
                            log_event("added_service_from_context", task=item)
                        elif target_field == "companies":
                            if "companies" not in merged:
                                merged["companies"] = []
                            
                            # Check for duplicates
                            for existing_company in merged["companies"]:
                                if isinstance(existing_company, dict) and string_similarity(
                                    existing_company.get("name", ""), item) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                    log_event("company_already_exists", name=item)
                                    return merged
                            
                            merged["companies"].append({"name": item})
                            log_event("added_company_from_context", name=item)
                    else:
                        log_event("no_item_in_context")
            return merged

        # Handle delete operations for entire categories
        for field in LIST_FIELDS:
            if field in new and isinstance(new[field], dict) and new[field].get("delete") is True:
                merged[field] = []
                log_event(f"deleted_entire_{field}")

        # Handle special deletion commands
        if "delete" in new:
            # Process each deletion
            for deletion in new["delete"]:
                field = deletion.get("field")
                value = deletion.get("value")
                
                # For "single word deletion", try to find the field based on the value
                if field is None and value:
                    # Search in all fields to find a match
                    for field_name in LIST_FIELDS:
                        if field_name not in merged:
                            continue
                            
                        if field_name in SIMPLE_LIST_FIELDS:  # people, activities
                            for item in merged[field_name]:
                                if string_similarity(value, item) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                    field = field_name
                                    break
                        elif field_name in DICT_LIST_FIELDS:  # companies, roles, tools, services, issues
                            field_key = (
                                "name" if field_name == "companies" else
                                "name" if field_name == "roles" else
                                "item" if field_name == "tools" else
                                "task" if field_name == "services" else
                                "description" if field_name == "issues" else
                                None
                            )
                            
                            if field_key:
                                for item in merged[field_name]:
                                    if isinstance(item, dict) and field_key in item and string_similarity(value, item[field_key]) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                        field = field_name
                                        break
                        
                        if field:
                            break
                    
                    if not field:
                        # Also check scalar fields
                        for field_name in SCALAR_FIELDS:
                            if field_name in merged and string_similarity(value, merged[field_name]) > CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                field = field_name
                                break
                
                if field:
                    merged = delete_entry(merged, field, value)
            
        # Handle corrections
        if "correct" in new:
            for correction in new["correct"]:
                field = correction.get("field")
                old_value = correction.get("old")
                new_value = correction.get("new")
                
                if field and old_value and new_value:
                    # Remember the original value for undo
                    if field in LIST_FIELDS:
                        original_value = merged.get(field, [])[:]  # Make a copy
                    else:
                        original_value = merged.get(field, "")
                        
                    merged[field] = update_field(merged.get(field, [] if field in LIST_FIELDS else ""), 
                                               field, old_value, new_value)
                    
                    # Save the change for undo_last
                    merged.setdefault("last_change_history", []).append((field, original_value))
            
        # Process other fields
        for key, value in new.items():
            if key in skipped_fields or key in ["delete", "correct"]:
                continue
                
            # Normalize field names
            if key == "company":
                key = "companies"
            elif key == "service":
                key = "services"
            elif key == "tool":
                key = "tools"
                
            # Remember the original value for undo
            if key in LIST_FIELDS:
                original_value = merged.get(key, [])[:]  # Make a copy
            else:
                original_value = merged.get(key, "")
                
            # Handle list fields
            if key in LIST_FIELDS:
                # Clear the list if empty value provided
                if value == []:
                    merged[key] = []
                    log_event("cleared_list", field=key)
                    
                    # Track the change for undo_last
                    merged.setdefault("last_change_history", []).append((key, original_value))
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
                
                # Track the change for undo_last
                merged.setdefault("last_change_history", []).append((key, original_value))
                
            # Handle scalar fields
            else:
                if value == "" and key in SCALAR_FIELDS:
                    merged[key] = ""
                    log_event("cleared_field", field=key)
                elif value:
                    merged[key] = value
                    log_event("updated_field", field=key, value=value)
                    
                # Track the change for undo_last
                merged.setdefault("last_change_history", []).append((key, original_value))
        
        # Limit the size of the last_change_history
        if "last_change_history" in merged and len(merged["last_change_history"]) > CONFIG["MAX_HISTORY"]:
            merged["last_change_history"] = merged["last_change_history"][-CONFIG["MAX_HISTORY"]:]
        
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

def update_field(current_value: Any, field: str, old_value: str, new_value: str) -> Any:
    """Update a field with robust pattern matching and error handling"""
    try:
        log_event("update_field", field=field, old=old_value, new=new_value)
        
        # Handle list fields
        if field in LIST_FIELDS:
            # Convert to list if necessary
            current_list = current_value if isinstance(current_value, list) else []
            old_value = old_value.strip()
            new_value = new_value.strip()
            
            # Handle dictionary list fields
            if field in DICT_LIST_FIELDS:
                field_key = (
                    "name" if field == "companies" else
                    "name" if field == "roles" else
                    "item" if field == "tools" else
                    "task" if field == "services" else
                    "description" if field == "issues" else
                    None
                )
                
                if field_key:
                    # Update existing item or add as new
                    found = False
                    for i, item in enumerate(current_list):
                        if (isinstance(item, dict) and field_key in item and 
                           string_similarity(item[field_key], old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                            if field == "roles" and field_key == "name":
                                # For roles, preserve the role value
                                role_value = item.get("role", "")
                                current_list[i] = {"name": new_value, "role": role_value}
                            else:
                                # For other fields, just update the key value
                                current_list[i][field_key] = new_value
                            found = True
                            log_event(f"updated_{field}_item", old=old_value, new=new_value)
                            break
                    
                    if not found:
                        # If not found, add as new
                        if field == "companies":
                            current_list.append({"name": new_value})
                        elif field == "roles":
                            # Try to parse role info from the new value
                            parts = re.match(r'(.*)\s+as\s+(.*)', new_value, re.IGNORECASE)
                            if parts:
                                name = parts.group(1).strip()
                                role = parts.group(2).strip()
                                current_list.append({"name": name, "role": role})
                            else:
                                current_list.append({"name": new_value, "role": "Unknown"})
                        elif field == "tools":
                            current_list.append({"item": new_value})
                        elif field == "services":
                            current_list.append({"task": new_value})
                        elif field == "issues":
                            current_list.append({"description": new_value})
                        
                        log_event(f"added_{field}_item", value=new_value)
                        
                    return current_list
            
            # Handle simple list fields (people, activities)
            else:
                # Try to update matching item
                for i, item in enumerate(current_list):
                    if (isinstance(item, str) and 
                       string_similarity(item, old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                        current_list[i] = new_value
                        log_event(f"updated_{field}_item", old=item, new=new_value)
                        return current_list
                
                # If not found, add new item
                current_list.append(new_value)
                log_event(f"added_{field}_item", value=new_value)
                return current_list
        
        # Handle scalar fields
        else:
            # Check if current value closely matches the old value
            if (not current_value or 
               string_similarity(current_value, old_value) > CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                log_event(f"updated_{field}", old=current_value, new=new_value)
                return new_value
            else:
                log_event(f"no_match_for_{field}", current=current_value, requested_old=old_value)
                return current_value
        
        return current_value
    except Exception as e:
        log_event("update_field_error", field=field, error=str(e))
        return current_value
    
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
    
    # Removed the "Fields to complete" section
    # Changed prompt to use "category" instead of "field"
    send_message(chat_id, f"**Report reset**\n\n{summary}\n\nSpeak or type your first category (e.g., 'add site Downtown Project').")

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
COMMAND_HANDLERS["export report"] = handle_export
COMMAND_HANDLERS["/help"] = handle_help
COMMAND_HANDLERS["undo last change"] = handle_undo_last
COMMAND_HANDLERS["summarize"] = handle_summary

def handle_command(chat_id: str, text: str, session: Dict[str, Any]) -> tuple[str, int]:
    """Process user command and update session data"""
    try:
        # Update last interaction time
        session["last_interaction"] = time()
        
        # Check for exact command matches first
        clean_text = text.lower().strip()
        if clean_text in COMMAND_HANDLERS:
            COMMAND_HANDLERS[clean_text](chat_id, session)
            return "ok", 200
        
        # Extract fields from input
        extracted = extract_fields(text)
        
        # Handle special commands
        if any(key in extracted for key in ["reset", "undo", "status", "help", "summary", 
                                           "detailed", "export_pdf", "undo_last"]):
            
            if "reset" in extracted:
                handle_reset(chat_id, session)
            elif "undo" in extracted:
                handle_undo(chat_id, session)
            elif "undo_last" in extracted:
                handle_undo_last(chat_id, session)
            elif "status" in extracted:
                handle_status(chat_id, session)
            elif "export_pdf" in extracted:
                handle_export(chat_id, session)
            elif "summary" in extracted:
                handle_summary(chat_id, session)
            elif "detailed" in extracted:
                handle_detailed(chat_id, session)
            elif "help" in extracted:
                topic = extracted["help"]
                handle_help(chat_id, session, topic)
            
            return "ok", 200
            
        # Handle error from field extraction
        if "error" in extracted:
            send_message(chat_id, f"⚠️ Error processing your request: {extracted['error']}")
            return "ok", 200
            
        # Skip empty inputs
        if not extracted:
            send_message(chat_id, "I didn't understand that. Type 'help' for assistance.")
            return "ok", 200
        
        # Save current state for undo
        session["command_history"].append(session["structured_data"].copy())
        
        # Update context tracking for references
        context = session.get("context", {
            "last_mentioned_person": None,
            "last_mentioned_item": None,
            "last_field": None,
        })
        
        # Track mentioned people
        if "people" in extracted and extracted["people"]:
            context["last_mentioned_person"] = extracted["people"][0]
            context["last_field"] = "people"
        elif "roles" in extracted and extracted["roles"]:
            for role in extracted["roles"]:
                if isinstance(role, dict) and "name" in role:
                    context["last_mentioned_person"] = role["name"]
                    context["last_field"] = "roles"
                    break
        
        # Track mentioned items
        if "issues" in extracted and extracted["issues"]:
            for issue in extracted["issues"]:
                if isinstance(issue, dict) and "description" in issue:
                    context["last_mentioned_item"] = issue["description"]
                    context["last_field"] = "issues"
                    break
        elif "activities" in extracted and extracted["activities"]:
            context["last_mentioned_item"] = extracted["activities"][0]
            context["last_field"] = "activities"
        elif "tools" in extracted and extracted["tools"]:
            for tool in extracted["tools"]:
                if isinstance(tool, dict) and "item" in tool:
                    context["last_mentioned_item"] = tool["item"]
                    context["last_field"] = "tools"
                    break
        elif "services" in extracted and extracted["services"]:
            for service in extracted["services"]:
                if isinstance(service, dict) and "task" in service:
                    context["last_mentioned_item"] = service["task"]
                    context["last_field"] = "services"
                    break
        
        session["context"] = context
        
        # Merge the extracted data with existing data
        session["structured_data"] = merge_data(session["structured_data"], extracted, chat_id)
        
        # Make sure date field is properly set
        session["structured_data"] = enrich_date(session["structured_data"])
        
        # Save session data
        save_session(session_data)
        
        # Provide feedback to user
        changed_fields = [field for field in extracted.keys() 
                         if field not in ["help", "reset", "undo", "status", "export_pdf", 
                                         "summary", "detailed", "undo_last", "error"]]
        
        if changed_fields:
            # Prepare a confirmation message based on what was changed
            if "delete" in extracted or any(field.endswith("delete") for field in extracted.keys()):
                message = "✅ Deleted information from your report."
            elif "correct" in extracted:
                message = "✅ Corrected information in your report."
            elif "context_add" in extracted:
                message = "✅ Added information using context."
            else:
                message = "✅ Added information to your report."
                
            # Add a summary of the report
            summary = summarize_report(session["structured_data"])
            send_message(chat_id, f"{message}\n\n{summary}")
        else:
            send_message(chat_id, "⚠️ No changes were made to your report.")
        
        return "ok", 200
        
    except Exception as e:
        log_event("handle_command_error", chat_id=chat_id, text=text, error=str(e))
        try:
            send_message(chat_id, "⚠️ An error occurred while processing your request. Please try again.")
        except Exception:
            pass
        return "error", 500

@app.route("/webhook", methods=["POST"])
def webhook() -> tuple[str, int]:
    """Handle incoming webhook from Telegram"""
    try:
        data = request.get_json()
        log_event("webhook_received", data=data)
        
        # Ignore messages without a message object
        if "message" not in data:
            return "ok", 200
            
        message = data["message"]
        
        # Ignore messages without a chat
        if "chat" not in message:
            return "ok", 200
            
        chat_id = str(message["chat"]["id"])
        
        # Initialize session if not exists
        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "command_history": deque(maxlen=CONFIG["MAX_HISTORY"]),
                "last_change_history": [],
                "last_interaction": time(),
                "context": {
                    "last_mentioned_person": None,
                    "last_mentioned_item": None,
                    "last_field": None,
                },
                "report_format": CONFIG["REPORT_FORMAT"]
            }
            save_session(session_data)
        
        # Handle voice messages
        if "voice" in message:
            # Extract file ID for the voice
            file_id = message["voice"]["file_id"]
            
            # Transcribe voice to text
            text, confidence = transcribe_voice(file_id)
            
            if not text:
                send_message(chat_id, "⚠️ I couldn't understand your voice message. Please try again or type your message.")
                return "ok", 200
                
            # If confidence is low, confirm with the user
            if confidence < 0.7:
                session_data[chat_id]["pending_transcription"] = text
                session_data[chat_id]["awaiting_transcription_confirmation"] = True
                save_session(session_data)
                send_message(chat_id, f"I heard:\n\n\"{text}\"\n\nIs this correct? Reply 'yes' to proceed or 'no' to try again.")
                return "ok", 200
                
            # Proceed with processing the command
            log_event("processing_voice_command", text=text)
            return handle_command(chat_id, text, session_data[chat_id])
        
        # Handle transcription confirmation
        if session_data[chat_id].get("awaiting_transcription_confirmation"):
            text = message.get("text", "").strip().lower()
            
            if text.lower() in ["yes", "y", "yeah", "yep"]:
                # Process the pending transcription
                session_data[chat_id]["awaiting_transcription_confirmation"] = False
                pending_text = session_data[chat_id].get("pending_transcription", "")
                
                if pending_text:
                    session_data[chat_id]["pending_transcription"] = None
                    save_session(session_data)
                    log_event("transcription_confirmed", text=pending_text)
                    return handle_command(chat_id, pending_text, session_data[chat_id])
                else:
                    send_message(chat_id, "⚠️ No pending transcription found. Please try again.")
                    return "ok", 200
            elif text == "no":
                # Clear the pending transcription
                session_data[chat_id]["awaiting_transcription_confirmation"] = False
                session_data[chat_id]["pending_transcription"] = None
                save_session(session_data)
                send_message(chat_id, "Please try speaking again or type your message.")
                return "ok", 200
            else:
                send_message(chat_id, "Please reply 'yes' or 'no' to confirm the transcription.")
                return "ok", 200
        
        # Handle text messages
        if "text" in message:
            text = message["text"].strip()
            log_event("processing_text_command", text=text)
            return handle_command(chat_id, text, session_data[chat_id])
        
        # Handle other types of messages
        send_message(chat_id, "⚠️ I can only process text and voice messages. Please try again.")
        return "ok", 200
        
    except Exception as e:
        log_event("webhook_error", error=str(e))
        return "error", 500
