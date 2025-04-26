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
                data = json.load(f)
                # Convert command_history back to deque
                for chat_id in data:
                    if "command_history" in data[chat_id]:
                        data[chat_id]["command_history"] = deque(data[chat_id]["command_history"], maxlen=MAX_HISTORY)
                return data
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data):
    try:
        # Convert deque to list for JSON serialization
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
    "site_name": r'^(?:site\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)([^,]+?)(?=(?:\s*,\s*(?:segment|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)',
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
    "time": r'^(?:time\s*[:,]?\s*|morning\s*time\s*|afternoon\s*time\s*|evening\s*time\s*)(morning|afternoon|evening|full day)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|weather|impression|comments)\s*:)|$)',
    "clear": r'^(issues|activities|comments)\s*[:,]?\s*none$',
    "reset": r'^(new|new report|reset|\/new)$'
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
                    continue  # Skip duplicates for scalar fields
                seen_fields.add(key)
                if key in ["people", "company", "roles", "tools", "service", "activities", "issues"]:
                    result.setdefault(key, []).extend(value)
                else:
                    result[key] = value
        logger.info({"event": "multi_field_extracted", "result": result})
        return result

    return extract_single_command(text)

def extract_single_command(text):
    result = {}

    # Handle reset commands
    reset_match = re.match(FIELD_PATTERNS["reset"], text, re.IGNORECASE)
    if reset_match:
        logger.info({"event": "reset_command"})
        return {"reset": True}

    # Handle undo command
    if text.lower() in ("undo", "/undo"):
        logger.info({"event": "undo_command"})
        return {"undo": True}

    # Handle status command
    if text.lower() in ("status", "/status"):
        logger.info({"event": "status_command"})
        return {"status": True}

    # Handle export command
    if text.lower() in ("export pdf", "/export pdf"):
        logger.info({"event": "export_pdf_command"})
        return {"export_pdf": True}

    # Regex-based parsing
    for field, pattern in FIELD_PATTERNS.items():
        if field == "reset":
            continue
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            if field == "site_name" and re.search(r'\b(add|delete|remove|correct|update|none|as|role|new|reset)\b', text.lower()):
                continue
            if field == "people":
                name = match.group(1).strip()
                result["people"] = [name]
                logger.info({"event": "extracted_field", "field": "people", "value": name})
            elif field == "role":
                name = match.group(1) or match.group(3)
                role = match.group(2) or match.group(4)
                role = "Worker" if role.lower() == "people" else role.title()
                result["people"] = [name.strip()]
                result["roles"] = [{"name": name.strip(), "role": role}]
                logger.info({"event": "extracted_field", "field": "role", "name": name, "role": role})
            elif field == "supervisor":
                result["people"] = ["User"]
                result["roles"] = [{"name": "User", "role": "Supervisor"}]
                logger.info({"event": "extracted_field", "field": "supervisor"})
            elif field == "company":
                name = match.group(2) if match.group(2) else match.group(1)
                result["company"] = [{"name": name.strip()}]
                logger.info({"event": "extracted_field", "field": "company", "value": name})
            elif field == "clear":
                field_name = match.group(1).lower()
                field_name = "issues" if field_name == "issues" else "activities" if field_name == "activities" else "comments"
                result[field_name] = [] if field_name in ["issues", "activities"] else ""
                logger.info({"event": "extracted_field", "field": "clear", "value": field_name})
            else:
                value = match.group(1).strip()
                result[field] = value
                logger.info({"event": "extracted_field", "field": field, "value": value})
            return result

    # GPT-based parsing for complex inputs
    messages = [
        {"role": "system", "content": "Extract explicitly stated fields from construction site report input. Handle multi-field inputs by splitting on commas or periods and parsing each field. Map ambiguous inputs to likely fields, prioritizing specific fields over site_name. Return JSON with extracted fields."},
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
        # Post-process category and segment
        for field in ["category", "segment"]:
            if field in data and isinstance(data[field], str):
                data[field] = re.sub(r'^(category|segment)\s*:?\s*', '', data[field], flags=re.IGNORECASE).strip()
        # Ensure tools and service only include company if explicitly stated
        for field in ["tools", "service"]:
            if field in data:
                for item in data[field]:
                    if isinstance(item, dict) and "company" in item and not item["company"]:
                        del item["company"]
        # Ensure people and roles consistency
        if "roles" in data:
            for role in data["roles"]:
                if isinstance(role, dict) and "name" in role and role["name"] not in data.get("people", []):
                    data.setdefault("people", []).append(role["name"])
        if not data and text.strip():
            issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error|injury)\b'
            activity_keywords = r'\b(activity|task|progress|construction|building|laying|setting|wiring|installation|scaffolding)\b'
            location_keywords = r'\b(at|in|on)\b'
            if re.search(issue_keywords, text.lower()):
                data = {"issues": [{"description": text.strip()}]}
                logger.info({"event": "fallback_issue", "data": data})
            elif re.search(activity_keywords, text.lower()) and re.search(location_keywords, text.lower()):
                parts = re.split(r'\b(at|in|on)\b', text, flags=re.IGNORECASE)
                location = ", ".join(part.strip().title() for part in parts[2::2] if part.strip())
                activity = parts[0].strip()
                data = {"site_name": location, "activities": [activity]}
                logger.info({"event": "fallback_activity_site", "data": data})
            else:
                data = {"comments": text.strip()}
                logger.info({"event": "fallback_comments", "data": data})
        return data
    except Exception as e:
        logger.error({"event": "gpt_extract_error", "input": text, "error": str(e)})
        if text.strip():
            issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error|injury)\b'
            if re.search(issue_keywords, text.lower()):
                data = {"issues": [{"description": text.strip()}]}
                logger.info({"event": "fallback_issue_error", "data": data})
                return data
            logger.info({"event": "fallback_comments_error", "input": text})
            return {"comments": text.strip()}
        return {}

def string_similarity(a, b):
    similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    logger.info({"event": "string_similarity", "a": a, "b": b, "similarity": similarity})
    return similarity

def merge_structured_data(existing, new):
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
            if key == "company":
                for new_item in new_items:
                    if not isinstance(new_item, dict) or "name" not in new_item:
                        continue
                    new_name = new_item.get("name", "")
                    replaced = False
                    for i, existing_item in enumerate(existing_list):
                        if (isinstance(existing_item, dict) and
                            string_similarity(existing_item.get("name", ""), new_name) > 0.6):
                            existing_list[i] = new_item
                            replaced = True
                            logger.info({"event": "replaced_company", "old": existing_item.get("name"), "new": new_name})
                            break
                    if not replaced and new_item not in existing_list:
                        existing_list.append(new_item)
                        logger.info({"event": "added_company", "name": new_name})
                merged[key] = existing_list
            elif key == "roles":
                for new_item in new_items:
                    if not isinstance(new_item, dict) or "name" not in new_item:
                        continue
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
                merged[key] = existing_list
            elif key == "issues":
                for new_item in new_items:
                    if not isinstance(new_item, dict) or "description" not in new_item:
                        continue
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
                merged[key] = existing_list
            else:
                for item in new_items:
                    if key in ["tools", "service"]:
                        if not isinstance(item, dict) or ("item" not in item and "task" not in item):
                            continue
                        existing_items = [
                            (existing_item.get("item") or existing_item.get("task"),
                             existing_item.get("company"))
                            for existing_item in existing_list if isinstance(existing_item, dict)
                        ]
                        new_key = item.get("item") or item.get("task")
                        if not any(string_similarity(existing_key, new_key) > 0.6 and
                                  string_similarity(existing_company or "", item.get("company") or "") > 0.6
                                  for existing_key, existing_company in existing_items):
                            existing_list.append(item)
                    elif item not in existing_list:
                        existing_list.append(item)
                merged[key] = existing_list
        elif key == "people":
            existing_list = merged.get(key, [])
            new_items = value if isinstance(value, list) else []
            for item in new_items:
                if item and item not in existing_list:
                    existing_list.append(item)
                    logger.info({"event": "added_person", "name": item})
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

def delete_entry(data, field, value=None):
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
        else:
            data[field] = []
            data["roles"] = []
    elif field in ["activities"]:
        if value:
            data[field] = [item for item in data[field] if item.lower() != value.lower()]
        else:
            data[field] = []
    elif field in ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]:
        data[field] = ""
    logger.info({"event": "data_after_deletion", "data": json.dumps(data, indent=2)})
    return data

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
                    "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'Site: Downtown Project' or 'People add Tobias').")
                return "ok", 200
            logger.info({"event": "transcribed_voice", "text": text})

        current_time = time()
        # Handle reset confirmation
        if sess.get("awaiting_reset_confirmation", False):
            logger.info({"event": "reset_confirmation", "text": text, "pending_input": sess["pending_input"]})
            if text.lower() in ("yes", "new", "new report"):
                sess["structured_data"] = blank_report()
                sess["awaiting_correction"] = False
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["command_history"].clear()
                save_session_data(session_data)
                tpl = summarize_data(sess["structured_data"])
                send_telegram_message(chat_id,
                    "**Starting a fresh report**\n\n" + tpl +
                    "\n\nSpeak or type your first field (e.g., 'Site: Downtown Project').")
                return "ok", 200
            elif text.lower() in ("no", "existing", "continue"):
                text = sess["pending_input"]
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["last_interaction"] = current_time
            else:
                send_telegram_message(chat_id,
                    "Please clarify: Reset the report? Reply 'yes' or 'no'.")
                return "ok", 200

        # Check for reset based on pause
        if (current_time - sess.get("last_interaction", 0) > PAUSE_THRESHOLD and
                text.lower() not in ("yes", "no", "new", "new report", "reset", "/new", "existing", "continue")):
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
        if text.lower() in ("new", "new report", "reset", "/new"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            logger.info({"event": "reset_initiated"})
            send_telegram_message(chat_id,
                "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200

        # Handle undo command
        if text.lower() in ("undo", "/undo"):
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
                    "No actions to undo. Add fields like 'Site: X' or 'People add Y'.")
            return "ok", 200

        # Handle status command
        if text.lower() in ("status", "/status"):
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "Current report status:\n\n" + tpl +
                "\n\nAdd more fields or use commands like '/export pdf'.")
            return "ok", 200

        # Handle export pdf command
        if text.lower() in ("export pdf", "/export pdf"):
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
            field = "issues" if field == "issues" else "activities" if field == "activities" else "comments"
            sess["command_history"].append(sess["structured_data"].copy())
            sess["structured_data"][field] = [] if field in ["issues", "activities"] else ""
            save_session_data(session_data)
            logger.info({"event": "cleared_field", "field": field})
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Cleared {field}\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Handle deletion commands
        delete_match = re.match(r'^(delete|remove)\s+(site|segment|category|company|person|people|role|roles|tool|service|activity|activities|issue|issues|time|weather|impression|comments)(?::\s*(.+))?$|^(delete|remove)\s+(site|segment|category|time|weather|impression|comments)$', text, re.IGNORECASE)
        if delete_match:
            action = delete_match.group(1) or delete_match.group(4)
            field = (delete_match.group(2) or delete_match.group(5)).lower()
            value = delete_match.group(3)
            if field in ["person", "people"]:
                field = "people"
            elif field in ["role", "roles"]:
                field = "roles"
            elif field in ["activity", "activities"]:
                field = "activities"
            elif field in ["issue", "issues"]:
                field = "issues"
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
        if extracted.get("reset"):
            sess["awaiting_reset_confirmation"] = True
            sess["pending_input"] = text
            save_session_data(session_data)
            logger.info({"event": "reset_initiated_extracted"})
            send_telegram_message(chat_id,
                "Are you sure you want to reset the report? Reply 'yes' or 'no'.")
            return "ok", 200
        if not any(k in extracted for k in ["company", "people", "roles", "tools", "service", "activities", "issues", "time", "weather", "impression", "comments", "segment", "category", "site_name"]):
            send_telegram_message(chat_id,
                f"⚠️ Unrecognized input: '{text}'. Try formats like 'Site: Downtown Project', 'People add Tobias', or 'Issue: Power outage'. Examples: 'Segment: B', 'Time: morning', 'delete time'.")
            return "ok", 200
        sess["command_history"].append(sess["structured_data"].copy())
        sess["structured_data"] = merge_structured_data(
            sess["structured_data"], enrich_with_date(extracted)
        )
        # Save to SharePoint (placeholder, to be implemented post-testing)
        save_to_sharepoint(chat_id, sess["structured_data"])
        sess["awaiting_correction"] = True
        save_session_data(session_data)
        logger.info({"event": "updated_session", "awaiting_correction": sess["awaiting_correction"]})
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
    logger.info({"event": "app_start", "mode": "local"})
    app.run(port=int(os.getenv("PORT", 5000)), debug=True)
