# Import necessary libraries at the top
import os
import sys
import io
import json
import re
import requests
import logging
import signal
import traceback
import pytz
from datetime import datetime
from time import time
from typing import Dict, Any, List, Optional, Callable, Tuple, Set, Union
from flask import Flask, request, jsonify
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from difflib import SequenceMatcher
from collections import defaultdict
from collections import deque
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, cm
from reportlab.lib import colors
from decouple import config
from functools import lru_cache

# Initialize Flask app
app = Flask(__name__)


# --- Timezone utility function ---
def get_berlin_time():
    """Get current time in Berlin timezone"""
    berlin_tz = pytz.timezone('Europe/Berlin')
    return datetime.now(berlin_tz)


# --- Important function definitions (adding stubs to prevent "not defined" errors) ---

# Define extract_fields function (REAL IMPLEMENTATION)
def extract_fields(text: str) -> Dict[str, Any]:
    """Extract fields from text input with enhanced error handling and field validation"""
    try:
        print("REAL extract_fields FUNCTION RUNNING")
        log_event("extract_fields_real", input=text[:100])
        
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())

        # Check for basic commands first
        for command in ["yes_confirm", "no_confirm", "reset", "undo_last", "summary", "detailed", "export_pdf", "help", "sharepoint", "sharepoint_status"]:
            if re.match(FIELD_PATTERNS[command], normalized_text, re.IGNORECASE):
                result[command] = True
                return result

        # Handle structured commands using FIELD_PATTERNS
        for field, pattern in FIELD_PATTERNS.items():
            match = re.match(pattern, normalized_text, re.IGNORECASE)
            if match:
                if field in ["site_name", "segment", "category", "impression", "weather", "time", "comments"]:
                    result[field] = match.group(1).strip()
                elif field == "company":
                    companies_text = match.group(1).strip()
                    companies = [c.strip() for c in re.split(r',|\s+and\s+', companies_text) if c.strip()]
                    result["companies"] = [{"name": company} for company in companies]
                elif field == "people":
                    people_text = match.group(1).strip()
                    people = [p.strip() for p in re.split(r',|\s+and\s+', people_text) if p.strip()]
                    result["people"] = people
                    if match.group(2):  # Role specified
                        role = match.group(2).strip()
                        result["roles"] = [{"name": p, "role": role} for p in people]
                elif field == "role":
                    if match.group(1) and match.group(2):  # Name and role
                        name = match.group(1).strip()
                        role = match.group(2).strip()
                        result["people"] = result.get("people", []) + [name]
                        result["roles"] = result.get("roles", []) + [{"name": name, "role": role}]
                    elif match.group(3):  # Role only
                        role = match.group(3).strip()
                        result["roles"] = result.get("roles", []) + [{"name": "Unknown", "role": role}]
                elif field == "supervisor":
                    name = match.group(1).strip()
                    result["people"] = result.get("people", []) + [name]
                    result["roles"] = result.get("roles", []) + [{"name": name, "role": "Supervisor"}]
                elif field == "tool":
                    tools_text = match.group(1).strip()
                    tools = [t.strip() for t in re.split(r',|\s+and\s+', tools_text) if t.strip()]
                    result["tools"] = [{"item": tool} for tool in tools]
                elif field == "service":
                    services_text = match.group(1).strip()
                    services = [s.strip() for s in re.split(r',|\s+and\s+', services_text) if s.strip()]
                    result["services"] = [{"task": service} for service in services]
                elif field == "activity":
                    activities_text = match.group(1).strip()
                    activities = [a.strip() for a in re.split(r',|\s+and\s+', activities_text) if a.strip()]
                    result["activities"] = activities
                elif field == "issue":
                    issues_text = match.group(1).strip()
                    issues = [i.strip() for i in re.split(r';|,|\s+and\s+', issues_text) if i.strip()]
                    result["issues"] = [{"description": issue, "has_photo": "photo" in issue.lower()} for issue in issues]
                elif field == "delete":
                    target = match.group(1).strip()
                    field_name = match.group(2).strip()
                    result["delete"] = {"target": target, "field": FIELD_MAPPING.get(field_name, field_name)}
                elif field == "delete_entire":
                    field_name = match.group(1).strip()
                    result["delete_entire"] = {"field": FIELD_MAPPING.get(field_name, field_name)}
                elif field == "correct":
                    old_value = match.group(1).strip()
                    field_name = match.group(2).strip()
                    new_value = match.group(3).strip()
                    result["correct"] = [{"field": FIELD_MAPPING.get(field_name, field_name), "old": old_value, "new": new_value}]
                return result

        # Handle free-form reports
        if len(text) > 50 and CONFIG["ENABLE_FREEFORM_EXTRACTION"]:
            log_event("detected_free_form_report", length=len(text))
            
            # Extract reporter's name
            reporter_pattern = r'(?:this\s+is|I\'m|I\s+am)\s+([A-Za-z\s]+)'
            reporter_match = re.search(reporter_pattern, text, re.IGNORECASE)
            reporter_name = reporter_match.group(1).strip() if reporter_match else "Unknown"

            # Extract site name
            site_pattern = r'(?:from|at|on|reporting\s+(?:from|at))\s+(?:the\s+)?([A-Za-z0-9\s]+?)(?=\s*(?:project|site|location|,|\.|$))'
            site_match = re.search(site_pattern, text, re.IGNORECASE)
            if site_match:
                result["site_name"] = site_match.group(1).strip()

            # Extract segment
            segment_pattern = r'(?:segment|section)\s+([A-Za-z0-9\s]+?)(?=\s*(?:category|,|\.|$))'
            segment_match = re.search(segment_pattern, text, re.IGNORECASE)
            if segment_match:
                result["segment"] = segment_match.group(1).strip()

            # Extract category
            category_pattern = r'(?:category|kategorie|file\s+under)\s*(?:is\s+|[:,]?\s*)([A-Za-z\s]+)(?=\s*(?:,|\.|$))'
            category_match = re.search(category_pattern, text, re.IGNORECASE)
            if category_match:
                result["category"] = category_match.group(1).strip()

            # Extract companies
            companies_pattern = r'(?:companies|company|contractors?)(?:\s+(?:involved|on-site|here|present|working|onsite|today|are|were))?\s*(?:were|was|are|is|:)?\s*([^.]+)'
            companies_match = re.search(companies_pattern, text, re.IGNORECASE)
            if companies_match:
                companies_text = companies_match.group(1).strip()
                company_names = [name.strip() for name in re.split(r'\s+and\s+|,', companies_text) if name.strip()]
                result["companies"] = [{"name": name} for name in company_names]

            # Extract people and roles
            roles_pattern = r'([A-Za-z\s]+|myself)\s+(?:as|is|handled|handling)\s+(?:the\s+)?([A-Za-z\s]+?)(?=\s*(?:,|\.|$|\sand\b))'
            roles_matches = re.findall(roles_pattern, text, re.IGNORECASE)
            result["people"] = []
            result["roles"] = []
            for name, role in roles_matches:
                name = name.strip()
                role = role.strip()
                if name.lower() == "myself":
                    name = reporter_name
                if name and name not in result["people"]:
                    result["people"].append(name)
                result["roles"].append({"name": name, "role": role})

            # Extract tools
            tools_pattern = r'(?:tools|equipment|gear|machinery)(?:\s+(?:used|utilized|employed|needed|brought|available))?\s*(?:were|was|are|is|:)?\s*([^.]+)'
            tools_match = re.search(tools_pattern, text, re.IGNORECASE)
            if tools_match:
                tools_text = tools_match.group(1).strip()
                tools = [t.strip() for t in re.split(r',|\s+and\s+', tools_text)]
                result["tools"] = [{"item": tool} for tool in tools if tool]

            # Extract services
            services_pattern = r'(?:services|service|tasks?)(?:\s+(?:provided|performed|done|were|was|included|offered))?\s*(?:were|was|are|is|:)?\s*([^.]+)'
            services_match = re.search(services_pattern, text, re.IGNORECASE)
            if services_match:
                services_text = services_match.group(1).strip()
                services = [s.strip() for s in re.split(r',|\s+and\s+', services_text)]
                result["services"] = [{"task": service} for service in services if service]

            # Extract activities
            activities_pattern = r'(?:activities|work|tasks)\s*(?:included|were|are|is|:)\s*([^.]+)'
            activities_match = re.search(activities_pattern, text, re.IGNORECASE)
            if activities_match:
                activities_text = activities_match.group(1).strip()
                activities = [a.strip() for a in re.split(r',|\s+and\s+', activities_text)]
                result["activities"] = activities

            # Extract issues
            issues_pattern = r'(?:issues|issue|problems?|delays?|injuries?|challenges|spotted|crack)(?:\s+(?:encountered|had|occurred|faced|experienced|ran\s+into))?\s*(?:were|was|are|is|:)?\s*([^.]+)'
            issues_match = re.search(issues_pattern, text, re.IGNORECASE)
            if issues_match:
                issues_text = issues_match.group(1).strip()
                issues = [i.strip() for i in re.split(r';|,|\s+and\s+', issues_text)]
                result["issues"] = [{"description": issue, "has_photo": "photo" in issue.lower()} for issue in issues]

            # Extract time
            time_pattern = r'(?:time|duration|hours?|period|worked)\s*(?:spent|worked|taken|lasted|required|needed)?\s*(?:was|is|:)?\s*([^.]+)'
            time_match = re.search(time_pattern, text, re.IGNORECASE)
            if time_match:
                result["time"] = time_match.group(1).strip()

            # Extract weather
            weather_pattern = r'(?:weather|conditions?|climate)(?:\s+(?:were|was|is|today|outside|current))?\s*(?:like|are|is|:)?\s*([^.]+)'
            weather_match = re.search(weather_pattern, text, re.IGNORECASE)
            if weather_match:
                result["weather"] = weather_match.group(1).strip()

            # Extract impression
            impression_pattern = r'(?:impression|assessment|overview|progress|rating|on\s+track)(?:\s+(?:was|is|overall|general))?\s*(?:like|as|is|:)?\s*([^.]+)'
            impression_match = re.search(impression_pattern, text, re.IGNORECASE)
            if impression_match:
                result["impression"] = impression_match.group(1).strip()

            # Extract comments
            comments_pattern = r'(?:comments|notes|additional\s+notes|thanks)(?:\s*(?:are|is|:))?\s*([^.]+)'
            comments_match = re.search(comments_pattern, text, re.IGNORECASE)
            if comments_match:
                result["comments"] = comments_match.group(1).strip()

            # If valid report, add date
            if any(result.get(field) for field in ["site_name", "people", "companies", "activities", "issues"]):
                log_event("free_form_extraction_success", found_fields=list(result.keys()))
                result["date"] = datetime.now().strftime("%d-%m-%Y")
                return result

        log_event("fields_extracted", result_fields=len(result))
        return result
    except Exception as e:
        log_event("extract_fields_error", input=text[:100], error=str(e), traceback=traceback.format_exc())
        print(f"ERROR in extract_fields: {str(e)}")
        return {"error": str(e)}
    
# Define merge_data function (stub version)
def merge_data(existing_data: Dict[str, Any], new_data: Dict[str, Any], chat_id: str) -> Dict[str, Any]:
    """Merge new data with existing data, handling special cases"""
    print("WARNING: Using stub merge_data function - real function not loaded!")
    return existing_data.copy()

# Define summarize_report function (stub version)
def summarize_report(data: Dict[str, Any]) -> str:
    """Generate a formatted text summary of the report data"""
    print("WARNING: Using stub summarize_report function - real function not loaded!")
    return "Report Summary"

# Define is_free_form_report function (stub version)
def is_free_form_report(text: str) -> bool:
    """Detect if the text looks like a free-form report"""
    return len(text) > CONFIG["FREEFORM_MIN_LENGTH"]

# Define send_message function (stub version)
def send_message(chat_id: str, text: str) -> None:
    """Send message to Telegram with enhanced error handling"""
    print("WARNING: Using stub send_message function - real function not loaded!")
    pass

# Define enrich_date function (stub version)
def enrich_date(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and standardize date format in report data"""
    print("WARNING: Using stub enrich_date function - real function not loaded!")
    return data

# Declare COMMAND_HANDLERS dictionary (empty version)
COMMAND_HANDLERS: Dict[str, Callable[[str, Dict[str, Any]], None]] = {}

# Declare session_data dictionary (empty version)
session_data: Dict[str, Any] = {}

# Declare config variables (empty version)
CONFIG = {}
SCALAR_FIELDS = []
LIST_FIELDS = []
FIELD_PATTERNS = {}


# --- Configuration ---
# --- Configuration - Add NLP extraction settings ---
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
    "ENABLE_FREEFORM_EXTRACTION": config("ENABLE_FREEFORM_EXTRACTION", default=True, cast=bool),
    "FREEFORM_MIN_LENGTH": config("FREEFORM_MIN_LENGTH", default=200, cast=int),
    "ENABLE_SHAREPOINT": config("ENABLE_SHAREPOINT", default=False, cast=bool),
    "SHAREPOINT": {
        "SITE_URL": config("SHAREPOINT_SITE_URL", default=""),
        "USERNAME": config("SHAREPOINT_USERNAME", default=""),
        "PASSWORD": config("SHAREPOINT_PASSWORD", default=""),
        "LIST_NAME": config("SHAREPOINT_LIST_NAME", default="ConstructionReports"),
        "REPORTS_FOLDER": config("SHAREPOINT_REPORTS_FOLDER", default="Shared Documents/ConstructionReports"),
    },
    # New NLP extraction settings
    "ENABLE_NLP_EXTRACTION": config("ENABLE_NLP_EXTRACTION", default=True, cast=bool),
    "NLP_MODEL": config("NLP_MODEL", default="gpt-4", cast=str),
    "NLP_EXTRACTION_CONFIDENCE_THRESHOLD": config("NLP_EXTRACTION_CONFIDENCE_THRESHOLD", default=0.7, cast=float),
    "NLP_MAX_TOKENS": config("NLP_MAX_TOKENS", default=2000, cast=int),
    "NLP_FALLBACK_TO_REGEX": config("NLP_FALLBACK_TO_REGEX", default=True, cast=bool),
    "NLP_COMMAND_PATTERN_WEIGHT": config("NLP_COMMAND_PATTERN_WEIGHT", default=0.7, cast=float),
    "NLP_FREE_FORM_WEIGHT": config("NLP_FREE_FORM_WEIGHT", default=0.3, cast=float),
}

# --- Enhanced GPT Prompt for Construction Site Reports ---
NLP_EXTRACTION_PROMPT = """
You are a specialized AI for extracting structured data from construction site reports. 
You understand specific construction terminology, abbreviations, and common misspellings.

CRITICAL: You're processing input from a construction site worker who might be using voice recognition in a noisy environment, 
so account for audio transcription errors and construction-specific terminology.

Extract information into these fields (only include fields that are explicitly mentioned):

- site_name: string - physical location or project name (e.g., "Downtown Project", "Building 7")
- segment: string - specific section or area within the site (e.g., "5", "North Wing", "Foundation")
- category: string - classification of work or report (e.g., "Bestand", "Safety", "Progress", "Mängelerfassung")
- companies: list of objects with company names [{"name": "BuildRight AG"}, {"name": "ElectricFlow GmbH"}]
- people: list of strings with names of individuals on site ["Anna Keller", "John Smith"]
- roles: list of objects associating people with roles [{"name": "Anna Keller", "role": "Supervisor"}]
- tools: list of objects with equipment/tools [{"item": "mobile crane"}, {"item": "welding equipment"}]
- services: list of objects with services provided [{"task": "electrical wiring"}, {"task": "HVAC installation"}]
- activities: list of strings describing work performed ["laying foundations", "setting up scaffolding"]
- issues: list of objects with problems and their attributes [{"description": "power outage at 10 AM", "has_photo": false}]
- time: string - duration or time period (e.g., "morning", "full day", "8 hours")
- weather: string - weather conditions (e.g., "cloudy with intermittent rain")
- impression: string - overall assessment (e.g., "productive despite setbacks")
- comments: string - additional notes or observations
- date: string - in dd-mm-yyyy format

Special commands to detect (return these as single-field objects, do not combine with other fields):
- reset: boolean (true) - if input contains commands like "new", "new report", or "reset"
- yes_confirm: boolean (true) - for responses like "yes", "yeah", "okay", "sure", "confirm"
- no_confirm: boolean (true) - for responses like "no", "nope", "nah", "negative"
- summary: boolean (true) - for requests like "summarize", "summary", "short report", "overview"
- detailed: boolean (true) - for requests like "detailed report", "full report", "comprehensive report"
- export_pdf: boolean (true) - for requests like "export", "export pdf", "generate report"
- undo_last: boolean (true) - for commands like "undo last", "undo last change"
- sharepoint_export: boolean (true) - for requests like "export to sharepoint", "sync to sharepoint"
- sharepoint_status: boolean (true) - for requests like "sharepoint status", "connection status"
- help: string - extract specific help topic if mentioned after "help"

Deletion commands (parse these accurately):
- If input is "delete X from Y" or "remove X from Y": return {"delete": {"target": "X", "field": "Y"}}
- If input is "delete all X" or "clear X": return {"X": {"delete": true}} where X is the field name

Correction commands:
- If input is "correct X in Y to Z" or similar: return {"correct": [{"field": "Y", "old": "X", "new": "Z"}]}

For voice inputs, handle common transcription errors like:
- "site vs. sight", "weather vs. whether", "crews vs. cruise", "concrete vs. concert", "form vs. foam"
- Misheard numbers: "to buy for" → "2x4", "for buy ate" → "4x8"
- Run-together words: "concretework" → "concrete work", "siteinspection" → "site inspection"

ONLY return a valid JSON object with the extracted fields, nothing else.
"""

# --- NLP-enhanced Field Extraction Functions ---

def extract_with_nlp(text: str) -> Tuple[Dict[str, Any], float]:
    """Use NLP to extract structured data from text with confidence score"""
    try:
        # Skip NLP for obvious command patterns to save time and resources
        if re.match(r'^(?:yes|no|help|new|reset|undo|export|summarize|detailed)\b', text.lower()):
            log_event("nlp_extraction_skipped", reason="obvious_command")
            return {}, 0.0
            
        # Call OpenAI API with the enhanced construction-focused prompt
        log_event("nlp_extraction_start", text_length=len(text))
        try:
            # First try with JSON format for newer models
            response = client.chat.completions.create(
                model=CONFIG["NLP_MODEL"],
                messages=[
                    {"role": "system", "content": NLP_EXTRACTION_PROMPT},
                    {"role": "user", "content": text}
                ],
                temperature=0.1,  # Lower temperature for more consistent extraction
                max_tokens=CONFIG["NLP_MAX_TOKENS"],
                response_format={"type": "json_object"}
            )
        except Exception as e:
            # If the model doesn't support JSON format, try without it
            if "response_format" in str(e):
                log_event("nlp_extraction_format_error", error=str(e))
                response = client.chat.completions.create(
                    model=CONFIG["NLP_MODEL"],
                    messages=[
                        {"role": "system", "content": NLP_EXTRACTION_PROMPT + "\nRespond ONLY with valid JSON."},
                        {"role": "user", "content": text}
                    ],
                    temperature=0.1,
                    max_tokens=CONFIG["NLP_MAX_TOKENS"]
                )
            else:
                raise
        content = response.choices[0].message.content.strip()
        log_event("nlp_extraction_completed", response_length=len(content))
        
        # Extract JSON from the response
        try:
            # First check if the entire response is JSON
            data = json.loads(content)
            
            # Post-process the extracted data
            data = standardize_nlp_output(data)
            
            # Calculate confidence based on fields present and structure
            confidence = calculate_extraction_confidence(data, text)
            
            return data, confidence
            
        except json.JSONDecodeError:
            # Try to extract JSON from the response if not pure JSON
            json_pattern = r'```(?:json)?\s*(.*?)```'
            json_match = re.search(json_pattern, content, re.DOTALL)
            
            if json_match:
                json_str = json_match.group(1)
                try:
                    data = json.loads(json_str)
                    data = standardize_nlp_output(data)
                    confidence = calculate_extraction_confidence(data, text) * 0.9  # Slight penalty for not being pure JSON
                    return data, confidence
                except json.JSONDecodeError:
                    log_event("nlp_json_parse_error", error="Extracted JSON invalid")
            
            log_event("nlp_response_parse_error", content_sample=content[:100])
            return {}, 0.0
            
    except Exception as e:
        log_event("nlp_extraction_error", error=str(e), traceback=traceback.format_exc())
        return {}, 0.0

def standardize_nlp_output(data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure NLP extracted data conforms to expected structure"""
    result = {}
    
    # Handle simple fields
    for field in SCALAR_FIELDS:
        if field in data:
            if data[field] is None:
                result[field] = ""
            else:
                result[field] = str(data[field])
    
    # Handle special command fields (return as is)
    for cmd in ["reset", "yes_confirm", "no_confirm", "summary", "detailed", 
                "export_pdf", "undo_last", "sharepoint_export", "sharepoint_status"]:
        if cmd in data and data[cmd]:
            result[cmd] = True
    
    # Handle help command
    if "help" in data:
        result["help"] = data["help"]
    
    # Handle deletion commands
    if "delete" in data:
        result["delete"] = data["delete"]
    
    # Handle field deletion commands
    for field in LIST_FIELDS:
        if field in data and isinstance(data[field], dict) and "delete" in data[field]:
            result[field] = {"delete": True}
    
    # Handle correction commands
    if "correct" in data:
        result["correct"] = data["correct"]
    
    # Handle structured list fields
    # Companies
    if "companies" in data:
        if isinstance(data["companies"], list):
            result["companies"] = []
            for company in data["companies"]:
                if isinstance(company, dict) and "name" in company:
                    result["companies"].append({"name": company["name"]})
                elif isinstance(company, str):
                    result["companies"].append({"name": company})
    
    # People
    if "people" in data:
        if isinstance(data["people"], list):
            result["people"] = []
            for person in data["people"]:
                if isinstance(person, str):
                    result["people"].append(person)
                elif isinstance(person, dict) and "name" in person:
                    result["people"].append(person["name"])
    
    # Roles
    if "roles" in data:
        if isinstance(data["roles"], list):
            result["roles"] = []
            for role in data["roles"]:
                if isinstance(role, dict) and "name" in role and "role" in role:
                    result["roles"].append({"name": role["name"], "role": role["role"]})
    
    # Tools
    if "tools" in data:
        if isinstance(data["tools"], list):
            result["tools"] = []
            for tool in data["tools"]:
                if isinstance(tool, dict) and "item" in tool:
                    result["tools"].append({"item": tool["item"]})
                elif isinstance(tool, str):
                    result["tools"].append({"item": tool})
    
    # Services
    if "services" in data:
        if isinstance(data["services"], list):
            result["services"] = []
            for service in data["services"]:
                if isinstance(service, dict) and "task" in service:
                    result["services"].append({"task": service["task"]})
                elif isinstance(service, str):
                    result["services"].append({"task": service})
    
    # Activities
    if "activities" in data:
        if isinstance(data["activities"], list):
            result["activities"] = []
            for activity in data["activities"]:
                if isinstance(activity, str):
                    result["activities"].append(activity)
    
    # Issues
    if "issues" in data:
        if isinstance(data["issues"], list):
            result["issues"] = []
            for issue in data["issues"]:
                if isinstance(issue, dict) and "description" in issue:
                    issue_obj = {"description": issue["description"]}
                    if "has_photo" in issue:
                        issue_obj["has_photo"] = bool(issue["has_photo"])
                    else:
                        # Check for photo reference in description
                        has_photo = "photo" in issue["description"].lower() or "picture" in issue["description"].lower()
                        issue_obj["has_photo"] = has_photo
                    result["issues"].append(issue_obj)
                elif isinstance(issue, str):
                    has_photo = "photo" in issue.lower() or "picture" in issue.lower()
                    result["issues"].append({"description": issue, "has_photo": has_photo})
    
    # Handle date field
    if "date" in data:
        # Ensure date is in dd-mm-yyyy format
        date_str = str(data["date"])
        try:
            # Try to parse the date in various formats
            for fmt in ["%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%m.%d.%Y"]:
                try:
                    date_obj = datetime.strptime(date_str, fmt)
                    result["date"] = date_obj.strftime("%d-%m-%Y")
                    break
                except ValueError:
                    continue
        except Exception:
            # If date parsing fails, use today's date
            result["date"] = datetime.now().strftime("%d-%m-%Y")
    
    return result

def calculate_extraction_confidence(data: Dict[str, Any], original_text: str) -> float:
    """Calculate confidence score for NLP extraction"""
    if not data:
        return 0.0
    
    # Base confidence starts at 0.5
    confidence = 0.5
    
    # Check for special commands which should have high confidence
    if any(key in data for key in ["reset", "yes_confirm", "no_confirm", "summary", 
                                  "detailed", "export_pdf", "undo_last", 
                                  "sharepoint_export", "sharepoint_status", "help"]):
        return 0.95
    
    # Check for correction or deletion commands which should have high confidence
    if "delete" in data or any(field in data and isinstance(data[field], dict) and "delete" in data[field] 
                               for field in LIST_FIELDS):
        return 0.9
    
    if "correct" in data:
        return 0.9
    
    # Count fields with data
    field_count = sum(1 for field in SCALAR_FIELDS if field in data and data[field])
    field_count += sum(1 for field in LIST_FIELDS if field in data and data[field] and 
                       (isinstance(data[field], list) and len(data[field]) > 0))
    
    # Adjust confidence based on field count (more fields = higher confidence)
    field_factor = min(0.3, 0.05 * field_count)
    confidence += field_factor
    
    # Check for key field matches in original text
    text_lower = original_text.lower()
    keyword_matches = 0
    
    for field in data:
        field_keywords = {
            "site_name": ["site", "project", "location"],
            "segment": ["segment", "section", "area"],
            "category": ["category", "type"],
            "companies": ["company", "companies", "contractors", "firms"],
            "people": ["people", "persons", "workers", "staff", "crew"],
            "roles": ["role", "position", "supervisor", "manager", "worker"],
            "tools": ["tool", "equipment", "machinery", "gear"],
            "services": ["service", "task", "job"],
            "activities": ["activity", "activities", "work", "tasks", "progress"],
            "issues": ["issue", "problem", "delay", "difficulties", "challenge"],
            "time": ["time", "duration", "hours", "period"],
            "weather": ["weather", "conditions", "sunny", "cloudy", "rain"],
            "impression": ["impression", "assessment", "progress", "rating"],
            "comments": ["comment", "note", "additional", "remark", "observation"]
        }
        
        if field in field_keywords:
            for keyword in field_keywords[field]:
                if keyword in text_lower:
                    keyword_matches += 1
                    break
    
    # Adjust confidence based on keyword matches
    keyword_factor = min(0.2, 0.02 * keyword_matches)
    confidence += keyword_factor
    
    # Check for common command patterns that might reduce confidence
    command_patterns = [
        r'^(?:can you|please|would you|could you)\b',
        r'^(?:what|who|where|when|why|how)\b.*\?$',
        r'^(?:tell me|show me|give me)\b'
    ]
    
    if any(re.search(pattern, original_text, re.IGNORECASE) for pattern in command_patterns):
        confidence -= 0.1
    
    # Check for excessive field values that don't match patterns
    field_value_counts = defaultdict(int)
    
    for field in SCALAR_FIELDS:
        if field in data and data[field]:
            value = str(data[field]).lower()
            for keyword in ["what", "where", "who", "tell me", "show me", "how to"]:
                if keyword in value:
                    field_value_counts[field] += 1
    
    # Reduce confidence for any field values that look like questions
    confidence -= min(0.2, 0.05 * sum(field_value_counts.values()))
    
    # Final confidence score bounded between 0 and 1
    return max(0.0, min(1.0, confidence))

def hybrid_field_extraction(text: str, chat_id: str = None) -> Dict[str, Any]:
    """
    Extract fields using both NLP and regex approaches for maximum accuracy.
    Uses NLP for complex text and falls back to regex for simpler commands.
    """
    try:
        # 1. First try regex pattern matching for command-like inputs
        if re.match(r'^(?:add|site|category|segment|people|companies|tools|services|activities|issues|weather|time|impression|reset|new|yes|no|export|summary|detailed|help)\b', text.lower()):
            # For command-like text, use regex extraction first
            log_event("hybrid_extraction_regex_first", chat_id=chat_id)
            regex_data = extract_fields_with_regex(text, chat_id)
            
            # If regex extraction found something substantial, use it
            if regex_data and not (len(regex_data) == 1 and ("error" in regex_data or 
                                                           (len(regex_data) == 2 and 
                                                            ("error" in regex_data and "site_name" in regex_data) and
                                                            not regex_data.get("site_name")))):
                return regex_data
        
        # 2. For non-command text or if regex failed, check if NLP is enabled
        if CONFIG["ENABLE_NLP_EXTRACTION"]:
            log_event("hybrid_extraction_nlp", chat_id=chat_id)
            nlp_data, confidence = extract_with_nlp(text)
            
            # If confidence meets threshold, use NLP results
            if confidence >= CONFIG["NLP_EXTRACTION_CONFIDENCE_THRESHOLD"]:
                # Add confidence metadata for logging
                nlp_data["_extraction_metadata"] = {
                    "method": "nlp",
                    "confidence": confidence,
                    "timestamp": datetime.now().isoformat()
                }
                
                # Clean up before returning
                if "_extraction_metadata" in nlp_data:
                    metadata = nlp_data.pop("_extraction_metadata")
                    log_event("nlp_extraction_successful", 
                             confidence=metadata["confidence"], 
                             fields=list(nlp_data.keys()),
                             chat_id=chat_id)
                
                return nlp_data
        
        # 3. Fallback to regex extraction if NLP failed or is disabled
        if CONFIG["NLP_FALLBACK_TO_REGEX"] or not CONFIG["ENABLE_NLP_EXTRACTION"]:
            log_event("hybrid_extraction_regex_fallback", chat_id=chat_id)
            return extract_fields_with_regex(text, chat_id)
        
        # 4. If all methods failed, return empty result
        log_event("hybrid_extraction_failed", chat_id=chat_id)
        return {}
        
    except Exception as e:
        log_event("hybrid_extraction_error", error=str(e), traceback=traceback.format_exc(), chat_id=chat_id)
        return {"error": str(e)}

def extract_fields_with_regex(text: str, chat_id: str = None) -> Dict[str, Any]:
    """The original regex-based field extraction (your existing code)"""
    try:
        # Print marker to confirm this function is being used
        print("REGEX extract_fields RUNNING")
        log_event("extract_fields_regex", input=text[:100])
        
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())

        # Check for basic commands first
        for command in ["yes_confirm", "no_confirm", "reset", "undo_last", "summary", "detailed", "export_pdf", "help", "sharepoint", "sharepoint_status"]:
            if re.match(FIELD_PATTERNS[command], normalized_text, re.IGNORECASE):
                result[command] = True
                return result

        # Handle structured commands using FIELD_PATTERNS
        for field, pattern in FIELD_PATTERNS.items():
            match = re.match(pattern, normalized_text, re.IGNORECASE)
            if match:
                if field in ["site_name", "segment", "category", "impression", "weather", "time", "comments"]:
                    result[field] = match.group(1).strip()
                elif field == "company":
                    companies_text = match.group(1).strip()
                    
                    # Remove any "add" prefix from the companies text
                    companies_text = re.sub(r'^add\s+', '', companies_text, flags=re.IGNORECASE)
                    
                    companies = [c.strip() for c in re.split(r',|\s+and\s+', companies_text)]
                    result["companies"] = [{"name": company} for company in companies if company]
                    return result
                elif field == "people":
                    people_text = match.group(1).strip()
                    people = [p.strip() for p in re.split(r',|\s+and\s+', people_text) if p.strip()]
                    result["people"] = people
                    if match.group(2):  # Role specified
                        role = match.group(2).strip()
                        result["roles"] = [{"name": p, "role": role} for p in people]
                elif field == "role":
                    if match.group(1) and match.group(2):  # Name and role
                        name = match.group(1).strip()
                        role = match.group(2).strip()
                        result["people"] = result.get("people", []) + [name]
                        result["roles"] = result.get("roles", []) + [{"name": name, "role": role}]
                    elif match.group(3):  # Role only
                        role = match.group(3).strip()
                        result["roles"] = result.get("roles", []) + [{"name": "Unknown", "role": role}]
                elif field == "supervisor":
                    name = match.group(1).strip()
                    result["people"] = result.get("people", []) + [name]
                    result["roles"] = result.get("roles", []) + [{"name": name, "role": "Supervisor"}]
                elif field == "tool":
                    tools_text = match.group(1).strip()
                    tools = [t.strip() for t in re.split(r',|\s+and\s+', tools_text) if t.strip()]
                    result["tools"] = [{"item": tool} for tool in tools]
                elif field == "service":
                    services_text = match.group(1).strip()
                    services = [s.strip() for s in re.split(r',|\s+and\s+', services_text) if s.strip()]
                    result["services"] = [{"task": service} for service in services]
                elif field == "activity":
                    activities_text = match.group(1).strip()
                    activities = [a.strip() for a in re.split(r',|\s+and\s+', activities_text) if a.strip()]
                    result["activities"] = activities
                elif field == "issue":
                    issues_text = match.group(1).strip()
                    issues = [i.strip() for i in re.split(r';|,|\s+and\s+', issues_text) if i.strip()]
                    result["issues"] = [{"description": issue, "has_photo": "photo" in issue.lower()} for issue in issues]
                elif field == "delete":
                    target = match.group(1).strip()
                    field_name = match.group(2).strip()
                    result["delete"] = {"target": target, "field": FIELD_MAPPING.get(field_name, field_name)}
                elif field == "delete_entire":
                    field_name = match.group(1).strip()
                    result["delete_entire"] = {"field": FIELD_MAPPING.get(field_name, field_name)}
                elif field == "correct":
                    old_value = match.group(1).strip()
                    field_name = match.group(2).strip()
                    new_value = match.group(3).strip()
                    result["correct"] = [{"field": FIELD_MAPPING.get(field_name, field_name), "old": old_value, "new": new_value}]
                return result

        # Handle free-form reports
        if len(text) > 50 and CONFIG["ENABLE_FREEFORM_EXTRACTION"]:
            log_event("detected_free_form_report", length=len(text))
            
            # Your existing free-form extraction code...
            # This should integrate with the existing extract_fields function
            
        log_event("fields_extracted_regex", result_fields=len(result))
        return result
    except Exception as e:
        log_event("extract_fields_regex_error", input=text[:100], error=str(e), traceback=traceback.format_exc())
        print(f"ERROR in extract_fields_regex: {str(e)}")
        return {"error": str(e)}

# --- REPLACE your main extract_fields function ---
def extract_fields(text: str, chat_id: str = None) -> Dict[str, Any]:
    """
    Extract fields from text input with enhanced NLP capabilities
    This is the main entry point for field extraction that other functions should call
    """
    try:
        print("MAIN extract_fields FUNCTION RUNNING")
        log_event("extract_fields_main", input=text[:100], chat_id=chat_id)
        
        # Use the hybrid extraction approach that combines NLP and regex
        result = hybrid_field_extraction(text, chat_id)
        
        # Handle additional post-processing if needed
        if result and not "error" in result:
            # For scalar fields, ensure they're strings
            for field in SCALAR_FIELDS:
                if field in result and not isinstance(result[field], str):
                    result[field] = str(result[field]) if result[field] is not None else ""
            
            # Make sure date is properly formatted
            if not "date" in result:
                result["date"] = datetime.now().strftime("%d-%m-%Y")
        
        log_event("extract_fields_completed", result_fields=len(result))
        return result
    except Exception as e:
        log_event("extract_fields_error", input=text[:100], error=str(e), traceback=traceback.format_exc())
        print(f"ERROR in extract_fields: {str(e)}")
        return {"error": str(e)}

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
    logger.info({"event": event, **kwargs})

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

# SharePoint field mappings
SHAREPOINT_FIELD_MAPPING = {
    "site_name": "Title",
    "segment": "Segment",
    "category": "Category",
    "time": "TimeSpent",
    "weather": "WeatherConditions",
    "impression": "Impression",
    "comments": "Comments",
    "date": "ReportDate",
    "people": "Personnel",
    "companies": "Companies",
    "tools": "Equipment",
    "services": "Services",
    "activities": "Activities",
    "issues": "Issues",
    "report_file_url": "ReportFileUrl",
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
# Part 2 Regex Patterns
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
    "site_name": r'^(?:(?:add|insert)\s+sites?\s+|sites?\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)(.+?)(?:\s*(?:,|\.|$))',
    "segment": r'^(?:(?:add|insert)\s+segments?\s+|segments?\s*[:,]?\s*|section\s*[:,]?\s*)(.+?)(?:\s*(?:,|\.|$))',
    "category": r'^(?:(?:add|insert)\s+(?:categories?|kategorie)\s+|(?:categories?|kategorie)\s*[:,]?\s*(?:is|are|:)?\\s*|category\s+)(.+?)(?:\\s*(?:,|\\.|$))',
    "impression": r'^(?:(?:add|insert)\s+impressions?\s+|impressions?\s*[:,]?\s*)(.+?)(?:\s*(?:,|\.|$))',
    "people": r'^(?:(?:add|insert)\s+(?:peoples?|persons?|pople)\s+|(?:peoples?|persons?|pople)\s*[:,]?\s*(?:are|is|were|include[ds]?|on\s+site\s+are|:)?\s*)(.+?)(?:\s+as\s+(.+?))?(?:\s*(?:,|\.|$))',
    "role": r'^(?:(?:add|insert)\s+roles?\s+|roles?\s*[:,]?\s*(?:are|is|for)?\s*)?(\w+\s+\w+|\w+)\s+(?:as|is)\s+(.+?)(?:\s*(?:,|\.|$))',
    "supervisor": r'^(?:supervisors?\s+were\s+|(?:add|insert)\s+roles?\s*[:,]?\s*supervisor\s*|roles?\s*[:,]?\s*supervisor\s*)(.+?)(?:\s*(?:,|\.|$))',
    "company": r'^(?:(?:add|insert)\s+compan(?:y|ies)(?:\'s)?\s+|compan(?:y|ies)(?:\'s)?\s*[:,]?\s*(?:are|is|were|include[ds]?|:)?\s*)(.+?)(?:\s*(?:,|\.|$))',
    "service": r'^(?:(?:add|insert)\s+services?\s+|services?\s*[:,]?\s*|services?\s*(?:were|provided)\s+)(.+?)(?:\s*(?:,|\.|$))',
    "tool": r'^(?:(?:add|insert)\s+tools?\s+|tools?\s*[:,]?\s*|tools?\s*used\s*(?:included|were)\s+)(.+?)(?:\s*(?:,|\.|$))',
    "activity": r'^(?:(?:add|insert)\s+activit(?:y|ies)\s+|activit(?:y|ies)\s*[:,]?\s*|activit(?:y|ies)\s*(?:covered|included)?\s*)(.+?)(?:\s*(?:,|\.|$))',
    "issue": r'^(?:(?:add|insert)\s+issues?\s+|issues?\s*[:,]?\s*|issues?\s*(?:encountered|included)?\s*|problem\s*:?\s*|delay\s*:?\s*|injury\s*:?\s*)(.+?)(?:\s*(?:,|\.|$))',
    "weather": r'^(?:(?:add|insert)\s+weathers?\s+|weathers?\s*[:,]?\s*|weather\s+was\s+|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)(.+?)(?:\s*(?:,|\.|$))',
    "time": r'^(?:(?:add|insert)\s+times?\s+|times?\s*[:,]?\s*|time\s+spent\s+|morning\s+time\s*|afternoon\s+time\s*|evening\s+time\s*)(.+?)(?:\s*(?:,|\.|$))',
    "comments": r'^(?:(?:add|insert)\s+comments?\s+|comments?\s*[:,]?\s*)(.+?)(?:\s*(?:,|\.|$))',
    "clear": r'^(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?|site_name|segment|category|time|weather|impression)\s*[:,]?\s*(?:none|delete|clear|remove|reset)$|^(?:clear|empty|reset)\s+(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?|site_name|segment|category|time|weather|impression)$',
    "reset": r'^(new|new\s+report|reset|reset\s+report|\/new)\s*[.!]?$',
    "delete": r'^(?:delete|remove|none)\s+(.+?)\s+from\s+(.+?)(?:\s*(?:,|\.|$))|^delete\s+(issues?|activit(?:y|ies)|comments?|tools?|services?|compan(?:y|ies)|peoples?|roles?|impression)(?:\s*(?:,|\.|$))|^delete\s+(.+?)(?:\s*(?:,|\.|$))', 
    "delete_entire": r'^(?:delete|remove|clear)\s+(?:entire|all)\s+(.+?)(?:\s*(?:,|\.|$))',
    "correct": r'^(?:correct|adjust|update|spell|fix)(?:\s+spelling)?\s+(.+?)\s+in\s+(.+?)\s+to\s+(.+?)(?:\s*(?:,|\.|$))',
    "help": r'^help(?:\s+on\s+([a-z_]+))?$|^\/help(?:\s+([a-z_]+))?$',
    "undo_last": r'^undo\s+last\s*[.!]?$|^undo\s+last\s+(?:change|modification|edit)\s*[.!]?$',
    "context_add": r'^(?:add|include|insert)\s+(?:it|this|that|him|her|them)\s+(?:to|in|into|as)\s+(.+?)\s*[.!]?$',
    "summary": r'^(summarize|summary|short report|brief report|overview|compact report)\s*[.!]?$',
    "detailed": r'^(detailed|full|complete|comprehensive)\s+report\s*[.!]?$',
    "export_pdf": r'^(export|export pdf|export report|generate pdf|generate report)\s*[.!]?$',
    "sharepoint": r'^(export|sync|upload|send|save)\s+(to|on|in|into)\s+sharepoint\s*[.!]?$',
    "sharepoint_status": r'^sharepoint\s+(status|info|information|connection|check)\s*[.!]?$',
    "yes_confirm": r'^(?:yes|ya|yep|yeah|yup|ok|okay|sure|confirm|confirmed|y|да|ню|нью)\s*[.!]?$',
    "no_confirm": r'^(?:no|nope|nah|negative|n|нет)\s*[.!]?$',
    "greeting": r'^(?:hi|hello|hey|greetings|good morning|good afternoon|good evening)\.?$',
    "conversation": r'^(?:i want to|i need to|i would like to|can i|could i|can you|could you)\s+(.+)$'

}
# Extended regex patterns for more nuanced commands
CONTEXTUAL_PATTERNS = {
    "reference_person": r'(he|she|they|him|her|them)\b',
    "reference_thing": r'(it|this|that|these|those)\b',
    "similarity_check": r'(similar|like|same as|close to)\s+([^,.]+)',
    "last_mentioned": r'(last|previous|earlier|before)\s+(mentioned|added|discussed|noted)',
}

#Part 3 Error Handling
# --- Errors and Exceptions ---
class BotError(Exception):
    """Base exception for bot-related errors"""
    pass

class TranscriptionError(BotError):
    """Raised when voice transcription fails"""
    pass

class SharePointError(BotError):
    """Base exception for SharePoint-related errors"""
    pass

class SharePointTemporaryError(SharePointError):
    """Temporary error with SharePoint that can be retried"""
    pass

class SharePointConfigurationError(SharePointError):
    """Error with SharePoint configuration"""
    pass

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
                
                # Add SharePoint tracking if not present
                if "sharepoint_status" not in session:
                    session["sharepoint_status"] = {
                        "synced": False,
                        "last_sync": None,
                        "list_item_id": None,
                        "file_url": None,
                        "sync_errors": []
                    }
                
                # Add awaiting_confirmation for the reset command
                if "awaiting_reset_confirmation" not in session:
                    session["awaiting_reset_confirmation"] = False
                
                # Add spell correction state
                if "awaiting_spelling_correction" not in session:
                    session["awaiting_spelling_correction"] = {
                        "active": False,
                        "field": None,
                        "old_value": None
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
        "impression": "", "comments": "", "date": get_berlin_time().strftime("%d-%m-%Y")
    }

# Part 4
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

Even when presented with a lengthy free-form report, analyze the entire text and extract all relevant information into the structured format above. Be thorough and attentive to details about site activities, personnel, issues, etc.

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
# Part 5 Signal Handlers and Telegram API
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
    """Transcribe voice message with confidence score and language normalization"""
    try:
        audio_url = get_telegram_file_path(file_id)
        audio_response = requests.get(audio_url)
        audio_response.raise_for_status()
        audio = audio_response.content
        log_event("audio_fetched", size_bytes=len(audio))
        
        # Log the raw audio for debugging purposes
        with open(f'/opt/render/project/src/voice_logs.txt', 'a') as f:
            timestamp = datetime.now().isoformat()
            f.write(f"{timestamp} - FILE_ID: {file_id} - SIZE: {len(audio)} bytes\n")
        
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio, "audio/ogg")
        )
        
        text = response.text.strip()
        if not text:
            log_event("transcription_empty")
            return "", 0.0
        
        # Normalize text - handle common non-English transcriptions
        text = normalize_transcription(text)
        
    
        # Calculate confidence based on multiple factors
        confidence = 0.0

        # Adjust confidence based on text length
        text_words = text.split()
        if len(text_words) < 5:  # Short command
            if any(cmd in text.lower() for cmd in ["delete", "add", "category", "reset", "export", "yes", "no"]):
                length_confidence = 0.5  # Higher confidence for common short commands
            else:
                length_confidence = min(0.7, (len(text) / 250) * 0.5)
        elif len(text_words) > 100:  # Long report
            length_confidence = 0.6  # Higher confidence for long reports
        else:
            length_confidence = min(0.7, (len(text) / 250) * 0.5)

        # Keyword confidence - if it contains key construction terms
        construction_keywords = [
            "site", "project", "concrete", "scaffold", "tools", "safety", 
            "worker", "supervisor", "engineer", "contractor", "weather",
            "issue", "delay", "material", "equipment", "schedule", "inspection"
        ]
        
        keyword_matches = sum(1 for word in construction_keywords if word in text.lower())
        keyword_confidence = min(0.3, keyword_matches * 0.05)
        
        # Command confidence - if it matches command patterns
        command_patterns = [
            r'\b(add|delete|update|correct|site|category|people|companies|tools|activities|issues)\b',
            r'\b(new|reset|undo|export|summary|help)\b'
        ]
        
        command_matches = any(re.search(pattern, text, re.IGNORECASE) for pattern in command_patterns)
        command_confidence = 0.3 if command_matches else 0.0
        
        # Combine confidences with different weights
        confidence = length_confidence + keyword_confidence + command_confidence
        
        # Minimum threshold
        confidence = max(0.1, min(confidence, 0.95))
        
        # Bonus for exact command matches
        if any(text.lower().startswith(cmd) for cmd in ["yes", "no", "new", "reset", "add", "site", "undo"]):
            confidence = 0.95
            
        # Log the confidence calculation components
        log_event("transcription_confidence_details", 
                text=text, 
                length_confidence=length_confidence,
                keyword_confidence=keyword_confidence,
                command_confidence=command_confidence,
                final_confidence=confidence)
        
        return text, confidence

        # Bypass confidence check for exact command matches or field-like patterns
        known_commands = ["new", "new report", "yes", "no", "reset", "status", "export", "summary", "detailed", "help"]
        field_patterns = [
            r'^(?:site|segment|category|companies|people|tools|services|activities|issues|time|weather|impression)\b',
            r'^(?:kategorie|baustelle|unternehmen|firma)\b'
        ]
        if (text.lower().strip() in known_commands or
            any(re.match(pattern, text, re.IGNORECASE) for pattern in field_patterns)):
            log_event("transcription_bypassed_confidence", text=text)
            return text, 1.0  # Assign maximum confidence
        
        # Extract and return confidence (approximate calculation)
        confidence = min(0.95, 0.5 + (len(text) / 200))

        # Boost confidence for simple commands
        if len(text.split()) < 5:
            if any(cmd in text.lower() for cmd in ["delete", "add", "yes", "no", "export"]):
                confidence = max(confidence, 0.6)
        
        # Log the transcription for debugging
        with open(f'/opt/render/project/src/voice_logs.txt', 'a') as f:
            timestamp = datetime.now().isoformat()
            f.write(f"{timestamp} - FILE_ID: {file_id} - TRANSCRIPTION: {text}\n")
        
        log_event("transcription_success", text=text, confidence=confidence)
        return text, confidence
    except (requests.RequestException, Exception) as e:
        log_event("transcription_failed", error=str(e))
        return "", 0.0
    
# REPLACE the normalize_transcription function with this enhanced version:
def normalize_transcription(text: str) -> str:
    """Normalize transcription text with enhanced construction vocabulary recognition"""
    # Construction-specific vocabulary for common misrecognitions
    construction_replacements = {
        # Common misheard terms
        r'\bside\s+([a-z]+)\b': r'site \1',  
        r'\bproject\s+section\b': r'project',
        r'\belse\s+true\s+fix\b': r'electro fix',
        r'\bbuild\s+a\b': r'builder',
        r'\broof\s+master\b': r'roof masters',
        
        # Additional construction-specific corrections
        r'\bsee\s+meant\b': r'cement', 
        r'\bscaffold\s+ink\b': r'scaffolding',
        r'\bwire\s+ink\b': r'wiring',
        r'\bfoam\s+work\b': r'form work',
        r'\brein\s+force\s+meant\b': r'reinforcement',
        r'\bcon\s+crete\b': r'concrete',
        r'\bweld\s+in\b': r'welding',
        r'\bheavy\s+coupe\s+meant\b': r'heavy equipment',
        r'\bpower\s+out\s+edge\b': r'power outage',
        r'\btool\s+box\s+talk\b': r'toolbox talk',
        r'\bsafe\s+tea\b': r'safety',
        r'\binspect\s+shun\b': r'inspection',
        r'\breg\s+you\s+late\s+shuns\b': r'regulations',
        
        # Numbers and units
        r'\btwo\s+by\s+four\b': r'2x4',
        r'\bfour\s+by\s+four\b': r'4x4',
        r'\bsquare\s+meter\b': r'square meter',
        r'\bsquare\s+foot\b': r'square foot',
        r'\bcubic\s+yard\b': r'cubic yard',
    }
    
    # Process all construction-specific terms
    for pattern, replacement in construction_replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        
    # Process common command words with typos
    command_corrections = {
        r'\b(ad|ed|odd|at)\b': 'add',
        r'\b(delet|deleet|dell eat|dell it)\b': 'delete',
        r'\b(new|nu|knew)\b': 'new',
        r'\b(reset|re set|resat)\b': 'reset',
        r'\b(expor|export|expoart)\b': 'export',
        r'\b(summery|summary|some mary)\b': 'summary',
        r'\b(komment|coment|comment)\b': 'comment',
    }
    
    for pattern, replacement in command_corrections.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Rest of the original function's code...
    # Convert common non-English transcriptions to English equivalents
    non_english_to_english = {
        # Russian/Cyrillic
        "да": "yes",
        "нет": "no",
        "ню": "new",
        "нью": "new",
        # German
        "kategorie": "category",
        "abnahme": "acceptance",
        "baustelle": "site",
        "unternehmen": "companies",
        "firma": "company",
    }
    
    # Check and replace known words
    text_lower = text.lower()
    for non_english, english in non_english_to_english.items():
        if text_lower.startswith(non_english):
            text = english + text[len(non_english):]
            break
    
    # Handle single-word responses with punctuation
    if re.match(r'^yes[.!?]*$', text_lower):
        text = "yes"
    
    if re.match(r'^no[.!?]*$', text_lower):
        text = "no"
    
    if re.match(r'^new[.!?]*$', text_lower):
        text = "new"
        
    if re.match(r'^new\s+report[.!?]*$', text_lower):
        text = "new report"
    
    return text.strip()

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
        # Format current datetime as DDMMYYYY_HHMMSS
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
    
    # Part 6 SharePoint Integration
    # --- SharePoint Integration ---
class SharePointService:
    """SharePoint service for integration with Microsoft 365"""
    
    def __init__(self, site_url: str, username: str, password: str):
        """Initialize SharePoint connection"""
        self.site_url = site_url
        self.username = username
        self.password = password
        self.is_connected = False
        
        # Verify credentials by making a test connection
        if CONFIG["ENABLE_SHAREPOINT"]:
            self.test_connection()
    
    def test_connection(self) -> bool:
        """Test SharePoint connection"""
        try:
            # This is a placeholder - in a real implementation, we'd establish 
            # a connection to SharePoint using appropriate libraries
            # For example: with Office365-REST-Python-Client
            
            # We're simulating a successful connection for demo purposes
            self.is_connected = True
            log_event("sharepoint_connection_success", site_url=self.site_url)
            return True
        except Exception as e:
            log_event("sharepoint_connection_error", error=str(e))
            self.is_connected = False
            return False
    
    def get_list_info(self, list_name: str) -> Dict[str, Any]:
        """Get SharePoint list information"""
        # This is a placeholder - would get list metadata in a real implementation
        return {"title": list_name, "item_count": 0}
    
    def get_folder_info(self, folder_path: str) -> Dict[str, Any]:
        """Get SharePoint folder information"""
        # This is a placeholder - would get folder metadata in a real implementation
        return {"server_relative_url": folder_path, "exists": True}
    
    def add_list_item(self, list_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Add item to SharePoint list"""
        # This is a placeholder - would add item to list in a real implementation
        
        # Simulate a response with an item ID
        item_id = f"item_{int(time())}"
        log_event("sharepoint_item_added", list_name=list_name, item_id=item_id)
        return {"id": item_id, "data": data}
    
    def update_list_item(self, list_name: str, item_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update SharePoint list item"""
        # This is a placeholder - would update list item in a real implementation
        log_event("sharepoint_item_updated", list_name=list_name, item_id=item_id)
        return {"id": item_id, "data": data}
    
    def upload_file(self, folder_path: str, file_name: str, file_content: bytes) -> Dict[str, Any]:
        """Upload file to SharePoint folder"""
        # This is a placeholder - would upload file in a real implementation
        file_url = f"{folder_path}/{file_name}"
        log_event("sharepoint_file_uploaded", file_path=file_url, size=len(file_content))
        return {"serverRelativeUrl": file_url, "name": file_name}
    
    def get_list_fields(self, list_name: str) -> List[Dict[str, Any]]:
        """Get fields for a SharePoint list"""
        # This is a placeholder - would return fields in a real implementation
        fields = []
        
        # Create dummy fields based on our mapping
        for sp_field in SHAREPOINT_FIELD_MAPPING.values():
            fields.append({
                "InternalName": sp_field,
                "TypeAsString": "Text",
                "Required": False,
                "Title": sp_field
            })
            
        return fields

def prepare_for_sharepoint(data: Dict[str, Any]) -> Dict[str, Any]:
    """Transform report data for SharePoint compatibility"""
    sp_data = {}
    
    # Map simple fields directly
    for field in SCALAR_FIELDS:
        if field in data and data[field]:
            sp_key = SHAREPOINT_FIELD_MAPPING.get(field, field)
            sp_data[sp_key] = data[field]
    
    # Transform complex fields
    if "people" in data and data["people"]:
        sp_data[SHAREPOINT_FIELD_MAPPING["people"]] = ", ".join(data["people"])
    
    if "companies" in data and data["companies"]:
        sp_data[SHAREPOINT_FIELD_MAPPING["companies"]] = ", ".join(
            c.get("name", "") for c in data["companies"] if isinstance(c, dict) and "name" in c
        )
    
    if "roles" in data and data["roles"]:
        roles_data = []
        for role in data["roles"]:
            if isinstance(role, dict) and "name" in role and "role" in role:
                roles_data.append(f"{role['name']} ({role['role']})")
        
        if roles_data:
            sp_data[SHAREPOINT_FIELD_MAPPING.get("roles", "Roles")] = ", ".join(roles_data)
    
    if "tools" in data and data["tools"]:
        sp_data[SHAREPOINT_FIELD_MAPPING["tools"]] = ", ".join(
            t.get("item", "") for t in data["tools"] if isinstance(t, dict) and "item" in t
        )
    
    if "services" in data and data["services"]:
        sp_data[SHAREPOINT_FIELD_MAPPING["services"]] = ", ".join(
            s.get("task", "") for s in data["services"] if isinstance(s, dict) and "task" in s
        )
    
    if "activities" in data and data["activities"]:
        sp_data[SHAREPOINT_FIELD_MAPPING["activities"]] = ", ".join(data["activities"])
    
    if "issues" in data and data["issues"]:
        issues_text = "; ".join(
            i.get("description", "") for i in data["issues"] 
            if isinstance(i, dict) and "description" in i
        )
        
        if issues_text:
            sp_data[SHAREPOINT_FIELD_MAPPING["issues"]] = issues_text
    
    # Add metadata
    sp_data["ReportTimestamp"] = datetime.now().isoformat()
    
    return sp_data

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type(SharePointTemporaryError)
)
def sync_to_sharepoint(chat_id: str, report_data: Dict[str, Any]) -> Dict[str, Any]:
    """Sync report to SharePoint"""
    if not CONFIG["ENABLE_SHAREPOINT"]:
        return {"success": False, "error": "SharePoint integration is not enabled"}
    
    try:
        # Initialize SharePoint service
        sp_service = SharePointService(
            CONFIG["SHAREPOINT"]["SITE_URL"],
            CONFIG["SHAREPOINT"]["USERNAME"],
            CONFIG["SHAREPOINT"]["PASSWORD"]
        )
        
        if not sp_service.is_connected:
            raise SharePointConfigurationError("Could not connect to SharePoint")
        
        # Prepare data for SharePoint
        sp_data = prepare_for_sharepoint(report_data)
        
        # Add to SharePoint list
        list_item = sp_service.add_list_item(
            CONFIG["SHAREPOINT"]["LIST_NAME"], 
            sp_data
        )
        
        # Generate PDF
        report_type = session_data.get(chat_id, {}).get("report_format", "detailed")
        pdf_buffer = generate_pdf(report_data, report_type)
        
        file_url = None
        if pdf_buffer:
            # Upload PDF to SharePoint
            site_name = report_data.get("site_name", "site").lower().replace(" ", "_")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{site_name}.pdf"
            
            file_result = sp_service.upload_file(
                CONFIG["SHAREPOINT"]["REPORTS_FOLDER"],
                filename,
                pdf_buffer.getvalue()
            )
            
            # Get file URL
            file_url = file_result["serverRelativeUrl"]
            
            # Update list item with file link
            sp_service.update_list_item(
                CONFIG["SHAREPOINT"]["LIST_NAME"],
                list_item["id"],
                {"ReportFileUrl": file_url}
            )
        
        # Update session with SharePoint status
        session = session_data.get(chat_id, {})
        session["sharepoint_status"] = {
            "synced": True,
            "last_sync": datetime.now().isoformat(),
            "list_item_id": list_item["id"],
            "file_url": file_url,
            "sync_errors": []
        }
        save_session(session_data)
        
        log_event("sharepoint_sync_success", chat_id=chat_id, item_id=list_item["id"], file_url=file_url)
        
        return {
            "success": True,
            "list_item_id": list_item["id"],
            "file_url": file_url
        }
    except SharePointTemporaryError as e:
        # These errors should be retried
        log_event("sharepoint_temporary_error", chat_id=chat_id, error=str(e))
        raise
    except Exception as e:
        # Record error in session
        session = session_data.get(chat_id, {})
        if "sharepoint_status" not in session:
            session["sharepoint_status"] = {
                "synced": False,
                "last_sync": None,
                "list_item_id": None,
                "file_url": None,
                "sync_errors": []
            }
        
        session["sharepoint_status"]["sync_errors"].append({
            "timestamp": datetime.now().isoformat(),
            "error": str(e)
        })
        save_session(session_data)
        
        log_event("sharepoint_sync_error", chat_id=chat_id, error=str(e))
        return {
            "success": False,
            "error": str(e)
        }
    
    # Part 7 Report Generation
    # --- Report Generation ---
@lru_cache(maxsize=32)
def get_pdf_styles():
    """Cache PDF styles to improve performance"""
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
    
    metadata_style = ParagraphStyle(
        'Metadata',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.gray
    )
    
    return {
        'title': title_style,
        'heading': heading_style,
        'normal': normal_style,
        'metadata': metadata_style
    }

def generate_pdf(report_data: Dict[str, Any], report_type: str = "detailed") -> Optional[io.BytesIO]:
    """Generate PDF report with enhanced performance"""
    try:
        # Generate a cache key for this report
        cache_key = f"{hash(json.dumps(report_data, sort_keys=True))}-{report_type}"
        
        # Check if we have a cached version
        if hasattr(generate_pdf, 'cache') and cache_key in generate_pdf.cache:
            log_event("pdf_cached_version_used", report_type=report_type)
            buffer = generate_pdf.cache[cache_key]
            buffer.seek(0)
            return buffer
            
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = get_pdf_styles()
        
        # Start building the document
        story = []
        
        # Add title and date
        title = f"Construction Site Report - {report_data.get('site_name', '') or 'Unknown Site'}"
        story.append(Paragraph(title, styles['title']))
        story.append(Paragraph(f"Date: {report_data.get('date', datetime.now().strftime('%d-%m-%Y'))}", styles['normal']))
        story.append(Spacer(1, 12))
        
        # Basic site information section
        story.append(Paragraph("Site Information", styles['heading']))
        site_info = [
            ("Site", report_data.get("site_name", "")),
            ("Segment", report_data.get("segment", "")),
            ("Category", report_data.get("category", ""))
        ]
        
        # Only show non-empty fields in summary mode
        site_info = [(label, value) for label, value in site_info if value or report_type == "detailed"]
        
        for label, value in site_info:
            if value:
                story.append(Paragraph(f"<b>{label}:</b> {value}", styles['normal']))
        
        if site_info:
            story.append(Spacer(1, 6))
        
        # Personnel section
        if report_data.get("people") or report_data.get("companies") or report_data.get("roles"):
            story.append(Paragraph("Personnel & Companies", styles['heading']))
            
            if report_data.get("companies"):
                companies_str = ", ".join(c.get("name", "") for c in report_data.get("companies", []) if c.get("name"))
                if companies_str:
                    story.append(Paragraph(f"<b>Companies:</b> {companies_str}", styles['normal']))
            
            if report_data.get("people"):
                people_str = ", ".join(report_data.get("people", []))
                if people_str:
                    story.append(Paragraph(f"<b>People:</b> {people_str}", styles['normal']))
            
            if report_data.get("roles"):
                roles_list = []
                for r in report_data.get("roles", []):
                    if isinstance(r, dict) and r.get("name") and r.get("role"):
                        roles_list.append(f"{r['name']} ({r['role']})")
                
                if roles_list:
                    story.append(Paragraph(f"<b>Roles:</b> {', '.join(roles_list)}", styles['normal']))
            
            story.append(Spacer(1, 6))
        
        # Equipment and services section
        if report_data.get("tools") or report_data.get("services"):
            story.append(Paragraph("Equipment & Services", styles['heading']))
            
            if report_data.get("tools"):
                tools_str = ", ".join(t.get("item", "") for t in report_data.get("tools", []) if t.get("item"))
                if tools_str:
                    story.append(Paragraph(f"<b>Tools:</b> {tools_str}", styles['normal']))
            
            if report_data.get("services"):
                services_str = ", ".join(s.get("task", "") for s in report_data.get("services", []) if s.get("task"))
                if services_str:
                    story.append(Paragraph(f"<b>Services:</b> {services_str}", styles['normal']))
            
            story.append(Spacer(1, 6))
        
        # Activities section
        if report_data.get("activities"):
            story.append(Paragraph("Activities", styles['heading']))
            activities = report_data.get("activities", [])
            
            if report_type == "detailed":
                # In detailed mode, list each activity with a bullet
                for activity in activities:
                    story.append(Paragraph(f"• {activity}", styles['normal']))
            else:
                # In summary mode, just list them with commas
                activities_str = ", ".join(activities)
                story.append(Paragraph(activities_str, styles['normal']))
            
            story.append(Spacer(1, 6))
        
        # Issues section
        if report_data.get("issues"):
            story.append(Paragraph("Issues & Problems", styles['heading']))
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
                            story.append(Paragraph(f"• {desc}{extra}{photo}", styles['normal']))
                else:
                    # In summary mode, just list them with semicolons
                    issues_str = "; ".join(i.get("description", "") for i in issues if isinstance(i, dict) and i.get("description"))
                    story.append(Paragraph(issues_str, styles['normal']))
            
            story.append(Spacer(1, 6))
        
        # Conditions section
        if report_data.get("time") or report_data.get("weather") or report_data.get("impression"):
            story.append(Paragraph("Conditions", styles['heading']))
            
            if report_data.get("time"):
                story.append(Paragraph(f"<b>Time:</b> {report_data.get('time', '')}", styles['normal']))
            
            if report_data.get("weather"):
                story.append(Paragraph(f"<b>Weather:</b> {report_data.get('weather', '')}", styles['normal']))
            
            if report_data.get("impression"):
                story.append(Paragraph(f"<b>Impression:</b> {report_data.get('impression', '')}", styles['normal']))
            
            story.append(Spacer(1, 6))
        
        # Comments section
        if report_data.get("comments"):
            story.append(Paragraph("Additional Comments", styles['heading']))
            story.append(Paragraph(report_data.get("comments", ""), styles['normal']))
        
        # Add SharePoint metadata if enabled
        if CONFIG["ENABLE_SHAREPOINT"]:
            story.append(Spacer(1, 20))
            footer_text = f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} | SharePoint ID: "
            story.append(Paragraph(footer_text, styles['metadata']))
        
        # Build the document
        doc.build(story)
        buffer.seek(0)
        
        # Cache the result before returning
        if not hasattr(generate_pdf, 'cache'):
            generate_pdf.cache = {}
        
        # Limit cache size
        if len(generate_pdf.cache) > 50:
            # Remove random item
            generate_pdf.cache.pop(next(iter(generate_pdf.cache)))
            
        generate_pdf.cache[cache_key] = buffer
        
        buffer.seek(0)
        log_event("pdf_generated", 
                size_bytes=buffer.getbuffer().nbytes, 
                report_type=report_type, 
                site=report_data.get("site_name", "Unknown"))
        return buffer
    except Exception as e:
        log_event("pdf_generation_error", error=str(e))
        return None
        
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
        
        # Add SharePoint metadata if enabled
        if CONFIG["ENABLE_SHAREPOINT"]:
            story.append(Spacer(1, 20))
            metadata_style = ParagraphStyle(
                'Metadata',
                parent=styles['Normal'],
                fontSize=7,
                textColor=colors.gray
            )
            footer_text = f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} | SharePoint ID: "
            story.append(Paragraph(footer_text, metadata_style))
        
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
    
    #Part 8 Free Form Processing

# --- Free-form Text Processing ---
def extract_with_gpt(text: str) -> Dict[str, Any]:
    """Use OpenAI to extract structured data from natural language text"""
    try:
        # Add construction site specific context to the prompt
        construction_prompt = GPT_PROMPT + "\n\nNote that this is for a construction site reporting bot. The input may be transcribed from voice in a noisy environment. Common terms include:\n- 'Site' or 'Project' followed by a location name\n- People's names followed by roles like 'Supervisor', 'Worker', 'Engineer', 'Electrician'\n- Company names like 'BuildRight AG', 'ElectricFlow GmbH'\n- Tools like 'crane', 'scaffolding', 'cement mixer', 'drill'\n- Activities like 'laying foundations', 'pouring concrete', 'electrical wiring'\n\nEven with incomplete or fragmented input, extract whatever information is present."
        
        # Call OpenAI with the enhanced prompt
        response = client.chat.completions.create(
            model=CONFIG["OPENAI_MODEL"],
            messages=[
                {"role": "system", "content": construction_prompt},
                {"role": "user", "content": text}
            ],
            temperature=CONFIG["OPENAI_TEMPERATURE"]
        )
        
        try:
            content = response.choices[0].message.content.strip()
            
            # First look for JSON between backticks
            json_match = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # Otherwise assume the whole response is JSON
                json_str = content
                
            # Parse the JSON
            data = json.loads(json_str)
            log_event("gpt_extracted_data", fields=list(data.keys()))
            return data
        except json.JSONDecodeError as e:
            log_event("gpt_json_parse_error", error=str(e), content=content[:100])
            return {}
        except Exception as e:
            log_event("gpt_response_parsing_error", error=str(e), content=content[:100])
            return {}
    except Exception as e:
        log_event("gpt_api_error", error=str(e))
        return {}

def is_free_form_report(text: str) -> bool:
    """Enhanced detection of free-form construction reports"""
     # If text is very short, it's definitely not a report
    if len(text) < 150:  # Lowered threshold
        return False
        
    # If text is very long, it's likely a report
    if len(text) > 500:
        return True
        
    # Check for command-like patterns first
    if re.match(r'^(?:add|insert|delete|remove|category|site|segment|people|companies|roles|tools|services|activities|issues|weather|time|impression|correct)\b', text.lower()):
        return False
        
    # Look for comprehensive site report indicators
    report_indicators = [
        # Reporting phrases
        r'\b(?:this\s+is|here\s+is|I\s+am\s+providing|I\s+am\s+submitting|sending|reporting)\s+(?:the|a|my)\s+(?:report|update|daily\s+report)\b',
        r'\b(?:daily|weekly|progress|site|inspection)\s+report\b',
        
        # Date/time markers
        r'\b(?:today|this\s+morning|this\s+afternoon|yesterday|on\s+site\s+today)\b',
        
        # Report categories together (multiple categories suggest a comprehensive report)
        r'\b(?:site|location)\b.*\b(?:weather|conditions)\b.*\b(?:work|activities)\b',
        r'\b(?:personnel|workers|people)\b.*\b(?:materials|equipment|tools)\b',
        
        # Paragraph structure with multiple sentences
        r'[.!?][^\n.!?]{20,}[.!?][^\n.!?]{20,}[.!?]',
        
        # Multiple data points
        r'\b(?:we|I)\s+(?:have|had)\s+\d+\s+(?:workers|people|contractors)\b',
        r'\b(?:completed|finished|started|began|continued)\s+(?:the|with|on)\s+[^.!?]+',
    ]
    
    # Count matching indicators
    indicator_count = sum(1 for pattern in report_indicators if re.search(pattern, text, re.IGNORECASE))
    
    # Analyze text structure
    sentence_count = len(re.findall(r'[.!?]+', text))
    comma_count = len(re.findall(r',', text))
    word_count = len(text.split())
    
    # Calculate a confidence score based on multiple factors
    structure_score = min(1.0, (sentence_count / 5) * 0.5 + (comma_count / 8) * 0.3 + (word_count / 100) * 0.2)
    indicator_score = min(1.0, indicator_count * 0.25)
    
    # Combined score
    report_confidence = (structure_score * 0.6) + (indicator_score * 0.4)
    
    log_event("free_form_detection", 
             length=len(text), 
             sentence_count=sentence_count, 
             indicator_count=indicator_count,
             structure_score=structure_score,
             indicator_score=indicator_score,
             report_confidence=report_confidence)
             
    # Return true if confidence exceeds threshold
    return report_confidence > 0.65
    
def custom_extract_fields(text: str) -> Dict[str, Any]:
    """Extract fields from common natural language patterns"""
    result = {}
    
    # Extract multiple roles from inputs like "X is Y, A is B"
    role_pattern = r'([A-Za-z]+(?:\s+[A-Za-z]+)?)\s+is\s+(?:the|a|an)?\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)'
    role_matches = re.findall(role_pattern, text)
    
    if role_matches:
        result["people"] = []
        result["roles"] = []
        for name, role in role_matches:
            name = name.strip()
            role = role.strip()
            if name and role:
                result["people"].append(name)
                result["roles"].append({"name": name, "role": role.title()})
    
    # Extract companies from inputs like "Companies are X, Y and Z"
    company_pattern = r'compan(?:y|ies)\s+(?:is|are)\s+(.*?)(?:\.|\s*$)'
    company_match = re.search(company_pattern, text, re.IGNORECASE)

def suggest_missing_fields(data: Dict[str, Any]) -> List[str]:
    """Intelligently suggest missing fields based on report context"""
    suggestions = []
    
    # Basic required fields
    if not data.get("site_name"):
        suggestions.append("site name")
        
    # Context-based suggestions
    if data.get("activities") and not data.get("time"):
        suggestions.append("time spent")
        
    if data.get("activities") and not data.get("companies") and not data.get("people"):
        suggestions.append("people or companies involved")
        
    if data.get("issues") and not data.get("impression"):
        suggestions.append("overall impression")
        
    if data.get("site_name") and not data.get("weather") and not data.get("activities"):
        suggestions.append("activities performed")
        
    if len(data.get("activities", [])) > 2 and not data.get("tools"):
        suggestions.append("tools used")
        
    # Site context suggestions
    if data.get("site_name") and "install" in " ".join(data.get("activities", [])).lower():
        if not data.get("services"):
            suggestions.append("services provided")
            
    if data.get("site_name") and any(issue.get("description", "").lower().find("delay") >= 0 
                                     for issue in data.get("issues", [])):
        if not data.get("comments"):
            suggestions.append("comments on how to address delays")
            
    return suggestions[:3]  # Limit to 3 suggestions

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
        cleaned = cleaned.replace('concret', 'concrete')
        cleaned = cleaned.replace('instalation', 'installation')
        cleaned = cleaned.replace('scafolding', 'scaffolding')
        cleaned = cleaned.replace('assembeling', 'assembling')
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
    
    #Part 9 Field Extraction
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


def debug_command_matching(text: str, chat_id: str) -> List[Dict[str, Any]]:
    """Debug command matching to identify why a command wasn't understood"""
    results = []
    for field, pattern in FIELD_PATTERNS.items():
        try:
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                results.append({
                    "field": field,
                    "matched": True,
                    "groups": [g for g in match.groups() if g is not None],
                    "pattern": str(pattern)
                })
            else:
                results.append({
                    "field": field,
                    "matched": False,
                    "groups": [],
                    "pattern": str(pattern)
                })
        except Exception as e:
            results.append({
                "field": field,
                "matched": False,
                "error": str(e),
                "pattern": str(pattern),
                "traceback": traceback.format_exc()
            })
    
    log_event("debug_command_matching", text=text, matches=results)
    matched_fields = [r["field"] for r in results if r.get("matched", False)]
    if matched_fields:
        log_event("matched_but_failed_extraction", fields=matched_fields)
        try:
            send_message(chat_id, f"I recognized patterns for {', '.join(matched_fields)} but couldn't process the input. Please clarify (e.g., 'add {matched_fields[0]} <value>').")
        except Exception as e:
            log_event("debug_send_message_error", chat_id=chat_id, error=str(e))
    
    return results

def string_similarity(a: str, b: str) -> float:
    """Calculate string similarity ratio between two strings"""
    try:
        if not a or not b:
            return 0.0
            
        a_lower = a.lower()
        b_lower = b.lower()
        
        # Check for exact match first
        if a_lower == b_lower:
            return 1.0
            
        # Check for direct substring match
        if a_lower in b_lower or b_lower in a_lower:
            # Calculate the ratio of the shorter string to the longer one
            shorter = min(len(a_lower), len(b_lower))
            longer = max(len(a_lower), len(b_lower))
            return min(0.95, shorter / longer + 0.3)  # Add 0.3 to favor substring matches
        
        # Otherwise use SequenceMatcher
        similarity = SequenceMatcher(None, a_lower, b_lower).ratio()
        
        # Log for debugging
        log_event("string_similarity", a=a, b=b, similarity=similarity)
        return similarity
    except Exception as e:
        log_event("string_similarity_error", error=str(e))
        return 0.0


def fuzzy_command_match(command: str, chat_id: str) -> Optional[str]:
    """Match a user input to a command using fuzzy matching"""
    command = command.lower().strip()
    
    # Check for direct matches first
    if command in COMMAND_HANDLERS:
        return command
        
    # Try cleaning up common prefixes
    cleaned_command = re.sub(r'^(please|can you|could you|would you|i want to|i need to)\s+', '', command)
    cleaned_command = cleaned_command.strip()
    
    if cleaned_command in COMMAND_HANDLERS:
        return cleaned_command
        
    # Try fuzzy matching
    best_match = None
    best_score = CONFIG["COMMAND_SIMILARITY_THRESHOLD"]
    
    for cmd in COMMAND_HANDLERS.keys():
        score = string_similarity(command, cmd)
        if score > best_score:
            best_score = score
            best_match = cmd
            
    if best_match:
        log_event("fuzzy_command_match", 
                 original=command, 
                 matched=best_match, 
                 score=best_score,
                 chat_id=chat_id)
        return best_match
        
    # Try partial matching for multi-word commands
    for cmd in COMMAND_HANDLERS.keys():
        if ' ' in cmd:
            cmd_parts = cmd.split()
            command_parts = command.split()
            
            # If first word matches and has similar length
            if (cmd_parts[0] == command_parts[0] and 
                abs(len(' '.join(cmd_parts)) - len(' '.join(command_parts))) < 5):
                log_event("partial_command_match", 
                         original=command, 
                         matched=cmd,
                         chat_id=chat_id)
                return cmd
                
    return None

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

def extract_single_command(cmd: str) -> Dict[str, Any]:
    """Extract structured data from a single command with enhanced error handling"""
    try:
        log_event("extract_single_command", input=cmd)
        result = {}
        
        # Check for reset/new commands
        reset_match = re.match(FIELD_PATTERNS["reset"], cmd, re.IGNORECASE)
        if reset_match:
            return {"reset": True}
            
        # Check for yes/no confirmations
        yes_match = re.match(FIELD_PATTERNS["yes_confirm"], cmd, re.IGNORECASE)
        if yes_match:
            return {"yes_confirm": True}
            
        no_match = re.match(FIELD_PATTERNS["no_confirm"], cmd, re.IGNORECASE)
        if no_match:
            return {"no_confirm": True}
            
        # Check for field-specific patterns
        for raw_field, pattern in FIELD_PATTERNS.items():
            # Skip non-field patterns
            if raw_field in ["reset", "delete", "correct", "clear", "help", 
                        "undo_last", "context_add", "summary", "detailed", 
                        "delete_entire", "export_pdf", "sharepoint",
                        "sharepoint_status", "yes_confirm", "no_confirm"]:
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
                    
                    # Process through all groups to find name and role
                    for i in range(1, len(match.groups()) + 1):
                        if match.group(i):
                            group_text = match.group(i)
                            # If this looks like a name and we don't have one yet
                            if not name and re.match(r'^[A-Za-z]+(\s+[A-Za-z]+)?$', group_text):
                                name = clean_value(group_text, field)
                            # If we have a name but no role yet, this must be the role
                            elif name and not role:
                                # Clean up role text by removing "the", "a", "an"
                                role_text = re.sub(r'^(?:the|a|an)\s+', '', group_text)
                                role = clean_value(role_text, field).title()
                    
                    if not name or not role or name.lower() == "supervisor":
                        continue
                        
                    log_event("role_extraction", name=name, role=role)
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
                    # If the first group is empty or starts with 'are/is', try the second group
                    if (not captured or captured.lower().startswith(('are', 'is', 'were'))) and len(match.groups()) >= 2:
                        captured = clean_value(match.group(2), field)
                    
                    # Remove leading "are", "is", etc.
                    captured = re.sub(r'^(?:are|is|were|include[ds]?)\s+', '', captured)
                    
                    company_names = [name.strip() for name in re.split(r'\s+and\s+|,', captured) if name.strip()]
                    log_event("company_extraction", captured=captured, company_names=company_names)
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
        
        # Check for deletion commands
        delete_match = re.match(FIELD_PATTERNS["delete"], cmd, re.IGNORECASE)
        if delete_match:
            groups = delete_match.groups()
            category = None
            value = None
            
            # Parse different delete syntax patterns
            if groups[0]:  # "delete category value"
                category = FIELD_MAPPING.get(groups[0].lower(), groups[0])
                value = groups[1].strip() if groups[1] else None
            elif groups[2] and groups[3]:  # "delete value from category"
                category = FIELD_MAPPING.get(groups[3].lower(), groups[3])
                value = groups[2].strip()
            elif groups[4] and groups[5]:  # "category delete value"
                category = FIELD_MAPPING.get(groups[4].lower(), groups[4])
                value = groups[5].strip() if groups[5] else None
            elif groups[6]:  # "delete value" (no category)
                value = groups[6].strip()
            
            return {"delete": {"category": category, "value": value}}

            
        # Check for delete entire category
        delete_entire_match = re.match(FIELD_PATTERNS["delete_entire"], cmd, re.IGNORECASE)
        if delete_entire_match:
            field = delete_entire_match.group(1).lower()
            mapped_field = FIELD_MAPPING.get(field, field)
            return {mapped_field: {"delete": True}}
            
        # Check for correction commands
        correct_match = re.match(FIELD_PATTERNS["correct"], cmd, re.IGNORECASE)
        if correct_match:
            raw_field = correct_match.group(1).lower() if correct_match.group(1) else None
            old_value = correct_match.group(2).strip() if correct_match.group(2) else None
            new_value = correct_match.group(3).strip() if correct_match.group(3) else None
            field = FIELD_MAPPING.get(raw_field, raw_field) if raw_field else None
            
            log_event("correct_command", field=field, old=old_value, new=new_value)
            
            if field and old_value:
                if new_value:
                    return {"correct": [{"field": field, "old": clean_value(old_value, field), "new": clean_value(new_value, field)}]}
                else:
                    # If no new value provided, we'll enter the correction mode
                    return {"spelling_correction": {"field": field, "old_value": clean_value(old_value, field)}}
        
        # If we get here, no pattern matched for this command
        return {}
    except Exception as e:
        log_event("extract_single_command_error", input=cmd, error=str(e))
        return {}
    
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def extract_fields(text: str, chat_id: str = None) -> Dict[str, Any]:
    """Extract fields from text input with enhanced error handling and field validation"""
    try:
        # Print marker to confirm this function is being used
        print("REAL extract_fields FUNCTION RUNNING")
        log_event("extract_fields_real", input=text[:100])
        
        result: Dict[str, Any] = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())
        
        # GET CONTEXT if chat_id is provided
        context = None
        if chat_id and chat_id in session_data:
            context = session_data[chat_id].get("context", {})
            existing_data = session_data[chat_id].get("structured_data", {})

        # NEW SECTION: Handle context references
        if context and re.search(r'\b(it|this|that|him|her|they|them)\b', normalized_text, re.IGNORECASE):
            # Pronoun resolution
            pronoun_type = None
            if re.search(r'\b(him|her|he|she|they|them)\b', normalized_text, re.IGNORECASE):
                pronoun_type = "person"
                last_person = context.get("last_mentioned_person")
                
                # Replace pronouns with the actual name if possible
                if last_person:
                    normalized_text = re.sub(r'\b(him|her|he|she|they|them)\b', last_person, normalized_text, flags=re.IGNORECASE)
                    log_event("resolved_person_pronoun", 
                             original=text, 
                             resolved=normalized_text,
                             person=last_person)
            
            elif re.search(r'\b(it|this|that)\b', normalized_text, re.IGNORECASE):
                pronoun_type = "item"
                last_item = context.get("last_mentioned_item")
                
                # Replace pronouns with the actual item if possible
                if last_item:
                    normalized_text = re.sub(r'\b(it|this|that)\b', last_item, normalized_text, flags=re.IGNORECASE)
                    log_event("resolved_item_pronoun", 
                             original=text, 
                             resolved=normalized_text,
                             item=last_item)

        # Check for basic commands first
        if normalized_text.lower() in ("yes", "y", "ya", "yeah", "yep", "yup", "okay", "ok"):
            return {"yes_confirm": True}
            
        if normalized_text.lower() in ("no", "n", "nope", "nah"):
            return {"no_confirm": True}

        if normalized_text.lower() in ("new", "new report", "/new", "reset", "reset report"):
            return {"reset": True}
            
        # Try FIELD_PATTERNS first for structured commands
        for field, pattern in FIELD_PATTERNS.items():
            match = re.match(pattern, normalized_text, re.IGNORECASE)
            if match:
                if field == "site_name":
                    result["site_name"] = match.group(1).strip()
                    return result
                elif field == "segment":
                    result["segment"] = match.group(1).strip()
                    return result
                elif field == "category":
                    # Special handling for Mängelerfassung
                    value = match.group(1).strip()
                    if value.lower() == "mängelerfassung":
                        result["category"] = "Mängelerfassung"
                    else:
                        result["category"] = value
                    return result
                elif field == "company":
                    companies_text = match.group(1).strip()
                    companies = [c.strip() for c in re.split(r',|\s+and\s+', companies_text)]
                    result["companies"] = [{"name": company} for company in companies if company]
                    return result
                elif field == "tool":
                    tools_text = match.group(1).strip()
                    tools = [t.strip() for t in re.split(r',|\s+and\s+', tools_text)]
                    result["tools"] = [{"item": tool} for tool in tools if tool]
                    return result
                elif field == "service":
                    services_text = match.group(1).strip()
                    services = [s.strip() for s in re.split(r',|\s+and\s+', services_text)]
                    result["services"] = [{"task": service} for service in services if service]
                    if chat_id and chat_id in session_data:
                        # Just return the new services, merging will be handled later
                        return {"services": [{"task": service} for service in services if service]}
                    else:
                        # Only return the complete result if we're not in a chat context
                        return result 
                elif field == "activity":
                    activities_text = match.group(1).strip()
                    activities = [a.strip() for a in re.split(r',|\s+and\s+', activities_text)]
                    result["activities"] = activities
                    return result
                elif field == "issue":
                    issues_text = match.group(1).strip()
                    issues = [i.strip() for i in re.split(r';|,|\s+and\s+', issues_text)]
                    result["issues"] = []
                    for issue in issues:
                        if issue:
                            has_photo = "photo" in issue.lower() or "picture" in issue.lower() or "took a" in issue.lower()
                            result["issues"].append({"description": issue, "has_photo": has_photo})
                    return result
                elif field == "time":
                    result["time"] = match.group(1).strip()
                    return result
                elif field == "weather":
                    result["weather"] = match.group(1).strip()
                    return result
                elif field == "impression":
                    result["impression"] = match.group(1).strip()
                    return result
                elif field == "comments":
                    result["comments"] = match.group(1).strip()
                    return result
                elif field == "people":
                    people_text = match.group(1).strip()
                    role_text = match.group(2).strip() if len(match.groups()) > 1 and match.group(2) else None
                    
                    # Clean up the people text - remove any "add" at the beginning
                    people_text = re.sub(r'^add\s+', '', people_text, flags=re.IGNORECASE)
                    
                    people = [p.strip() for p in re.split(r',|\s+and\s+', people_text)]
                    result["people"] = people
                    
                    if role_text:
                        # Assign the role to all people in the list
                        result["roles"] = [{"name": person, "role": role_text} for person in people]
                    
                    return result
                elif field == "role":
                    # This pattern has multiple group captures for different variations
                    name = None
                    role = None
                    
                    # Find the non-None groups for name and role
                    for i in range(1, len(match.groups()) + 1):
                        if match.group(i):
                            if name is None:
                                name = match.group(i).strip()
                            elif role is None:
                                role = match.group(i).strip()
                    
                    if name and role:
                        result["people"] = [name]
                        result["roles"] = [{"name": name, "role": role}]
                    
                    return result
                elif field == "supervisor":
                    name = match.group(1).strip()
                    result["people"] = [name]
                    result["roles"] = [{"name": name, "role": "Supervisor"}]
                    return result
                elif field == "delete":
                    # Parse different delete syntax patterns
                    groups = match.groups()
                    category = None
                    value = None
                    
                    if groups[0] and groups[1]:  # "delete value from category"
                        value = groups[0].strip()
                        category = groups[1].lower()
                    elif groups[2] and groups[3]:  # "delete category value"
                        category = groups[2].lower()
                        value = groups[3].strip() if groups[3] else None
                    elif groups[4] and groups[5]:  # "category delete value"
                        category = groups[4].lower()
                        value = groups[5].strip() if groups[5] else None
                    elif groups[6]:  # "delete value" (no category)
                        value = groups[6].strip()
                    
                    # Map category names to field names
                    if category:
                        category = FIELD_MAPPING.get(category, category)
                    
                    return {"delete": {"category": category, "value": value}}
                elif field == "delete_entire":
                    field_name = match.group(1).lower()
                    mapped_field = FIELD_MAPPING.get(field_name, field_name)
                    return {mapped_field: {"delete": True}}
                elif field == "correct":
                    raw_field = match.group(1).lower()
                    old_value = match.group(2).strip() if match.group(2) else None
                    new_value = match.group(3).strip() if match.group(3) else None
                    field_name = FIELD_MAPPING.get(raw_field, raw_field)
                    
                    if old_value:
                        if new_value:
                            return {"correct": [{"field": field_name, "old": old_value, "new": new_value}]}
                        else:
                            return {"spelling_correction": {"field": field_name, "old_value": old_value}}
                elif field == "reset":
                    return {"reset": True}
                elif field == "undo_last":
                    return {"undo_last": True}
                elif field == "help":
                    topic = match.group(1) or match.group(2) or "general"
                    return {"help": topic.lower()}
                elif field == "summary":
                    return {"summary": True}
                elif field == "detailed":
                    return {"detailed": True}
                elif field == "export_pdf":
                    return {"export_pdf": True}
                elif field == "sharepoint":
                    return {"sharepoint_export": True}
                elif field == "sharepoint_status":
                    return {"sharepoint_status": True}
                elif field == "clear":
                    field_name = match.group(1).lower()
                    field_name = FIELD_MAPPING.get(field_name, field_name)
                    result[field_name] = [] if field_name in LIST_FIELDS else ""
                    return result
        
        # Handle direct "add issue X" commands
        issue_add_pattern = r'^(?:add|insert)\s+issues?\s+(.+)$'
        issue_add_match = re.match(issue_add_pattern, normalized_text, re.IGNORECASE)
        if issue_add_match:
            issue_text = issue_add_match.group(1).strip()
            if issue_text:
                has_photo = "photo" in issue_text.lower() or "picture" in issue_text.lower() or "took a" in issue_text.lower()
                return {"issues": [{"description": issue_text, "has_photo": has_photo}]}
        
        # Handle direct "add activity X" commands
        activity_add_pattern = r'^(?:add|insert)\s+activit(?:y|ies)\s+(.+)$'
        activity_add_match = re.match(activity_add_pattern, normalized_text, re.IGNORECASE)
        if activity_add_match:
            activity_text = activity_add_match.group(1).strip()
            if activity_text:
                return {"activities": [activity_text]}
        
        # Handle direct "tools: X" commands
        tool_pattern = r'^tools?:?\s+(.+)$'
        tool_match = re.match(tool_pattern, normalized_text, re.IGNORECASE)
        if tool_match:
            tool_text = tool_match.group(1).strip()
            if tool_text:
                tools = [t.strip() for t in re.split(r',|\s+and\s+', tool_text)]
                return {"tools": [{"item": tool} for tool in tools if tool]}
        
        # Free-form report handling - only if we get here and no structured command matched
        if len(text) > 50:
            log_event("detected_free_form_report", length=len(text))
            
            # Extract site name
            site_pattern = r'(?:from|at|on|reporting\s+(?:from|at))\s+(?:the\s+)?([A-Za-z0-9\s]+?)(?:\s*(?:project|site|location)(?:,|\.|$|\s+section|\s+segment))'
            site_match = re.search(site_pattern, text, re.IGNORECASE)
            if site_match:
                result["site_name"] = site_match.group(1).strip()
            
            # ... rest of free-form extraction logic ...
            
            # If we found at least a site name or people, consider it a valid report
            if result.get("site_name") or result.get("people"):
                log_event("free_form_extraction_success", found_fields=list(result.keys()))
                result["date"] = datetime.now().strftime("%d-%m-%Y")
                return result
        
        # If we reach here, no structured or free-form extraction worked
        log_event("fields_extracted", result_fields=len(result))
        return result
    except Exception as e:
        log_event("extract_fields_error", input=text[:100], error=str(e))
        print(f"ERROR in extract_fields: {str(e)}")
        # Return a minimal result to avoid breaking the app
        return {"error": str(e)}

def preserve_existing_data(chat_id, new_data):
    """Make sure we don't lose existing data when adding new items"""
    if chat_id in session_data and "structured_data" in session_data[chat_id]:
        existing_data = session_data[chat_id]["structured_data"]
        # Preserve all existing fields that aren't in the new data
        for field in existing_data:
            if field not in new_data:
                new_data[field] = existing_data[field]
    return new_data

# Part 10 - b Merge Data Function

def merge_data(existing_data: Dict[str, Any], new_data: Dict[str, Any], chat_id: str) -> Dict[str, Any]:
    """Merge new data with existing data, handling special cases"""
    result = existing_data.copy()
    changes = []
    
    # Handle special operation: delete items
    if "delete" in new_data:
        delete_info = new_data.pop("delete")
        category = delete_info.get("category")
        value = delete_info.get("value")
        
        if category and value:  # Both category and value provided
            # Save last state for undo
            if category in existing_data:
                session_data[chat_id]["last_change_history"].append((category, existing_data[category].copy() if isinstance(existing_data[category], list) else existing_data[category]))
            
            if category in SCALAR_FIELDS:
                # Clear scalar field
                result[category] = ""
                changes.append(f"cleared {category}")
            elif category in LIST_FIELDS:
                if category == "people":
                    # Remove person and their roles
                    person_match = None
                    for person in result["people"]:
                        if string_similarity(person.lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                            person_match = person
                            break
                    
                    if person_match:
                        result["people"].remove(person_match)
                        # Also remove from roles
                        session_data[chat_id]["last_change_history"].append(("roles", existing_data["roles"].copy()))
                        result["roles"] = [r for r in result["roles"] 
                                        if not (isinstance(r, dict) and "name" in r and 
                                                string_similarity(r["name"].lower(), person_match.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"])]
                        changes.append(f"removed person '{person_match}' and their roles")
                
                elif category == "companies":
                    # Remove company
                    removed = False
                    for i, company in enumerate(result["companies"]):
                        if isinstance(company, dict) and "name" in company:
                            if company["name"].lower() == value.lower() or string_similarity(company["name"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                del result["companies"][i]
                                changes.append(f"removed company '{company['name']}'")
                                removed = True
                                break
                    
                    if not removed:
                        changes.append(f"no company '{value}' found to remove")
                
                elif category == "tools":
                    # Remove tool
                    removed = False
                    for i, tool in enumerate(result["tools"]):
                        if isinstance(tool, dict) and "item" in tool:
                            if string_similarity(tool["item"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                del result["tools"][i]
                                changes.append(f"removed tool '{tool['item']}'")
                                removed = True
                                break
                    
                    if not removed:
                        changes.append(f"no tool '{value}' found to remove")
                
                elif category == "services":
                    # Remove service
                    removed = False
                    for i, service in enumerate(result["services"]):
                        if isinstance(service, dict) and "task" in service:
                            if string_similarity(service["task"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                del result["services"][i]
                                changes.append(f"removed service '{service['task']}'")
                                removed = True
                                break
                    
                    if not removed:
                        changes.append(f"no service '{value}' found to remove")
                
                elif category == "activities":
                    # Remove activity
                    removed = False
                    for i, activity in enumerate(result["activities"]):
                        if string_similarity(activity.lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                            del result["activities"][i]
                            changes.append(f"removed activity '{activity}'")
                            removed = True
                            break
                    
                    if not removed:
                        changes.append(f"no activity '{value}' found to remove")
                
                elif category == "issues":
                    # Remove issue
                    removed = False
                    for i, issue in enumerate(result["issues"]):
                        if isinstance(issue, dict) and "description" in issue:
                            if string_similarity(issue["description"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                del result["issues"][i]
                                changes.append(f"removed issue '{issue['description']}'")
                                removed = True
                                break
                    
                    if not removed:
                        changes.append(f"no issue '{value}' found to remove")
        
        elif value and not category:  # Only value provided, try to find it in any field
            # Try scalar fields first
            for field in SCALAR_FIELDS:
                if field in result and result[field] and string_similarity(result[field].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field]))
                    result[field] = ""
                    changes.append(f"cleared {field} '{value}'")
                    break
            
            # If no scalar field matched, try list fields
            if not changes:
                # Try people
                for person in list(result["people"]):
                    if string_similarity(person.lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                        session_data[chat_id]["last_change_history"].append(("people", existing_data["people"].copy()))
                        result["people"].remove(person)
                        
                        # Also remove from roles
                        session_data[chat_id]["last_change_history"].append(("roles", existing_data["roles"].copy()))
                        result["roles"] = [r for r in result["roles"] 
                                        if not (isinstance(r, dict) and "name" in r and 
                                                string_similarity(r["name"].lower(), person.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"])]
                        changes.append(f"removed person '{person}' and their roles")
                        break
                
                # Try companies
                if not changes:
                    for i, company in enumerate(result["companies"]):
                        if isinstance(company, dict) and "name" in company:
                            if string_similarity(company["name"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                session_data[chat_id]["last_change_history"].append(("companies", existing_data["companies"].copy()))
                                del result["companies"][i]
                                changes.append(f"removed company '{company['name']}'")
                                break
                
                # Try tools
                if not changes:
                    for i, tool in enumerate(result["tools"]):
                        if isinstance(tool, dict) and "item" in tool:
                            if string_similarity(tool["item"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                session_data[chat_id]["last_change_history"].append(("tools", existing_data["tools"].copy()))
                                del result["tools"][i]
                                changes.append(f"removed tool '{tool['item']}'")
                                break
                
                # Try services
                if not changes:
                    for i, service in enumerate(result["services"]):
                        if isinstance(service, dict) and "task" in service:
                            if string_similarity(service["task"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                session_data[chat_id]["last_change_history"].append(("services", existing_data["services"].copy()))
                                del result["services"][i]
                                changes.append(f"removed service '{service['task']}'")
                                break
                
                # Try activities
                if not changes:
                    for i, activity in enumerate(result["activities"]):
                        if string_similarity(activity.lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                            session_data[chat_id]["last_change_history"].append(("activities", existing_data["activities"].copy()))
                            del result["activities"][i]
                            changes.append(f"removed activity '{activity}'")
                            break
                
                # Try issues
                if not changes:
                    for i, issue in enumerate(result["issues"]):
                        if isinstance(issue, dict) and "description" in issue:
                            if string_similarity(issue["description"].lower(), value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                session_data[chat_id]["last_change_history"].append(("issues", existing_data["issues"].copy()))
                                del result["issues"][i]
                                changes.append(f"removed issue '{issue['description']}'")
                                break
    
    # Handle deletions of entire categories
    for field in LIST_FIELDS:
        if field in new_data and isinstance(new_data[field], dict) and new_data[field].get("delete") is True:
            # Save last state for undo
            session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
            
            result[field] = []
            changes.append(f"deleted all {field}")
            
            # Also clear related fields
            if field == "people":
                session_data[chat_id]["last_change_history"].append(("roles", existing_data["roles"].copy()))
                result["roles"] = []
                changes.append("deleted all roles")
            
            # Remove this processed field from new_data
            new_data.pop(field)
    
    # Handle correcting values
    if "correct" in new_data:
        corrections = new_data.pop("correct")
        for correction in corrections:
            field = correction.get("field")
            old_value = correction.get("old")
            new_value = correction.get("new")
            
            if not field or not old_value or not new_value:
                continue
                
            if field in SCALAR_FIELDS:
                # Save last state for undo
                session_data[chat_id]["last_change_history"].append((field, existing_data[field]))
                
                # Simple replace for scalar fields
                if string_similarity(result[field].lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                    result[field] = new_value
                    changes.append(f"corrected {field} '{old_value}' to '{new_value}'")
            elif field in LIST_FIELDS:
                # More complex handling for list fields
                if field == "people":
                    # Save last state for undo
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
                    session_data[chat_id]["last_change_history"].append(("roles", existing_data["roles"].copy()))
                    
                    # Update person names
                    matched = False
                    for i, person in enumerate(result[field]):
                        if string_similarity(person.lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                            result[field][i] = new_value
                            matched = True
                            
                            # Also update roles that refer to this person
                            for role in result["roles"]:
                                if (isinstance(role, dict) and role.get("name") and 
                                    string_similarity(role["name"].lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                                    role["name"] = new_value
                            
                            changes.append(f"corrected person '{old_value}' to '{new_value}'")
                            break
                    
                    if not matched:
                        # If no match, add the new person
                        result[field].append(new_value)
                        changes.append(f"added corrected person '{new_value}'")
                elif field == "roles":
                    # Interpret as correcting a role for a person
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
                    
                    matched = False
                    for role in result[field]:
                        if (isinstance(role, dict) and role.get("name") and 
                            string_similarity(role["name"].lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                            role["role"] = new_value.title()
                            matched = True
                            changes.append(f"corrected role for '{old_value}' to '{new_value}'")
                            break
                    
                    if not matched:
                        # If no exact match, try finding the person elsewhere
                        person_name = None
                        for person in result["people"]:
                            if string_similarity(person.lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                person_name = person
                                break
                        
                        if person_name:
                            # Add this person with the new role
                            result[field].append({"name": person_name, "role": new_value.title()})
                            changes.append(f"added role '{new_value}' for '{person_name}'")
                        else:
                            # Add both the person and role
                            result["people"].append(old_value)
                            result[field].append({"name": old_value, "role": new_value.title()})
                            changes.append(f"added person '{old_value}' with role '{new_value}'")
                elif field == "companies":
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
                    
                    matched = False
                    for i, company in enumerate(result[field]):
                        if (isinstance(company, dict) and company.get("name") and 
                            string_similarity(company["name"].lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                            company["name"] = new_value
                            matched = True
                            changes.append(f"corrected company '{old_value}' to '{new_value}'")
                            break
                    
                    if not matched:
                        # If no match, add the new company
                        result[field].append({"name": new_value})
                        changes.append(f"added corrected company '{new_value}'")
                elif field == "tools":
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
                    
                    matched = False
                    for i, tool in enumerate(result[field]):
                        if (isinstance(tool, dict) and tool.get("item") and 
                            string_similarity(tool["item"].lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                            tool["item"] = new_value
                            matched = True
                            changes.append(f"corrected tool '{old_value}' to '{new_value}'")
                            break
                    
                    if not matched:
                        # If no match, add the new tool
                        result[field].append({"item": new_value})
                        changes.append(f"added corrected tool '{new_value}'")
                elif field == "services":
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
                    
                    matched = False
                    for i, service in enumerate(result[field]):
                        if (isinstance(service, dict) and service.get("task") and 
                            string_similarity(service["task"].lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                            service["task"] = new_value
                            matched = True
                            changes.append(f"corrected service '{old_value}' to '{new_value}'")
                            break
                    
                    if not matched:
                        # If no match, add the new service
                        result[field].append({"task": new_value})
                        changes.append(f"added corrected service '{new_value}'")
                elif field == "activities":
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
                    
                    matched = False
                    for i, activity in enumerate(result[field]):
                        if string_similarity(activity.lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                            result[field][i] = new_value
                            matched = True
                            changes.append(f"corrected activity '{old_value}' to '{new_value}'")
                            break
                    
                    if not matched:
                        # If no match, add the new activity
                        result[field].append(new_value)
                        changes.append(f"added corrected activity '{new_value}'")
                elif field == "issues":
                    session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
                    
                    matched = False
                    for i, issue in enumerate(result[field]):
                        if (isinstance(issue, dict) and issue.get("description") and 
                            string_similarity(issue["description"].lower(), old_value.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                            issue["description"] = new_value
                            matched = True
                            changes.append(f"corrected issue '{old_value}' to '{new_value}'")
                            break
                    
                    if not matched:
                        # If no match, add the new issue
                        has_photo = "photo" in old_value.lower() or "photo" in new_value.lower()
                        result[field].append({"description": new_value, "has_photo": has_photo})
                        changes.append(f"added corrected issue '{new_value}'")
    
    # Handle context-based additions
    if "context_add" in new_data:
        context_info = new_data.pop("context_add")
        target_field = context_info.get("field")
        
        context = session_data.get(chat_id, {}).get("context", {})
        last_person = context.get("last_mentioned_person")
        last_item = context.get("last_mentioned_item")
        
        if target_field and (last_person or last_item):
            # For people, companies, roles fields, use last_person
            if target_field in ["people", "companies", "roles"]:
                if last_person:
                    if target_field == "people" and last_person not in result["people"]:
                        # Save last state for undo
                        session_data[chat_id]["last_change_history"].append((target_field, existing_data[target_field].copy()))
                        
                        result["people"].append(last_person)
                        changes.append(f"added last mentioned person '{last_person}' to people")
                    elif target_field == "companies":
                        # Save last state for undo
                        session_data[chat_id]["last_change_history"].append((target_field, existing_data[target_field].copy()))
                        
                        # Check if already exists
                        company_names = [c.get("name", "").lower() for c in result["companies"] if isinstance(c, dict)]
                        if last_person.lower() not in company_names:
                            result["companies"].append({"name": last_person})
                            changes.append(f"added last mentioned person '{last_person}' as company")
                    elif target_field == "roles":
                        # Need a role title, look for "as X" in original text
                        context_add_role = re.search(r"as\s+([a-zA-Z\s]+)", context_info.get("original_text", ""))
                        if context_add_role:
                            role_title = context_add_role.group(1).strip().title()
                            
                            # Save last state for undo
                            session_data[chat_id]["last_change_history"].append((target_field, existing_data[target_field].copy()))
                            
                            # Check if already exists
                            role_exists = False
                            for role in result["roles"]:
                                if (isinstance(role, dict) and role.get("name") and role.get("role") and
                                    role["name"].lower() == last_person.lower() and role["role"].lower() == role_title.lower()):
                                    role_exists = True
                                    break
                            
                            if not role_exists:
                                # Also make sure person is in people list
                                if last_person not in result["people"]:
                                    result["people"].append(last_person)
                                
                                result["roles"].append({"name": last_person, "role": role_title})
                                changes.append(f"added last mentioned person '{last_person}' as {role_title}")
            
            # For other fields, use last_item
            elif target_field in ["tools", "services", "activities", "issues"]:
                if last_item:
                    # Save last state for undo
                    session_data[chat_id]["last_change_history"].append((target_field, existing_data[target_field].copy()))
                    
                    if target_field == "tools":
                        # Check if already exists
                        tool_names = [t.get("item", "").lower() for t in result["tools"] if isinstance(t, dict)]
                        if last_item.lower() not in tool_names:
                            result["tools"].append({"item": last_item})
                            changes.append(f"added last mentioned item '{last_item}' as tool")
                    elif target_field == "services":
                        # Check if already exists
                        service_names = [s.get("task", "").lower() for s in result["services"] if isinstance(s, dict)]
                        if last_item.lower() not in service_names:
                            result["services"].append({"task": last_item})
                            changes.append(f"added last mentioned item '{last_item}' as service")
                    elif target_field == "activities":
                        # Check if already exists
                        if last_item.lower() not in [a.lower() for a in result["activities"]]:
                            result["activities"].append(last_item)
                            changes.append(f"added last mentioned item '{last_item}' as activity")
                    elif target_field == "issues":
                        # Check if already exists
                        issue_descriptions = [i.get("description", "").lower() for i in result["issues"] if isinstance(i, dict)]
                        if last_item.lower() not in issue_descriptions:
                            has_photo = "photo" in last_item.lower() or "picture" in last_item.lower()
                            result["issues"].append({"description": last_item, "has_photo": has_photo})
                            changes.append(f"added last mentioned item '{last_item}' as issue")
    
    # Regular field updates
    for field in new_data:
        # Skip fields we've already processed
        if field in ["reset", "undo", "status", "export_pdf", "help", "summary", "detailed", 
                    "correct_prompt", "error", "yes_confirm", "no_confirm", "spelling_correction",
                    "delete", "correct", "context_add"]:
            continue
            
        # Save state for undo if we're changing a field
        if field in existing_data:
            if field in LIST_FIELDS and existing_data[field] != new_data[field]:
                session_data[chat_id]["last_change_history"].append((field, existing_data[field].copy()))
            elif field in SCALAR_FIELDS and existing_data[field] != new_data[field]:
                session_data[chat_id]["last_change_history"].append((field, existing_data[field]))
        
        # For scalar fields, just replace
        if field in SCALAR_FIELDS:
            result[field] = new_data[field]
            if new_data[field]:  # Only log non-empty
                changes.append(f"updated {field} to '{new_data[field]}'")
        
        # For list fields, handle special cases and append
        elif field in LIST_FIELDS:
            if field in ["companies", "tools", "services", "issues"]:
                # These are dictionaries with specific keys
                if field == "companies":
                    key = "name"
                    existing_values = [c.get(key, "").lower() for c in result[field] if isinstance(c, dict)]
                elif field == "tools":
                    key = "item"
                    existing_values = [t.get(key, "").lower() for t in result[field] if isinstance(t, dict)]
                elif field == "services":
                    key = "task"
                    existing_values = [s.get(key, "").lower() for s in result[field] if isinstance(s, dict)]
                elif field == "issues":
                    key = "description"
                    existing_values = [i.get(key, "").lower() for i in result[field] if isinstance(i, dict)]
                
                # Add new items
                for item in new_data[field]:
                    if isinstance(item, dict) and key in item:
                        item_value = item[key].lower()
                        
                        # Check if this item already exists
                        already_exists = False
                        for existing_value in existing_values:
                            if string_similarity(item_value, existing_value) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                already_exists = True
                                break
                                
                        if not already_exists:
                            result[field].append(item)
                            changes.append(f"added {field[:-1]} '{item[key]}'")
                        else:
                            log_event("skipped_duplicate", field=field, value=item[key])
            
            elif field == "roles":
                # Handle roles specially to update people list too
                existing_roles = [(r.get("name", "").lower(), r.get("role", "").lower()) 
                                for r in result[field] if isinstance(r, dict)]
                
                for role in new_data[field]:
                    if isinstance(role, dict) and "name" in role and "role" in role:
                        role_tuple = (role["name"].lower(), role["role"].lower())
                        
                        # Check if this role already exists
                        already_exists = False
                        for existing_name, existing_role in existing_roles:
                            if (string_similarity(role["name"].lower(), existing_name) >= CONFIG["NAME_SIMILARITY_THRESHOLD"] and
                                string_similarity(role["role"].lower(), existing_role) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]):
                                already_exists = True
                                break
                                
                        if not already_exists:
                            result[field].append(role)
                            changes.append(f"added role {role['role']} for {role['name']}")
                            
                            # Also make sure the person is in the people list
                            person_exists = False
                            for person in result["people"]:
                                if string_similarity(role["name"].lower(), person.lower()) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                                    person_exists = True
                                    break
                                    
                            if not person_exists:
                                result["people"].append(role["name"])
                                changes.append(f"added person {role['name']}")
                        else:
                            log_event("skipped_duplicate_role", name=role["name"], role=role["role"])
            
            elif field in ["people", "activities"]:
                # Simple string lists
                existing_values = [v.lower() for v in result[field]]
                
                for item in new_data[field]:
                    # Check if this item already exists
                    already_exists = False
                    for existing_value in existing_values:
                        if string_similarity(item.lower(), existing_value) >= CONFIG["NAME_SIMILARITY_THRESHOLD"]:
                            already_exists = True
                            break
                            
                    if not already_exists:
                        result[field].append(item)
                        changes.append(f"added {field[:-1]} '{item}'")
                    else:
                        log_event("skipped_duplicate", field=field, value=item)
    
    if changes:
        log_event("merged_data", changes=changes)
    
    return result
    
    # Part 11 Command Handlers
# ADD the format_response function here:
def format_response(message_type: str, message: str, data: Dict[str, Any] = None) -> str:
    """Format response messages consistently"""
    
    # Define emojis for each message type
    emojis = {
        "success": "✅",
        "error": "⚠️",
        "info": "ℹ️",
        "question": "❓",
        "warning": "⚠️",
        "reset": "🔄",
        "export": "📤",
        "help": "📚",
        "summary": "📋"
    }
    
    emoji = emojis.get(message_type, "")
    
    # Format the message with emoji
    formatted_message = f"{emoji} {message}"
    
    # Add structured data if provided
    if data and "structured_data" in data:
        summary = summarize_report(data["structured_data"])
        formatted_message = f"{formatted_message}\n\n{summary}"
        
    return formatted_message

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
    # Check if we're awaiting confirmation
    if session.get("awaiting_reset_confirmation", False):
        # User has already confirmed
        session["awaiting_reset_confirmation"] = False
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
        
        send_message(chat_id, f"**Report reset**\n\n{summary}\n\nSpeak or type your first category (e.g., 'add site Downtown Project').")
    else:
        # Request confirmation first
        if any(field for field in session.get("structured_data", {}).values() if field):
            session["awaiting_reset_confirmation"] = True
            save_session(session_data)
            send_message(chat_id, "⚠️ This will delete your current report. Are you sure you want to start a new report? Reply 'yes' to confirm or 'no' to cancel.")
        else:
            # If report is empty, no need for confirmation
            session["structured_data"] = blank_report()
            save_session(session_data)
            summary = summarize_report(session["structured_data"])
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
@command("export report")
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

@command("sharepoint")
@command("export to sharepoint")
def handle_sharepoint_export(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle SharePoint export command"""
    if not CONFIG["ENABLE_SHAREPOINT"]:
        send_message(chat_id, "⚠️ SharePoint integration is not enabled. Please contact your administrator.")
        return
    
    send_message(chat_id, "Uploading report to SharePoint... This may take a moment.")
    
    result = sync_to_sharepoint(chat_id, session["structured_data"])
    
    if result["success"]:
        message = "✅ Report successfully uploaded to SharePoint!\n\n"
        if result.get("file_url"):
            message += f"PDF report saved at: {result['file_url']}\n"
        if result.get("list_item_id"):
            message += f"List item created with ID: {result['list_item_id']}"
            
        send_message(chat_id, message)
    else:
        send_message(chat_id, f"⚠️ Failed to upload to SharePoint: {result.get('error', 'Unknown error')}")

@command("sharepoint status")
def handle_sharepoint_status(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle SharePoint status command"""
    if not CONFIG["ENABLE_SHAREPOINT"]:
        send_message(chat_id, "⚠️ SharePoint integration is not enabled. Please contact your administrator.")
        return
    
    sharepoint_status = session.get("sharepoint_status", {
        "synced": False,
        "last_sync": None,
        "list_item_id": None,
        "file_url": None,
        "sync_errors": []
    })
    
    message = "**SharePoint Status**\n\n"
    
    if sharepoint_status.get("synced"):
        message += "✅ Report is synced to SharePoint\n"
        if sharepoint_status.get("last_sync"):
            message += f"Last sync: {sharepoint_status['last_sync']}\n"
        if sharepoint_status.get("list_item_id"):
            message += f"List item ID: {sharepoint_status['list_item_id']}\n"
        if sharepoint_status.get("file_url"):
            message += f"PDF report URL: {sharepoint_status['file_url']}\n"
    else:
        message += "⚠️ Report is not synced to SharePoint\n"
        
        if sharepoint_status.get("sync_errors"):
            message += "\nRecent sync errors:\n"
            for error in sharepoint_status["sync_errors"][-3:]:  # Show last 3 errors
                message += f"• {error.get('timestamp', '')}: {error.get('error', 'Unknown error')}\n"
    
    send_message(chat_id, message)

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
            "• Export report: 'export pdf' or 'export report'\n"
            "• Reset report: 'reset' or 'new report'\n"
            "• Undo changes: 'undo' or 'undo last'\n"
            "• Get status: 'status'\n\n"
            "For help on specific topics, type 'help [topic]' where topic can be: fields, commands, adding, deleting, examples, sharepoint"
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
            "• export/export pdf/export report - Generate PDF report\n"
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
        ),
        "sharepoint": (
            "**SharePoint Integration**\n\n"
            "You can save your reports directly to SharePoint:\n\n"
            "• 'export to sharepoint' - Upload current report to SharePoint\n"
            "• 'sharepoint status' - Check sync status with SharePoint\n\n"
            "Your report will be saved to a SharePoint list and the PDF will be uploaded to a document library. "
            "If the SharePoint integration is not enabled, please contact your administrator."
        )
    }
    
    # Get the appropriate help text
    message = help_text.get(topic.lower(), help_text["general"])
    send_message(chat_id, message)

@command("greeting")
def handle_greeting(chat_id: str, session: Dict[str, Any]) -> None:
    """Handle greeting messages"""
    import random
    greetings = [
        "Hello! How can I help with your construction site report today?",
        "Hi there! Need help with a construction report?",
        "Hello! Would you like to create a new report or continue your existing one?",
        "Hi! I'm your construction report assistant. What would you like to do today?"
    ]
    send_message(chat_id, random.choice(greetings))


@command("start")
def handle_start(chat_id: str, session: Dict[str, Any]) -> None:
    """Help new users get started"""
    message = (
        "👋 Welcome to the Construction Site Report Bot!\n\n"
        "Here's how to create a report:\n"
        "1️⃣ Say 'site: [location]' to set your site\n"
        "2️⃣ Add people with 'people: [names]'\n"
        "3️⃣ Add companies with 'companies: [names]'\n"
        "4️⃣ Continue adding other details\n"
        "5️⃣ Say 'export pdf' when you're done\n\n"
        "Type 'help' any time for more information."
    )
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
COMMAND_HANDLERS["export to sharepoint"] = handle_sharepoint_export
COMMAND_HANDLERS["sharepoint status"] = handle_sharepoint_status

def recognize_intent(text: str) -> Dict[str, Any]:
    """Recognize user intent from conversational messages"""
    # Check for reset/new report intent
    reset_phrases = [
        "create a new report", "start a new report", "make a new report",
        "begin a new report", "start over", "start fresh"
    ]
    
    for phrase in reset_phrases:
        if phrase in text.lower():
            return {"reset": True}
    
    delete_match = re.search(r"delete\s+(.+?)(?:\s+from\s+(.+))?", text.lower())
    if delete_match:
        category = delete_match.group(2)
        value = delete_match.group(1)
        if category:
            return {"delete": {"category": category, "value": value}}
        elif value in ["issues", "activities", "comments", "tools", "services", "companies", "people", "roles"]:
            return {value: {"delete": True}}
        else:
            return {"delete": {"value": value}}
    
    return {}

# Part 12 Handle Commands 
def handle_command(chat_id: str, text: str, session: Dict[str, Any]) -> tuple[str, int]:
    """Process user command and update session data"""
    try:
        # Update last interaction time
        session["last_interaction"] = time()
        
        # Handle confirmation for reset command
        if session.get("awaiting_reset_confirmation", False):
            if text.lower() in ["yes", "y", "yeah", "yep", "sure", "ok", "okay"] or re.match(FIELD_PATTERNS["yes_confirm"], text, re.IGNORECASE):
                COMMAND_HANDLERS["reset"](chat_id, session)
                return "ok", 200
            elif text.lower() in ["no", "n", "nope", "nah"] or re.match(FIELD_PATTERNS["no_confirm"], text, re.IGNORECASE):
                session["awaiting_reset_confirmation"] = False
                save_session(session_data)
                send_message(chat_id, "Reset cancelled. Your report was not changed.")
                return "ok", 200
        # Handle greetings
        if re.match(FIELD_PATTERNS["greeting"], text, re.IGNORECASE):
            handle_greeting(chat_id, session)
            return "ok", 200
        
        # Handle conversational intents
        if re.match(FIELD_PATTERNS["conversation"], text, re.IGNORECASE):
            conversation_match = re.match(FIELD_PATTERNS["conversation"], text, re.IGNORECASE)
            intent_text = conversation_match.group(1)
            intent = recognize_intent(intent_text)
            if intent:
                # For reset intent, send confirmation message
                if "reset" in intent:
                    send_message(chat_id, "You want to start a new report? Please confirm with 'yes'.")
                    session["awaiting_reset_confirmation"] = True
                    save_session(session_data)
                    return "ok", 200
                # For other intents, process them
                extracted = intent
                # Continue with regular command processing
            else:
                send_message(chat_id, "I'm not sure what you want to do. Try being more specific or use commands like 'add site Downtown Project'.")
                return "ok", 200
                
        # Handle spelling correction confirmations
        if session.get("awaiting_spelling_correction", {}).get("active", False):
            if re.match(FIELD_PATTERNS["yes_confirm"], text, re.IGNORECASE):
                field = session["awaiting_spelling_correction"]["field"]
                old_value = session["awaiting_spelling_correction"]["old_value"]
                session["awaiting_spelling_correction"] = {
                    "active": True,
                    "field": field,
                    "old_value": old_value,
                    "awaiting_new_value": True
                }
                save_session(session_data)
                send_message(chat_id, f"Please enter the correct spelling for '{old_value}' in {field}:")
                return "ok", 200
            elif re.match(FIELD_PATTERNS["no_confirm"], text, re.IGNORECASE):
                session["awaiting_spelling_correction"] = {"active": False, "field": None, "old_value": None}
                save_session(session_data)
                send_message(chat_id, "Correction cancelled.")
                return "ok", 200
            else:
                field = session["awaiting_spelling_correction"]["field"]
                old_value = session["awaiting_spelling_correction"]["old_value"]
                new_value = text.strip()
                session["awaiting_spelling_correction"] = {"active": False, "field": None, "old_value": None}
                extracted = {"correct": [{"field": field, "old": old_value, "new": new_value}]}
                session["command_history"].append(session["structured_data"].copy())
                session["structured_data"] = merge_data(session["structured_data"], extracted, chat_id)
                save_session(session_data)
                summary = summarize_report(session["structured_data"])
                send_message(chat_id, f"✅ Corrected {field} from '{old_value}' to '{new_value}'.\n\n{summary}")
                return "ok", 200
                
        # Check for exact command matches
        clean_text = text.lower().strip()
        if clean_text in COMMAND_HANDLERS:
            COMMAND_HANDLERS[clean_text](chat_id, session)
            return "ok", 200
        
        # Notify user for free-form reports
        if CONFIG["ENABLE_FREEFORM_EXTRACTION"] and is_free_form_report(text):
            send_message(chat_id, "I noticed you sent a detailed report. I'll try to extract all the information from it...")
        
        # For free-form reports, make sure to use NLP extraction
        if CONFIG["ENABLE_NLP_EXTRACTION"]:
            nlp_data, confidence = extract_with_nlp(text)
            if confidence >= CONFIG["NLP_EXTRACTION_CONFIDENCE_THRESHOLD"]:
                log_event("free_form_nlp_extraction", confidence=confidence)
                session_data[chat_id]["command_history"].append(session_data[chat_id]["structured_data"].copy())
                session_data[chat_id]["structured_data"] = merge_data(
                    session_data[chat_id]["structured_data"], 
                    nlp_data, 
                    chat_id
                )
                save_session(session_data)
                summary = summarize_report(session_data[chat_id]["structured_data"])
                send_message(chat_id, f"✅ I've extracted the following information from your report:\n\n{summary}")
                return "ok", 200

        # Extract fields from input
        extracted = extract_fields(text)
        
        # Handle empty or invalid extractions
        if not extracted or "error" in extracted:
            debug_results = debug_command_matching(text, chat_id)
            if debug_results:
                matched_fields = [r["field"] for r in debug_results if r["matched"]]
                if matched_fields:
                    send_message(chat_id, f"I recognized patterns for {', '.join(matched_fields)} but couldn't process the input. Please clarify (e.g., 'add {matched_fields[0]} <value>').")
                else:
                    send_message(chat_id, "I didn't understand that command. Debug info: no patterns matched. Type 'help' for assistance.")
            else:
                send_message(chat_id, "I didn't understand that. Type 'help' for assistance or try saying one category at a time.")
            return "ok", 200
        
        # Process special commands
        if any(key in extracted for key in ["reset", "undo", "status", "help", "summary", 
                                        "detailed", "export_pdf", "undo_last",
                                        "sharepoint", "sharepoint_status",
                                        "yes_confirm", "no_confirm", "spelling_correction"]):
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
            elif "sharepoint" in extracted:
                handle_sharepoint_export(chat_id, session)
            elif "sharepoint_status" in extracted:
                handle_sharepoint_status(chat_id, session)
            elif "help" in extracted:
                topic = extracted.get("help", "general")
                handle_help(chat_id, session, topic)
            elif "yes_confirm" in extracted and session.get("awaiting_reset_confirmation"):
                handle_reset(chat_id, session)
            elif "no_confirm" in extracted and session.get("awaiting_reset_confirmation"):
                session["awaiting_reset_confirmation"] = False
                save_session(session_data)
                send_message(chat_id, "Reset cancelled. Your report was not changed.")
            elif "spelling_correction" in extracted:
                field = extracted["spelling_correction"]["field"]
                old_value = extracted["spelling_correction"]["old_value"]
                session["awaiting_spelling_correction"] = {
                    "active": True,
                    "field": field,
                    "old_value": old_value
                }
                save_session(session_data)
                send_message(chat_id, f"Do you want to correct '{old_value}' in {field}? Please reply with 'yes' or 'no'.")
            return "ok", 200

        # Handle field updates
        session["command_history"].append(session["structured_data"].copy())
        session["structured_data"] = merge_data(session["structured_data"], extracted, chat_id)
        session["structured_data"] = enrich_date(session["structured_data"])
        save_session(session_data)

        # Prepare feedback
        changed_fields = [field for field in extracted.keys() 
                        if field not in ["help", "reset", "undo", "status", "export_pdf", 
                                        "summary", "detailed", "undo_last", "error",
                                        "sharepoint", "sharepoint_status",
                                        "yes_confirm", "no_confirm", "spelling_correction"]]
        
        if changed_fields:
            message = "✅ Updated report."
            if "delete" in extracted:
                message = "✅ Deleted information from your report."
            elif "delete_entire" in extracted:
                message = "✅ Cleared entire field from your report."
            elif "correct" in extracted:
                message = "✅ Corrected information in your report."

            # ... keep any other existing code here ...

            summary = summarize_report(session["structured_data"])
            send_message(chat_id, f"{message}\n\n{summary}")
            
            # Add intelligent suggestions only if changes were actually made
            if changed_fields:
                missing_suggestions = suggest_missing_fields(session["structured_data"])
                if missing_suggestions:
                    suggestion_text = "You might also want to add: " + ", ".join(missing_suggestions)
                    send_message(chat_id, suggestion_text)
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

def process_chained_commands(text: str, chat_id: str) -> List[Dict[str, Any]]:
    """Process multiple commands in a single message"""
    # Split text on semicolons and periods that aren't part of numbers
    command_texts = re.split(r'(?<!\d)[;.]\s+', text)
    
    results = []
    for cmd_text in command_texts:
        if not cmd_text.strip():
            continue
            
        extracted = extract_fields(cmd_text, chat_id)
        if extracted and "error" not in extracted:
            results.append(extracted)
            log_event("chained_command_extracted", text=cmd_text, fields=list(extracted.keys()))
    
    return results

# Part 13 Construction Site Report Bot

@app.route("/webhook", methods=["POST"])
def webhook() -> tuple[str, int]:
    """Handle incoming webhook from Telegram"""
    try:
        data = request.get_json()
        if not data:
            log_event("webhook_invalid_data", error="No JSON data received")
            return "error", 400

        log_event("webhook_received", data=data)
        
        # Ignore messages without a message object
        if "message" not in data:
            log_event("webhook_no_message", data=data)
            return "ok", 200
            
        message = data["message"]
        
        # Ignore messages without a chat
        if "chat" not in message:
            log_event("webhook_no_chat", message=message)
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
                "report_format": CONFIG["REPORT_FORMAT"],
                "sharepoint_status": {
                    "synced": False,
                    "last_sync": None,
                    "list_item_id": None,
                    "file_url": None,
                    "sync_errors": []
                },
                "awaiting_reset_confirmation": False,
                "awaiting_spelling_correction": {
                    "active": False,
                    "field": None,
                    "old_value": None
                }
            }
            save_session(session_data)
        
        # Handle voice messages
        if "voice" in message:
            try:
                file_id = message["voice"]["file_id"]
                if message["voice"].get("duration", 0) > 20:  # If longer than 20 seconds
                    send_message(chat_id, "I'm processing your detailed report. This may take a moment...")
                text, confidence = transcribe_voice(file_id)
                
                # For short commands (less than 5 words), lower the threshold
                if len(text.split()) < 5 and any(cmd in text.lower() for cmd in ["delete", "add", "category", "reset", "export"]):
                    confidence_threshold = 0.3
                else:
                    confidence_threshold = 0.5

                if not text or (confidence < confidence_threshold and not any(re.match(pattern, text, re.IGNORECASE) for pattern in [
                    # patterns
                ])):
                    log_event("low_confidence_transcription", text=text, confidence=confidence)
                    error_message = "⚠️ I couldn't clearly understand your voice message."
                    if text:
                        error_message += f" I heard: '{text}'."
                    
                    error_message += "\n\nWhen recording, try to:\n• Speak clearly and slowly\n• Reduce background noise\n• Keep the phone close to your mouth"
                    send_message(chat_id, error_message)
                    return "ok", 200
                
                if CONFIG["ENABLE_FREEFORM_EXTRACTION"] and is_free_form_report(text):
                    send_message(chat_id, "Processing your detailed report...")
                
                log_event("processing_voice_command", text=text, confidence=confidence)
                return handle_command(chat_id, text, session_data[chat_id])
                
                

            except Exception as e:
                log_event("voice_processing_error", error=str(e))
                send_message(chat_id, "⚠️ There was an error processing your voice message. Please try again or type your message.")
                return "ok", 200
    
        # Handle text messages
        if "text" in message:
            text = message["text"].strip()
            
            # Handle reset confirmation
            if session_data[chat_id].get("awaiting_reset_confirmation", False):
                # ... keep existing reset confirmation code ...
                return "ok", 200
            
            # Handle spelling correction
            if session_data[chat_id].get("awaiting_spelling_correction", {}).get("active", False):
                # ... keep existing spelling correction code ...
                return "ok", 200
            
            # Check for command chaining
            if ";" in text or re.search(r'(?<!\d)\.\s+[A-Za-z]', text):
                chained_commands = process_chained_commands(text, chat_id)
                
                if chained_commands:
                    # Process each command
                    for i, extracted in enumerate(chained_commands):
                        # Skip the first save_state to avoid duplicating
                        if i == 0:
                            session_data[chat_id]["command_history"].append(session_data[chat_id]["structured_data"].copy())
                        
                        session_data[chat_id]["structured_data"] = merge_data(
                            session_data[chat_id]["structured_data"], 
                            extracted, 
                            chat_id
                        )
                    
                    session_data[chat_id]["structured_data"] = enrich_date(session_data[chat_id]["structured_data"])
                    save_session(session_data)
                    
                    send_message(chat_id, f"✅ Processed {len(chained_commands)} commands.\n\n{summarize_report(session_data[chat_id]['structured_data'])}")
                    return "ok", 200
            
            # Regular single command processing
            log_event("processing_text_command", text=text)
            return handle_command(chat_id, text, session_data[chat_id])
        
        # Handle other types of messages
        send_message(chat_id, "⚠️ I can only process text and voice messages. Please try again.")
        return "ok", 200
        
    except Exception as e:
        log_event("webhook_error", error=str(e))
        try:
            if 'chat_id' in locals():
                send_message(chat_id, "⚠️ An error occurred while processing your request. Please try again.")
            else:
                log_event("webhook_no_chat_id", error="Cannot send error message due to missing chat_id")
        except Exception as send_error:
            log_event("webhook_send_error", error=str(send_error))
        return "error", 500

# Health check endpoint
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for load balancers and monitoring"""
    return jsonify({
        "status": "healthy",
        "version": "1.2.0",  # Incremented version to reflect bug fixes
        "telegram_connected": bool(TELEGRAM_TOKEN),
        "openai_connected": bool(OPENAI_API_KEY),
        "sharepoint_enabled": CONFIG["ENABLE_SHAREPOINT"],
        "free_form_extraction": CONFIG["ENABLE_FREEFORM_EXTRACTION"],
        "bug_fixes": [
            "added confirmation for 'new report' command",
            "fixed deletion of people and items",
            "improved spelling correction handling",
            "added handling for simple 'yes' responses",
            "improved error handling and feedback"
        ]
    }), 200

# Start Flask server if running directly
if __name__ == "__main__":
    # Run Flask app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
