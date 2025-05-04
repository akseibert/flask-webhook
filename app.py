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
        level=logging.DEBUG,
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
    openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    logger.info({"event": "openai_client_initialized"})
except Exception as e:
    logger.error(f"OpenAI initialization failed: {e}")
    raise

# --- Helper function for fuzzy matching ---
def is_similar(a, b, threshold=0.7):
    ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    if ratio >= threshold:
        logger.info({"event": "fuzzy_match", "a": a, "b": b, "similarity": ratio})
    return ratio >= threshold

# --- GPT Prompt for complex input parsing ---
gpt_prompt = """
You are an AI assistant extracting a construction site report from user input. Extract all explicitly mentioned fields and return them in JSON format. Process the entire input as a single unit, splitting on commas or periods only when fields are clearly separated by keywords. Map natural language phrases and standardized commands (add, delete, correct, insert) to fields accurately, prioritizing specific fields over comments or site_name. Do not treat reset commands ("new", "new report", "reset", "reset report", "/new") as comments or fields; return {} for these. Handle "none" inputs (e.g., "Tools: none") as clearing the respective field, and vague or misspelled inputs (e.g., "Activities: many", "site lake propert") by adding them and noting clarification needed. Ensure no command words (e.g., "add", "delete", "correct", "insert", "s:") appear in the extracted values. For fields like time, prioritize the last mentioned value (e.g., "morning, full day" -> "time": "full day"). Always attempt to extract the category field when relevant (e.g., "Bestand" for construction context).

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
- delete <category> [value]: Remove a value or clear the category (e.g., "delete company Acme Corp" or "delete companies").
- correct <category> <old> to <new>: Update a value (e.g., "correct company Acme to Acme Corp").
- insert <category> <value>: Add a value (e.g., "insert company WindowCleaner" -> "company": [{"name": "WindowCleaner"}]).
- delete <role>: Remove all entries with the specified role (e.g., "delete architect" removes all architects from roles).
- <category>: <value>: Add or update a value (e.g., "Services: abc" -> "service": [{"task": "abc"}]).
- <category>: none: Clear the category (e.g., "Tools: none" -> "tools": []).

Rules:
- Extract fields from colon-separated inputs (e.g., "Services: abc"), natural language (e.g., "weather was cloudy" -> "weather": "cloudy"), or commands (e.g., "add people Anna").
- For segment and category:
  - Extract the value, allowing multi-word inputs (e.g., "Segment: groundfloor" -> "segment": "groundfloor").
- For issues:
  - Recognize keywords: "Issue", "Issues", "Problem", "Delay", "Injury".
  - "Issues: none" clears the issues list.
- For activities:
  - Recognize keywords: "Activity", "Activities", "Task", "Progress", "Construction", or action-oriented phrases.
  - "Activities: none" clears the activities list.
  - Handle vague inputs like "Activities: many" by adding them and noting clarification needed.
- For site_name:
  - Recognize location-like phrases following "site", "at", "in", "on" (e.g., "site Lake Property" -> "site_name": "Lake Property").
  - Handle typos like "lake propert" by suggesting "Lake Property" or similar.
- For people and roles:
  - Recognize "add [name] as [role]" or "[name] [role]" (e.g., "Anna Kasel architect" -> "people": ["Anna Kasel"], "roles": [{"name": "Anna Kasel", "role": "Architect"}]).
  - Support multi-word roles (e.g., "Michael Rich as window cleaner" -> "roles": [{"name": "Michael Rich", "role": "Window Cleaner"}]).
  - Handle multiple person-role pairs (e.g., "People Anna Keller as engineer, Michael Robert as window installation specialist" -> "roles": [{"name": "Anna Keller", "role": "Engineer"}, {"name": "Michael Robert", "role": "Window Installation Specialist"}]).
  - "Roles supervisor" assigns "Supervisor" to the user.
  - Do not assign "Supervisor" unless explicitly stated.
- For tools and service:
  - Recognize "Tool: [item]", "Service: [task]", or commands like "add service abc".
  - Strip command words like "add", "delete", "insert" from the value.
- For time:
  - Prioritize the last mentioned time-related phrase (e.g., "morning, full day" -> "time": "full day").
- Comments should only include non-field-specific notes.
- Return {} for reset commands or irrelevant inputs.
- Case-insensitive matching.

Examples:
- Input: "segment 5" -> {"segment": "5"}
- Input: "Segment: groundfloor" -> {"segment": "groundfloor"}
- Input: "category Bestand" -> {"category": "Bestand"}
- Input: "Morning! At Mountain View Apartments, section 9C, category Bestand, firms BuildFast AG, time full day..." -> {"site_name": "Mountain View Apartments", "segment": "9C", "category": "Bestand", "company": [{"name": "BuildFast AG"}], "time": "full day", ...}
- Input: "People Michael Rich as window cleaner" -> {"people": ["Michael Rich"], "roles": [{"name": "Michael Rich", "role": "Window Cleaner"}]}
- Input: "People Anna Keller as engineer, Michael Robert as window installation specialist" -> {"people": ["Anna Keller", "Michael Robert"], "roles": [{"name": "Anna Keller", "role": "Engineer"}, {"name": "Michael Robert", "role": "Window Installation Specialist"}]}
- Input: "delete companies" -> {"company": {"delete": true}}
- Input: "insert company WindowCleaner" -> {"company": [{"name": "WindowCleaner"}]}
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
    "site_name": r'^(?:add\s+|insert\s+)?(?:site|location|project)\s*[:,\s]*\s*(.+?)\s*$',
    "segment": r'^(?:add\s+|insert\s+)?segment\s*[:,\s]*\s*(.+?)\s*$',
    "category": r'^(?:add\s+|insert\s+)?category\s*[:,\s]*\s*(.+?)\s*$',
    "impression": r'^(?:add\s+|insert\s+)?impression\s*[:,\s]*\s*(.+?)\s*$',
    "people": r'^(?:add\s+|insert\s+)?(?:people|person)\s*[:,\s]*\s*(.+?)(?:\s+as\s+|\s+)(architect|engineer|supervisor|manager|worker|window\s+installer)\s*$|^(?:add\s+|insert\s+)?(?:people|person)\s*[:,\s]*\s*([^:,\s]+(?:\s+[^:,\s]+)*?)(?!\s+as\s+.*)\s*$',
    "role": r'^(?:add\s+|insert\s+)?(?:people\s+|person\s+)?((?:[^,]+?\s+as\s+[^,]+?)(?:,\s*[^,]+?\s+as\s+[^,]+?)*)\s*$|^(?:add\s+|insert\s+)?(?:person|people)\s*[:,\s]*\s*(.+?)\s*,\s*role\s*[:,\s]*\s*(.+?)\s*$',
    "supervisor": r'^(?:add\s+|insert\s+)?(?:supervisors\s*(?:were|are)\s+|i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised|roles?\s*[:,\s]*\s*supervisor\s*$)(.+?)?\s*$',
    "company": r'^(?:add\s+|insert\s+)?(?:company|companies)\s*[:,\s]*\s*(.+?)\s*$',
    "service": r'^(?:add\s+|insert\s+)?(?:service|services|services\s*(?:were|provided))\s*[:,\s]*\s*(.+?)\s*$',
    "tool": r'^(?:add\s+|insert\s+)?(?:tool|tools|tools\s*used\s*(?:included|were))\s*[:,\s]*\s*(.+?)\s*$',
    "activity": r'^(?:add\s+|insert\s+)?(?:activity|activities|activities\s*(?:covered|included))\s*[:,\\s]*\s*(.+?)\s*$',
    "issue": r'^(?:add\s+|insert\s+)?(?:issue|issues|issues\s*(?:encountered|included))\s*[:,\s]*\s*(.+?)\s*$',
    "weather": r'^(?:add\s+|insert\s+)?(?:weather|weather\s+was|good\s+weather|bad\s+weather|sunny|cloudy|rainy)\s*[:,\s]*\s*(.+?)\s*$',
    "time": r'^(?:add\s+|insert\s+)?(?:time|time\s+spent|morning|afternoon|evening|full\s+day)\s*[:,\s]*\s*(.+?)\s*$',
    "comments": r'^(?:add\s+|insert\s+)?(?:comment|comments)\s*[:,\s]*\s*(.+?)\s*$',
    "clear": r'^(issues|activities|comments|tools|service|company|people|roles)\s*[:,\s]*\s*none\s*$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": r'^(?:delete|remove)\s+(site|segment|category|company|companies|person|people|role|roles|tool|tools|service|services|activity|activities|issue|issues|time|weather|impression|comments|architect|engineer|supervisor|manager|worker|window\s+installer)(?:\s+(.+?))?\s*$',
    "correct": r'^(?:correct\s+|update\s+)(site|segment|category|company|person|people|role|roles|tool|service|activity|issue|time|weather|impression|comments)\s+(.+?)\s+to\s+(.+?)\s*$'
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
        with io.BytesIO(audio) as audio_file:
            audio_file.name = "audio.ogg"  # Set a name for compatibility
            response = openai.audio.transcribe(model="whisper-1", file=audio_file)
            logger.debug({"event": "transcription_response", "response": response})
        if "text" not in response:
            logger.error({"event": "transcription_error", "response": response})
            return ""
        text = response["text"].strip()
        if not text:
            logger.warning({"event": "transcription_empty", "result": text})
            return ""
        logger.info({"event": "transcription_success", "text": text})
        return text
    except Exception as e:
        logger.error({"event": "transcription_error", "error": str(e)})
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

def summarize_data(data):
    lines = [f"📍 Site: {data.get('site_name', '')}",
             f"📦 Segment: {data.get('segment', '')}",
             f"📑 Category: {data.get('category', '')}",
             f"🏢 Company: {', '.join(c['name'] for c in data.get('company', []))}",
             f"👷 People: {', '.join(data.get('people', []))}",
             f"🎭 Roles: {', '.join(f'{r['name']} ({r['role']})' for r in data.get('roles', []))}",
             f"🛠 Tools: {', '.join(t['item'] for t in data.get('tools', []))}",
             f"🔨 Service: {', '.join(s['task'] for s in data.get('service', []))}",
             f"🏃 Activities: {', '.join(data.get('activities', []))}",
             f"⚠ Issues: {', '.join(i['description'] for i in data.get('issues', []))}",
             f"⏰ Time: {data.get('time', '')}",
             f"☀ Weather: {data.get('weather', '')}",
             f"💬 Comments: {data.get('comments', '')}",
             f"📅 Date: {data.get('date', '')}"]
    return "\n".join(lines)

def merge_structured_data(existing, new):
    merged = existing.copy()
    deleted = False
    corrected = False
    target = None
    old_value = None
    new_value = None

    for key, value in new.items():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "correct" in value[0]:
            corrected = True
            old_value = value[0]["correct"]["old"]
            new_value = value[0]["correct"]["new"]
            if key == "people":
                merged[key] = [new_value if is_similar(p, old_value) else p for p in merged[key]]
                for r in merged["roles"]:
                    if is_similar(r["name"], old_value):
                        r["name"] = new_value
            elif key == "roles":
                for r in merged["roles"]:
                    if is_similar(r["name"], old_value):
                        r["name"] = new_value
            elif key == "company":
                for c in merged[key]:
                    if is_similar(c["name"], old_value):
                        c["name"] = new_value
            elif key == "tools":
                for t in merged[key]:
                    if is_similar(t["item"], old_value):
                        t["item"] = new_value
            elif key == "service":
                for s in merged[key]:
                    if is_similar(s["task"], old_value):
                        s["task"] = new_value
            elif key == "activities":
                merged[key] = [new_value if is_similar(a, old_value) else a for a in merged[key]]
            elif key == "issues":
                for i in merged[key]:
                    if is_similar(i["description"], old_value):
                        i["description"] = new_value
            continue

        if isinstance(value, dict) and "delete" in value:
            deleted = True
            target = value["delete"].lower() if value["delete"] else None
            if key in ["people", "roles", "company", "tools", "service", "activities", "issues"]:
                field_key = {"people": None, "roles": "name", "company": "name",
                             "tools": "item", "service": "task", "issues": "description"}.get(key)
                if target:
                    if field_key:
                        merged[key] = [item for item in merged[key] if not is_similar(item[field_key], target)]
                    else:
                        merged[key] = [item for item in merged[key] if not is_similar(item, target)]
                else:
                    merged[key] = []
            elif key in ["site_name", "segment", "category", "time", "weather", "impression", "comments"]:
                if target is None or is_similar(merged[key], target):
                    merged[key] = ""
            continue

        if key in merged:
            merged[key] = value

    return merged, deleted, corrected, target, old_value, new_value

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

        # Handle multi-field inputs by splitting on clear separators
        commands = [cmd.strip() for cmd in re.split(r'(?<!\w)\.\s*(?=[A-Z])|;\s*|\n\s*', normalized_text) if cmd.strip()]
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
            # Fallback to GPT for complex multi-field inputs if regex fails
            if not result:
                return extract_gpt_fallback(normalized_text)
            return result

        return extract_single_command(normalized_text)
    except Exception as e:
        logger.error({"event": "extract_site_report_error", "input": text, "error": str(e)})
        raise

def extract_single_command(text):
    try:
        result = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())
        cleaned_text = re.sub(r'^\s*(s+:?|add|delete|remove|correct|update|insert)\s+', '', normalized_text, flags=re.IGNORECASE).strip()
        logger.debug({"event": "extract_single_command", "normalized_text": normalized_text, "cleaned_text": cleaned_text})

        # Handle deletion commands
        delete_match = re.match(FIELD_PATTERNS["delete"], normalized_text, re.IGNORECASE)
        if delete_match:
            field = delete_match.group(1).lower()
            value = delete_match.group(2).strip() if delete_match.group(2) else None
            if field in ["company", "companies"]:
                field = "company"
            elif field in ["person", "people"]:
                field = "people"
            elif field in ["role", "roles"]:
                field = "roles"
            elif field in ["tool", "tools"]:
                field = "tools"
            elif field in ["service", "services"]:
                field = "service"
            elif field in ["activity", "activities"]:
                field = "activities"
            elif field in ["issue", "issues"]:
                field = "issues"
            elif field == "site":
                field = "site_name"
            elif field in ["architect", "engineer", "supervisor", "manager", "worker", "window installer"]:
                result["roles"] = {"delete_role": field}
                result["people"] = {"update_from_roles": True}
                logger.info({"event": "delete_command", "field": "roles", "value": field})
                return result
            else:
                result[field] = {"delete": value if value else True}
            logger.info({"event": "delete_command", "field": field, "value": value})
            return result

        # Handle correction commands
        correct_match = re.match(FIELD_PATTERNS["correct"], normalized_text, re.IGNORECASE)
        if correct_match:
            field = correct_match.group(1).lower()
            old_value = correct_match.group(2).strip()
            new_value = correct_match.group(3).strip()
            if field in ["company", "companies"]:
                field = "company"
                result[field] = [{"correct": {"old": old_value, "new": new_value}}]
            elif field in ["person", "people"]:
                field = "people"
                result[field] = [{"correct": {"old": old_value, "new": new_value}}]
            elif field in ["role", "roles"]:
                field = "roles"
                result[field] = [{"correct": {"old": old_value, "new": new_value}}]
            elif field in ["tool", "tools"]:
                field = "tools"
                result[field] = [{"correct": {"old": old_value, "new": new_value}}]
            elif field in ["service", "services"]:
                field = "service"
                result[field] = [{"correct": {"old": old_value, "new": new_value}}]
            elif field in ["activity", "activities"]:
                field = "activities"
                result[field] = [{"correct": {"old": old_value, "new": new_value}}]
            elif field in ["issue", "issues"]:
                field = "issues"
                result[field] = [{"correct": {"old": old_value, "new": new_value}}]
            elif field == "site":
                field = "site_name"
                result[field] = new_value
            else:
                result[field] = new_value
            logger.info({"event": "correct_command", "field": field, "old_value": old_value, "new_value": new_value})
            return result

        # Handle field extraction
        for field, pattern in FIELD_PATTERNS.items():
            if field in ["reset", "delete", "correct"]:
                continue
            match = re.match(pattern, normalized_text, re.IGNORECASE)
            if match:
                logger.debug({"event": "regex_match", "field": field, "pattern": pattern, "match": match.groups()})
                if field == "site_name" and re.search(r'\b(add|delete|remove|correct|update|none|as|role|new|reset|insert)\b', normalized_text.lower()):
                    continue
                if field == "people":
                    if match.group(2):  # Role detected
                        name = match.group(1).strip()
                        role = match.group(2).title()
                        result["people"] = [name]
                        result["roles"] = [{"name": name, "role": role}]
                    elif match.group(3):  # Name without role
                        names = [name.strip() for name in match.group(3).split(",") if name.strip()]
                        result["people"] = names
                    else:
                        logger.debug({"event": "people_regex_failed", "input": normalized_text})
                        continue
                    logger.info({"event": "extracted_field", "field": "people", "value": result.get("people", [])})
                elif field == "role":
                    role_text = match.group(1) or match.group(3)
                    if role_text:
                        # Split multiple person-role pairs
                        pairs = [pair.strip() for pair in role_text.split(",") if pair.strip()]
                        names = []
                        roles = []
                        for pair in pairs:
                            pair_match = re.match(r'(.+?)\s+as\s+(.+)', pair, re.IGNORECASE)
                            if pair_match:
                                name = pair_match.group(1).strip()
                                role = pair_match.group(2).strip().title()
                                names.append(name)
                                roles.append({"name": name, "role": role})
                        result["people"] = names
                        result["roles"] = roles
                    else:
                        name = match.group(3).strip()
                        role = match.group(4).strip().title()
                        names = [n.strip() for n in name.split(",") if n.strip()]
                        result["people"] = names
                        result["roles"] = [{"name": n, "role": role} for n in names]
                    logger.info({"event": "extracted_field", "field": "roles", "names": result.get("people", []), "role": [r["role"] for r in result.get("roles", [])]})
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
                    name = re.sub(r'^(?:add|insert|company|companies|s:)\s*[:,\s]*', '', match.group(1), flags=re.IGNORECASE).strip()
                    companies = [c.strip() for c in re.split(r',|and', name) if c.strip()]
                    result["company"] = [{"name": c} for c in companies]
                    logger.info({"event": "extracted_field", "field": "company", "value": companies})
                elif field == "clear":
                    field_name = match.group(1).lower()
                    result[field_name] = []
                    logger.info({"event": "extracted_field", "field": field_name, "value": "none"})
                elif field == "service":
                    value = re.sub(r'^(?:add|insert|service|services|services\s*(?:were|provided)|s:)\s*[:,\s]*', '', match.group(1), flags=re.IGNORECASE).strip()
                    if value.lower() == "none":
                        result["service"] = []
                    else:
                        services = [s.strip() for s in re.split(r',|and', value) if s.strip()]
                        result["service"] = [{"task": s} for s in services]
                    logger.info({"event": "extracted_field", "field": "service", "value": value})
                elif field == "tool":
                    value = re.sub(r'^(?:add|insert|tool|tools|tools\s*used\s*(?:included|were)|s:)\s*[:,\s]*', '', match.group(1), flags=re.IGNORECASE).strip()
                    if value.lower() == "none":
                        result["tools"] = []
                    else:
                        tools = [t.strip() for t in re.split(r',|and', value) if t.strip()]
                        known_tools = ["excavator", "scaffold", "crane", "drill", "hammer", "screwdriver"]
                        for i, tool in enumerate(tools):
                            best_match = max(known_tools, key=lambda x: SequenceMatcher(None, tool.lower(), x).ratio(), default=tool)
                            similarity = SequenceMatcher(None, tool.lower(), best_match).ratio()
                            tools[i] = best_match if similarity > 0.8 else tool
                        result["tools"] = [{"item": t} for t in tools]
                    logger.info({"event": "extracted_field", "field": "tools", "value": value})
                elif field == "issue":
                    value = re.sub(r'^(?:add|insert|issue|issues|issues\s*(?:encountered|included)|s:)\s*[:,\s]*', '', match.group(1), flags=re.IGNORECASE).strip()
                    if value.lower() == "none":
                        result["issues"] = []
                    else:
                        issues = [i.strip() for i in re.split(r',|and', value) if i.strip()]
                        result["issues"] = [{"description": i} for i in issues]
                    logger.info({"event": "extracted_field", "field": "issues", "value": value})
                elif field == "activity":
                    value = re.sub(r'^(?:add|insert|activity|activities|activities\s*(?:covered|included)|s:)\s*[:,\s]*', '', match.group(1), flags=re.IGNORECASE).strip()
                    if value.lower() == "none":
                        result["activities"] = []
                    else:
                        activities = [a.strip() for a in re.split(r',|and', value) if a.strip()]
                        result["activities"] = activities
                    logger.info({"event": "extracted_field", "field": "activities", "value": activities})
                elif field in ["weather", "time", "comments", "impression", "site_name", "segment", "category"]:
                    value = re.sub(r'^(?:add|insert|s:)\s*[:,\s]*', '', match.group(1), flags=re.IGNORECASE).strip()
                    result[field] = value
                    logger.info({"event": "extracted_field", "field": field, "value": value})
                return result

        # Fallback to GPT for complex inputs
        return extract_gpt_fallback(cleaned_text)
    except Exception as e:
        logger.error({"event": "extract_single_command_error", "input": text, "error": str(e)})
        raise

def extract_gpt_fallback(text):
    messages = [
        {"role": "system", "content": "Extract explicitly stated fields from construction site report input. Handle multi-field inputs by processing the entire input as a single unit. Return JSON with extracted fields."},
        {"role": "user", "content": gpt_prompt + "\nInput text: " + text}
    ]
    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        raw_response = response.choices[0].message.content
        logger.info({"event": "gpt_response", "raw_response": raw_response})
        data = json.loads(raw_response)
        # Ensure people are populated from roles
        if "roles" in data and data["roles"]:
            existing_people = data.get("people", [])
            for role in data["roles"]:
                name = role.get("name")
                if name and name not in existing_people:
                    existing_people.append(name)
            data["people"] = existing_people
        logger.info({"event": "gpt_extracted", "data": data})
        return data
    except Exception as e:
        logger.error({"event": "gpt_extract_error", "input": text, "error": str(e)})
        return {"comments": text} if text.strip() else {}

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
        logger.debug({"event": "session_accessed", "chat_id": chat_id, "session_keys": list(sess.keys())})

        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id, "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'site Downtown Project' or 'delete company Acme Corp').")
                return "ok", 200
            logger.info({"event": "transcribed_voice", "text": text})

        current_time = time()
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip()) if text else ""

        if not normalized_text:
            send_telegram_message(chat_id, "⚠️ Empty input received. Please provide a valid command (e.g., 'site Downtown Project' or 'delete company Acme Corp').")
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
                send_telegram_message(chat_id, "**Starting a fresh report**\n\n" + tpl + "\n\nSpeak or type your first field (e.g., 'site Downtown Project').")
                return "ok", 200
            elif normalized_text_lower in ("no", "existing", "continue"):
                if sess["pending_input"].lower() in ("new", "new report", "reset", "reset report", "/new"):
                    sess["awaiting_reset_confirmation"] = False
                    sess["pending_input"] = None
                    send_telegram_message(chat_id, "Report not reset. Please provide your next input.")
                    return "ok", 200
                else:
                    text = sess["pending_input"]
                    sess["awaiting_reset_confirmation"] = False
                    sess["pending_input"] = None
                    sess["last_interaction"] = current_time
            else:
                send_telegram_message(chat_id, "Please clarify: Reset the report? Reply 'yes' or 'no'.")
                return "ok", 200

        # Check for reset based on pause
        normalized_text_lower = normalized_text.lower()
        if (current_time - sess.get("last_interaction", 0) > PAUSE_THRESHOLD and
                normalized_text_lower not in ("yes", "no", "new", "new report", "reset", "reset report", "/new", "existing", "continue")):
            sess["pending_input"] = text
            sess["awaiting_reset_confirmation"] = True
            sess["last_interaction"] = current_time
            save_session_data(session_data)
            send_telegram_message(chat_id, "It’s been a while! Reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        sess["last_interaction"] = current_time

        # Handle explicit reset commands
        if normalized_text_lower in ("new", "new report", "reset", "reset report", "/new"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            send_telegram_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        # Handle undo command
        if normalized_text_lower in ("undo", "/undo"):
            if sess["command_history"]:
                prev_state = sess["command_history"].pop()
                sess["structured_data"] = prev_state
                save_session_data(session_data)
                tpl = summarize_data(sess["structured_data"])
                send_telegram_message(chat_id, "Undone last action. Here’s the updated report:\n\n" + tpl + "\n\nAnything else to add or correct?")
            else:
                send_telegram_message(chat_id, "No actions to undo. Add fields like 'site X' or 'delete company Y'.")
            return "ok", 200

        # Handle status command
        if normalized_text_lower in ("status", "/status"):
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id, "Current report status:\n\n" + tpl + "\n\nAdd more fields or use commands like '/export pdf'.")
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
            send_telegram_message(chat_id, f"Cleared {field}\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Process new data or corrections
        extracted = extract_site_report(text)
        logger.info({"event": "extracted_data", "extracted": extracted})
        if extracted.get("reset"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            send_telegram_message(chat_id, "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        if not extracted:
            known_commands = ["site", "add site", "add people", "add tools", "delete company", "correct company", "delete architect", "segment", "category", "insert company"]
            best_match = max(known_commands, key=lambda x: SequenceMatcher(None, text.lower(), x).ratio(), default="")
            similarity = SequenceMatcher(None, text.lower(), best_match).ratio()
            suggestion = f" Did you mean '{best_match}'?" if similarity > 0.6 else ""
            send_telegram_message(chat_id, f"⚠️ Unrecognized input: '{text}'. Try formats like 'site Downtown Project', 'segment 5', 'category Bestand', 'delete company Acme Corp', or 'correct company Acme to Acme Corp'.{suggestion}")
            return "ok", 200

        sess["command_history"].append(sess["structured_data"].copy())
        merged_data, deleted, corrected, target, old_value, new_value = merge_structured_data(
            sess["structured_data"], extracted
        )
        sess["structured_data"] = merged_data
        save_to_sharepoint(chat_id, sess["structured_data"])
        save_session_data(session_data)

        # Provide feedback for deletion or correction
        if deleted:
            field_name = next(iter(extracted))
            if target:
                send_telegram_message(chat_id, f"Removed '{target}' from {field_name}.\n\nHere’s the updated report:\n\n{summarize_data(sess['structured_data'])}\n\nAnything else to add or correct?")
            else:
                send_telegram_message(chat_id, f"Cleared {field_name}.\n\nHere’s the updated report:\n\n{summarize_data(sess['structured_data'])}\n\nAnything else to add or correct?")
        elif corrected:
            field_name = next(iter(extracted))
            send_telegram_message(chat_id, f"Corrected '{old_value}' to '{new_value}' in {field_name}.\n\nHere’s the updated report:\n\n{summarize_data(sess['structured_data'])}\n\nAnything else to add or correct?")
        else:
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id, f"Here’s what I understood:\n\n{tpl}\n\nIs this correct? Reply with corrections or more details.")

        return "ok", 200
    except Exception as e:
        logger.error({"event": "webhook_error", "error": str(e)})
        send_telegram_message(chat_id, "⚠️ An error occurred. Please try again later.")
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
        app.run(port=int(os.getenv("PORT", 10000)), debug=False)
    except Exception as e:
        logger.error({"event": "app_start_error", "error": str(e)})
        raise
