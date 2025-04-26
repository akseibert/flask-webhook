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

# --- Initialize logging ---
logging.basicConfig(
    filename="/opt/render/project/src/app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())

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

def load_session_data():
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE) as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data):
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Missing TELEGRAM_BOT_TOKEN")
        raise ValueError("Missing TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    response.raise_for_status()
    logger.info(f"Sent Telegram message to {chat_id}: {text[:50]}…")
    return response

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_telegram_file_path(file_id):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Missing TELEGRAM_BOT_TOKEN")
        raise ValueError("Missing TELEGRAM_BOT_TOKEN")
    response = requests.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}")
    response.raise_for_status()
    file_path = response.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def transcribe_from_telegram_voice(file_id):
    try:
        audio_url = get_telegram_file_path(file_id)
        logger.info(f"Fetching audio: {audio_url}")
        audio_response = requests.get(audio_url)
        audio_response.raise_for_status()
        audio = audio_response.content
        logger.info(f"Audio file size: {len(audio)} bytes")
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio, "audio/ogg")
        )
        text = response.text.strip()
        if not text:
            logger.warning(f"Empty transcription result: '{text}'")
            return ""
        logger.info(f"Transcribed audio: '{text}'")
        return text
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return ""

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
    logger.info(f"Summarizing data: {json.dumps(d, indent=2)}")
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
    logger.info(f"Generated summary: {summary}")
    return summary

gpt_prompt = """
You are an AI assistant extracting a construction site report from user input. Extract only explicitly mentioned fields and return them in JSON format. If no fields are clearly identified, check for specific keywords to map to fields or treat as comments for general statements.

Fields to extract (omit if not present):
- site_name: string (e.g., "Downtown Project")
- segment: string (e.g., "5", do not prefix with "Segment")
- category: string (e.g., "3", do not prefix with "Category")
- company: list of objects with "name" (e.g., [{"name": "Acme Corp"}])
- people: list of strings (e.g., ["Anna", "Tobias"])
- roles: list of objects with "name" and "role" (e.g., [{"name": "Anna", "role": "Supervisor"}, {"name": "Tobias", "role": "Crane Operator"}])
- tools: list of objects with "item" and optional "company" (e.g., [{"item": "Crane", "company": "Acme Corp"}])
- service: list of objects with "task" and optional "company" (e.g., [{"task": "Excavation", "company": "Acme Corp"}])
- activities: list of strings (e.g., ["Concrete pouring"])
- issues: list of objects with "description" (required), "caused_by" (optional), and "has_photo" (optional, default false)
  (e.g., [{"description": "Delayed delivery", "caused_by": "Supplier", "has_photo": true}])
- time: string (e.g., "morning")
- weather: string (e.g., "good")
- impression: string
- comments: string
- date: string (format dd-mm-yyyy)

Rules:
- Extract fields when explicitly mentioned with keywords like "Site:", "Company:", "Person:", "People:", "Issue:", "Issues:", "Service:", "Tool:", "Activity:", "Activities:", "Time:", "Weather:", "Segment:", "Category:", "Impression:", etc., or clear intent in natural language.
- For segment and category:
  - Extract the value only (e.g., "Category: 3" -> "category": "3", not "Category 3").
  - Recognize "Segment a" or "Category Bestand" as valid inputs.
- For issues:
  - Recognize keywords: "Issue", "Issues", "Problem", "Problems", "Delay", "Fault", "Error", or natural language (e.g., "The issue is...", "There’s a delay").
  - "Issues: none" or "Issues none" clears the issues list (return "issues": []).
  - "description" is mandatory for non-empty issues.
  - "caused_by" is optional (e.g., "caused_by Supplier").
  - "has_photo" is true only if "with photo" or "has photo" is stated.
  - Handle multiple issues as separate objects.
- For activities:
  - Recognize keywords: "Activity", "Activities", "Task", "Progress", "Construction", or action-oriented phrases (e.g., "Work was done", "Concrete pouring").
  - "Activities: none" or "Activities none" clears the activities list (return "activities": []).
  - Extract exact activity phrases from phrases like "Work was done" or "Laying foundation".
- For site_name:
  - Recognize keywords: "Site", "Location", "Project", location-like phrases following "at", "in", "on", or standalone location names (e.g., "Downtown project", "Side downtown project").
- For people:
  - Recognize "add [name]", "People [name]", "Person: [name]", "People add [name]", or "add people [name]" to add names to the people list (e.g., ["Tobias"]).
  - Do not assign a role unless explicitly mentioned in a role-related input.
- For roles:
  - Recognize "add [name] as [role]", "People [name] as [role]", "Person: [name], role: [role]", or phrases like "[name] was handling [role]" (e.g., "Tobias was handling the crane" -> {"name": "Tobias", "role": "Crane Operator"}).
  - If the input includes "I was supervising" or "I am supervising", add a role entry (e.g., {"name": "User", "role": "Supervisor"}).
  - Ensure the name in roles corresponds to a name in the people list; if not, add the name to people.
- For company:
  - Recognize "Company: [name]", "Companies: [name]", "add company [name]", "add [name] as company", or phrases like "by [company]" (e.g., "by Bill Corp and Orion Corp").
- For tools and service:
  - Recognize "Tool: [item]", "Service: [task]", "add [task/item]", or phrases like "Tools were [item]" (e.g., "Tools were crane and hammer").
  - Only include "company" if explicitly stated in the context (e.g., "Crane by Acme Corp").
  - Do not infer company names from other fields (e.g., "company" list).
- For time:
  - Recognize "Time: [value]", "Time [value]", or natural language (e.g., "morning time", "afternoon").
- For weather:
  - Recognize "Weather: [value]", "Weather [value]", or natural language (e.g., "good weather", "sunny").
- For impression:
  - Recognize "Impression: [value]", "Impression [value]".
- For comments:
  - Recognize "Comments: none" or "Comments none" to clear comments (return "comments": "").
  - Use as a fallback only for general statements that don’t match other fields or reset commands.
- Do not treat reset commands like "new", "new report", "reset", "/new" as comments; these should not be processed here.
- Return {} only for irrelevant inputs (e.g., "Hello world").
- Case-insensitive matching for keywords.

Examples:
1. Input: "Site: Downtown Project, Issue: Delayed delivery caused by Supplier with photo"
   Output: {"site_name": "Downtown Project", "issues": [{"description": "Delayed delivery", "caused_by": "Supplier", "has_photo": true}]}
2. Input: "Activities: Concrete pouring"
   Output: {"activities": ["Concrete pouring"]}
3. Input: "Issues: none"
   Output: {"issues": []}
4. Input: "Company: Acme Corp, There’s a delay"
   Output: {"company": [{"name": "Acme Corp"}], "issues": [{"description": "Delay"}]}
5. Input: "Hello world"
   Output: {}
6. Input: "All good today"
   Output: {"comments": "All good today"}
7. Input: "Service: Erecting steel frames"
   Output: {"service": [{"task": "Erecting steel frames"}]}
8. Input: "Segment: a"
   Output: {"segment": "a"}
9. Input: "Category: Bestand"
   Output: {"category": "Bestand"}
10. Input: "People add Tobias"
    Output: {"people": ["Tobias"]}
11. Input: "People Frank as Supervisor"
    Output: {"people": ["Frank"], "roles": [{"name": "Frank", "role": "Supervisor"}]}
12. Input: "add people XYZ"
    Output: {"people": ["XYZ"]}
13. Input: "add company Acme Corp"
    Output: {"company": [{"name": "Acme Corp"}]}
14. Input: "Activities: none"
    Output: {"activities": []}
15. Input: "Person: John, role: Foreman"
    Output: {"people": ["John"], "roles": [{"name": "John", "role": "Foreman"}]}
16. Input: "Company: Delta Build"
    Output: {"company": [{"name": "Delta Build"}]}
17. Input: "Weather: good"
    Output: {"weather": "good"}
18. Input: "Weather good"
    Output: {"weather": "good"}
19. Input: "Time: morning"
    Output: {"time": "morning"}
20. Input: "Time morning"
    Output: {"time": "morning"}
21. Input: "Impression: abc"
    Output: {"impression": "abc"}
22. Input: "Side downtown project"
    Output: {"site_name": "Side downtown project"}
23. Input: "Work was done on the East Wing on Zurich downtown project by Bill Corp and Orion Corp. I was supervising and Tobias was handling the crane. Tools were crane and hammer."
    Output: {
        "site_name": "East Wing, Zurich downtown project",
        "company": [
            {"name": "Bill Corp"},
            {"name": "Orion Corp"}
        ],
        "people": ["User", "Tobias"],
        "roles": [
            {"name": "User", "role": "Supervisor"},
            {"name": "Tobias", "role": "Crane Operator"}
        ],
        "tools": [
            {"item": "Crane"},
            {"item": "Hammer"}
        ],
        "activities": ["Work was done"]
    }
"""

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_site_report(text):
    # Handle reset commands explicitly
    if text.lower() in ("new", "new report", "reset", "/new"):
        logger.info(f"Recognized reset command: {text}")
        return {"reset": True}

    # Handle site_name addition
    site_match = re.match(r'^(?:site\s*[:,]?\s*|location\s*[:,]?\s*|project\s*[:,]?\s*)?(.+)$', text, re.IGNORECASE)
    if site_match and not re.search(r'\b(add|delete|remove|correct|update|none|as|role)\b', text.lower()):
        site_name = site_match.group(1).strip()
        logger.info(f"Extracted site_name: {site_name}")
        return {"site_name": site_name}

    # Handle segment addition
    segment_match = re.match(r'^(?:segment\s*[:,]?\s*)(.+)$', text, re.IGNORECASE)
    if segment_match:
        segment = segment_match.group(1).strip()
        logger.info(f"Extracted segment: {segment}")
        return {"segment": segment}

    # Handle category addition
    category_match = re.match(r'^(?:category\s*[:,]?\s*)(.+)$', text, re.IGNORECASE)
    if category_match:
        category = category_match.group(1).strip()
        logger.info(f"Extracted category: {category}")
        return {"category": category}

    # Handle impression addition
    impression_match = re.match(r'^(?:impression\s*[:,]?\s*)(.+)$', text, re.IGNORECASE)
    if impression_match:
        impression = impression_match.group(1).strip()
        logger.info(f"Extracted impression: {impression}")
        return {"impression": impression}

    # Handle people addition (name only)
    people_match = re.match(
        r'^(?:add\s+|people\s+|person\s+|add\s+people\s+|people\s+add\s+|person\s+add\s+)(\w+\s*\w*)$',
        text, re.IGNORECASE
    )
    if people_match:
        name = people_match.group(1)
        logger.info(f"Extracted person: {name}")
        return {"people": [name.strip()]}

    # Handle role addition
    role_match = re.match(
        r'^(?:add\s+|people\s+|person\s+)?(\w+\s*\w*)\s*[:,]?\s*as\s+(\w+\s*\w*)$|^(?:person|people)\s*[:,]?\s*(\w+\s*\w*)\s*,\s*role\s*[:,]?\s*(\w+\s*\w*)$',
        text, re.IGNORECASE
    )
    if role_match:
        if role_match.group(1):  # add/people/person [name] as [role]
            name, role = role_match.group(1), role_match.group(2)
        else:  # person: [name], role: [role]
            name, role = role_match.group(3), role_match.group(4)
        role = "Worker" if role.lower() == "people" else role.title()
        logger.info(f"Extracted role: {name}, role: {role}")
        return {"people": [name.strip()], "roles": [{"name": name.strip(), "role": role}]}

    # Handle supervisor self-reference
    supervisor_match = re.match(r'^(?:i\s+was\s+supervising|i\s+am\s+supervising|i\s+supervised)(?:\s+.*)?$', text, re.IGNORECASE)
    if supervisor_match:
        logger.info(f"Extracted supervisor: User")
        return {"people": ["User"], "roles": [{"name": "User", "role": "Supervisor"}]}

    # Handle company addition
    company_match = re.match(
        r'^(?:add\s+company\s+|company\s+|companies\s+|add\s+(\w+\s*\w*)\s+as\s+company\s*)[:,]?\s*(.+)$',
        text, re.IGNORECASE
    )
    if company_match:
        name = company_match.group(2) if company_match.group(2) else company_match.group(1)
        logger.info(f"Extracted company: {name}")
        return {"company": [{"name": name.strip()}]}

    # Handle service addition
    service_match = re.match(r'^(?:add\s+service\s+|service\s+|services\s+)[:,]?\s*(.+)$', text, re.IGNORECASE)
    if service_match:
        task = service_match.group(1).strip()
        logger.info(f"Extracted service: {task}")
        return {"service": [{"task": task}]}

    # Handle tool addition
    tool_match = re.match(r'^(?:add\s+tool\s+|tool\s+|tools\s+)[:,]?\s*(.+)$', text, re.IGNORECASE)
    if tool_match:
        item = tool_match.group(1).strip()
        logger.info(f"Extracted tool: {item}")
        return {"tools": [{"item": item}]}

    # Handle activity addition
    activity_match = re.match(r'^(?:add\s+activity\s+|activity\s+|activities\s+)[:,]?\s*(.+)$', text, re.IGNORECASE)
    if activity_match:
        activity = activity_match.group(1).strip()
        logger.info(f"Extracted activity: {activity}")
        return {"activities": [activity]}

    # Handle issue addition
    issue_match = re.match(r'^(?:add\s+issue\s+|issue\s+|issues\s+)[:,]?\s*(.+)$', text, re.IGNORECASE)
    if issue_match:
        description = issue_match.group(1).strip()
        logger.info(f"Extracted issue: {description}")
        return {"issues": [{"description": description}]}

    # Handle weather addition
    weather_match = re.match(r'^(?:weather\s*[:,]?\s*|good\s+weather\s*|bad\s+weather\s*|sunny\s*|cloudy\s*|rainy\s*)(.+)$', text, re.IGNORECASE)
    if weather_match:
        weather = weather_match.group(1).strip()
        logger.info(f"Extracted weather: {weather}")
        return {"weather": weather}

    # Handle time addition
    time_match = re.match(r'^(?:time\s*[:,]?\s*|morning\s*time\s*|afternoon\s*time\s*|evening\s*time\s*)(.+)$', text, re.IGNORECASE)
    if time_match:
        time_value = time_match.group(1).strip()
        logger.info(f"Extracted time: {time_value}")
        return {"time": time_value}

    # Handle "Issues none", "Activities none", "Comments none"
    clear_match = re.match(r'^(issues|activities|comments)\s*[:,]?\s*none$', text, re.IGNORECASE)
    if clear_match:
        field = clear_match.group(1).lower()
        field = "issues" if field == "issues" else "activities" if field == "activities" else "comments"
        logger.info(f"Clearing field: {field}")
        return {field: [] if field in ["issues", "activities"] else ""}

    messages = [
        {"role": "system", "content": "Extract explicitly stated fields; map ambiguous inputs to likely fields or comments based on keywords."},
        {"role": "user", "content": gpt_prompt + "\nInput text: " + text}
    ]
    try:
        logger.info(f"Processing input text: '{text}'")
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, temperature=0.2
        )
        raw_response = response.choices[0].message.content
        logger.info(f"Raw GPT response: {raw_response}")
        data = json.loads(raw_response)
        logger.info(f"Extracted report: {data}")
        # Post-process category and segment to remove prefixes
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
            issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error)\b'
            if re.search(issue_keywords, text.lower()):
                data = {"issues": [{"description": text.strip()}]}
                logger.info(f"Fallback applied: Treated as issue: {data}")
            else:
                activity_keywords = r'\b(work|activity|task|progress|construction)\b'
                location_keywords = r'\b(at|in|on)\b'
                if re.search(activity_keywords, text.lower()) and re.search(location_keywords, text.lower()):
                    parts = re.split(r'\b(at|in|on)\b', text, flags=re.IGNORECASE)
                    location = ", ".join(part.strip().title() for part in parts[2::2] if part.strip())
                    activity = parts[0].strip()
                    data = {"site_name": location, "activities": [activity]}
                    logger.info(f"Fallback applied: Treated as activity and site: {data}")
                else:
                    # Treat standalone location-like phrases as site_name
                    data = {"site_name": text.strip()}
                    logger.info(f"Fallback applied: Treated as site_name: {data}")
        logger.info(f"Final extracted report: {data}")
        return data
    except Exception as e:
        logger.error(f"GPT extract error for input '{text}': {e}")
        issue_keywords = r'\b(issue|issues|problem|problems|delay|fault|error)\b'
        if text.strip() and re.search(issue_keywords, text.lower()):
            logger.info(f"Extraction failed; fallback to issue: {text}")
            return {"issues": [{"description": text.strip()}]}
        activity_keywords = r'\b(work|activity|task|progress|construction)\b'
        location_keywords = r'\b(at|in|on)\b'
        if text.strip() and re.search(activity_keywords, text.lower()) and re.search(location_keywords, text.lower()):
            parts = re.split(r'\b(at|in|on)\b', text, flags=re.IGNORECASE)
            location = ", ".join(part.strip().title() for part in parts[2::2] if part.strip())
            activity = parts[0].strip()
            data = {"site_name": location, "activities": [activity]}
            logger.info(f"Extraction failed; fallback to activity and site: {data}")
            return data
        logger.info(f"Extraction failed; fallback to site_name: {text}")
        return {"site_name": text.strip()}

def string_similarity(a, b):
    similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    logger.info(f"String similarity between '{a}' and '{b}': {similarity}")
    return similarity

def merge_structured_data(existing, new):
    merged = existing.copy()
    for key, value in new.items():
        if key == "reset":
            continue  # Skip reset flag
        if key in ["company", "roles", "tools", "service", "activities", "issues"]:
            if value == []:  # Handle "none" cases
                merged[key] = []
                logger.info(f"Cleared {key} list")
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
                            logger.info(f"Replaced company {existing_item.get('name')} with {new_name}")
                            break
                    if not replaced and new_item not in existing_list:
                        existing_list.append(new_item)
                        logger.info(f"Added new company {new_name}")
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
                            logger.info(f"Replaced role for {existing_item.get('name')} with {new_name}")
                            break
                    if not replaced:
                        existing_list.append(new_item)
                        logger.info(f"Added new role for {new_name}")
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
                            logger.info(f"Replaced issue {existing_item.get('description')} with {new_desc}")
                            break
                    if not replaced:
                        existing_list.append(new_item)
                        logger.info(f"Added new issue {new_desc}")
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
                    logger.info(f"Added new person: {item}")
            merged[key] = existing_list
        else:
            if value == "" and key in ["comments"]:  # Handle "Comments none"
                merged[key] = ""
                logger.info(f"Cleared {key}")
            elif value:
                merged[key] = value
    logger.info(f"Merged data: {json.dumps(merged, indent=2)}")
    return merged

def delete_entry(data, field, value=None):
    logger.info(f"Deleting field: {field}, value: {value}")
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
    elif field == "people" and value:
        data[field] = [item for item in data[field] if item.lower() != value.lower()]
        # Remove corresponding roles
        data["roles"] = [role for role in data.get("roles", []) if role.get("name", "").lower() != value.lower()]
    elif field in ["activities"] and not value:
        data[field] = []
    elif field in ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]:
        data[field] = ""
    logger.info(f"Data after deletion: {json.dumps(data, indent=2)}")
    return data

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def apply_correction(orig, corr):
    prompt = (
        "Original JSON:\n" + json.dumps(orig) +
        "\n\nUser correction:\n\"" + corr + "\"\n\n"
        "Return JSON with only corrected fields. For list fields like 'company', 'roles', or 'issues', replace entries when correcting (e.g., 'Correct issue Delayed delivery to Late shipment' should replace the issue description). For 'people', update the name in the list. Do not add new entries for corrections; update existing ones. Do not modify fields not explicitly mentioned."
    )
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        partial = json.loads(response.choices[0].message.content)
        logger.info(f"Correction response: {partial}")
        merged = orig.copy()
        for key, value in partial.items():
            if key in ["company", "roles", "tools", "service", "issues"]:
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else []
                for new_item in new_items:
                    if not isinstance(new_item, dict):
                        continue
                    if key == "company" and "name" in new_item:
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("name", ""), new_item.get("name", "")) > 0.6):
                                existing_list[i] = new_item
                                logger.info(f"Applied correction: Replaced {existing_item.get('name')} with {new_item.get('name')}")
                                break
                        else:
                            logger.warning(f"Correction: No matching {key} found for {new_item.get('name')}")
                    elif key == "roles" and "name" in new_item:
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("name", ""), new_item.get("name", "")) > 0.6):
                                existing_list[i] = new_item
                                logger.info(f"Applied correction: Replaced role for {existing_item.get('name')} with {new_item.get('name')}")
                                break
                        else:
                            logger.warning(f"Correction: No matching {key} found for {new_item.get('name')}")
                    elif key in ["tools", "service"]:
                        key_field = "item" if key == "tools" else "task"
                        if key_field in new_item:
                            for i, existing_item in enumerate(existing_list):
                                if (isinstance(existing_item, dict) and
                                    string_similarity(existing_item.get(key_field, ""), new_item.get(key_field, "")) > 0.6 and
                                    string_similarity(existing_item.get("company", "") or "", new_item.get("company", "") or "") > 0.6):
                                    existing_list[i] = new_item
                                    logger.info(f"Applied correction: Replaced {existing_item.get(key_field)} with {new_item.get(key_field)}")
                                    break
                            else:
                                logger.warning(f"Correction: No matching {key} found for {new_item.get(key_field)}")
                    elif key == "issues" and "description" in new_item:
                        for i, existing_item in enumerate(existing_list):
                            if (isinstance(existing_item, dict) and
                                string_similarity(existing_item.get("description", ""), new_item.get("description", "")) > 0.6):
                                existing_list[i] = new_item
                                logger.info(f"Applied correction: Replaced issue {existing_item.get('description')} with {new_item.get('description')}")
                                break
                            elif (isinstance(existing_item, dict) and
                                  existing_item.get("description", "").lower() == new_item.get("description", "").lower()):
                                existing_list[i] = new_item
                                logger.info(f"Applied correction: Replaced issue {existing_item.get('description')} with {new_item.get('description')}")
                                break
                        else:
                            existing_list.append(new_item)
                            logger.info(f"Applied correction: Added new issue {new_item.get('description')}")
                merged[key] = existing_list
            elif key == "people":
                existing_list = merged.get(key, [])
                new_items = value if isinstance(value, list) else []
                for i, item in enumerate(existing_list):
                    if item.lower() == old_value.lower():
                        existing_list[i] = new_value
                        logger.info(f"Corrected person: {old_value} to {new_value}")
                        # Update roles if necessary
                        for j, role in enumerate(merged.get("roles", [])):
                            if role.get("name", "").lower() == old_value.lower():
                                merged["roles"][j]["name"] = new_value
                        break
                merged[key] = existing_list
            else:
                merged[key] = value
        logger.info(f"Applied correction: {corr}, Result: {json.dumps(merged, indent=2)}")
        return merged
    except Exception as e:
        logger.error(f"GPT correction error: {e}")
        return orig

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        logger.info("Webhook endpoint hit")
        data = request.get_json(force=True)
        if "message" not in data:
            logger.info("No message in webhook data")
            return "ok", 200

        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        logger.info(f"Received webhook message: chat_id={chat_id}, text='{text}'")

        if chat_id not in session_data:
            session_data[chat_id] = {
                "structured_data": blank_report(),
                "awaiting_correction": False,
                "last_interaction": time(),
                "pending_input": None,
                "awaiting_reset_confirmation": False
            }
        sess = session_data[chat_id]

        if "voice" in msg:
            text = transcribe_from_telegram_voice(msg["voice"]["file_id"])
            if not text:
                send_telegram_message(chat_id,
                    "⚠️ Couldn't understand the audio. I heard nothing.\nPlease speak clearly (e.g., say 'Work was done at ABC') and try again.")
                return "ok", 200
            logger.info(f"Transcribed voice to text: '{text}'")

        current_time = time()
        # Handle reset confirmation
        if sess.get("awaiting_reset_confirmation", False):
            logger.info(f"Processing reset confirmation: text='{text}', pending_input='{sess['pending_input']}'")
            if text.lower() in ("new", "new report"):
                sess["structured_data"] = blank_report()
                sess["awaiting_correction"] = False
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["last_interaction"] = current_time
                save_session_data(session_data)
                tpl = summarize_data(sess["structured_data"])
                send_telegram_message(chat_id,
                    "**Starting a fresh report**\n\n" + tpl +
                    "\n\nSpeak or type your first field (site name required).")
                return "ok", 200
            elif text.lower() in ("existing", "continue"):
                text = sess["pending_input"]
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["last_interaction"] = current_time
            else:
                send_telegram_message(chat_id,
                    "Please clarify: Is this for a **new report** or an **existing one**? Reply with 'new' or 'existing'.")
                return "ok", 200

        # Check for reset based on pause
        if (current_time - sess.get("last_interaction", 0) > PAUSE_THRESHOLD and
                text.lower() not in ("new", "new report", "reset", "/new", "existing", "continue")):
            sess["pending_input"] = text
            sess["awaiting_reset_confirmation"] = True
            sess["last_interaction"] = current_time
            save_session_data(session_data)
            logger.info(f"Triggered reset prompt due to pause: pending_input='{text}'")
            send_telegram_message(chat_id,
                "It’s been a while! Is this for a **new report** or an **existing one**? Reply with 'new' or 'existing'.")
            return "ok", 200

        sess["last_interaction"] = current_time

        # Handle explicit reset commands
        if text.lower() in ("new", "new report", "reset", "/new"):
            sess["structured_data"] = blank_report()
            sess["awaiting_correction"] = False
            sess["awaiting_reset_confirmation"] = False
            sess["pending_input"] = None
            save_session_data(session_data)
            logger.info("Reset report due to explicit command")
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "**Starting a fresh report**\n\n" + tpl +
                "\n\nSpeak or type your first field (site name required).")
            return "ok", 200

        # Handle clear commands (e.g., "Issues none", "Activities none")
        clear_match = re.match(r'^(issues|activities|comments)\s*[:,]?\s*none$', text, re.IGNORECASE)
        if clear_match:
            field = clear_match.group(1).lower()
            field = "issues" if field == "issues" else "activities" if field == "activities" else "comments"
            sess["structured_data"][field] = [] if field in ["issues", "activities"] else ""
            save_session_data(session_data)
            logger.info(f"Cleared field: {field}")
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Cleared {field}\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Handle deletion commands
        delete_match = re.match(r'^(delete|remove)\s+(site|segment|category|company|person|people|role|roles|tool|service|activity|activities|issue|issues|time|weather|impression|comments)(?::\s*(.+))?$', text, re.IGNORECASE)
        if delete_match:
            action, field, value = delete_match.groups()
            field = field.lower()
            if field in ["person", "people"]:
                field = "people"
            elif field in ["role", "roles"]:
                field = "roles"
            elif field in ["activity", "activities"]:
                field = "activities"
            elif field in ["issue", "issues"]:
                field = "issues"
            sess["structured_data"] = delete_entry(sess["structured_data"], field, value)
            save_session_data(session_data)
            logger.info(f"Deleted {field}" + (f": {value}" if value else ""))
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                f"Removed {field}" + (f": {value}" if value else "") + f"\n\nHere’s the updated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        # Process new data or corrections
        extracted = extract_site_report(text)
        if extracted.get("reset"):
            sess["structured_data"] = blank_report()
            sess["awaiting_correction"] = False
            sess["awaiting_reset_confirmation"] = False
            sess["pending_input"] = None
            save_session_data(session_data)
            logger.info("Reset report due to extracted reset command")
            tpl = summarize_data(sess["structured_data"])
            send_telegram_message(chat_id,
                "**Starting a fresh report**\n\n" + tpl +
                "\n\nSpeak or type your first field (site name required).")
            return "ok", 200
        # Allow updates even without site_name if there are valid fields
        if not any(k in extracted for k in ["company", "people", "roles", "tools", "service", "activities", "issues", "time", "weather", "impression", "comments", "segment", "category", "site_name"]):
            send_telegram_message(chat_id,
                "🏗️ Please provide a valid field (e.g., 'Site: Downtown Project', 'People add Tobias', 'Impression: abc').")
            return "ok", 200
        sess["structured_data"] = merge_structured_data(
            sess["structured_data"], enrich_with_date(extracted)
        )
        sess["awaiting_correction"] = True
        save_session_data(session_data)
        logger.info(f"Updated session data: awaiting_correction={sess['awaiting_correction']}")
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id,
            f"Here’s what I understood:\n\n{tpl}\n\nIs this correct? Reply with corrections or more details.")
        return "ok", 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

@app.get("/")
def health():
    logger.info("Health check endpoint hit")
    return "OK", 200

# Log startup
logger.info("Initializing Flask app for deployment")

if __name__ == "__main__":
    logger.info("Starting Flask app in local mode")
    app.run(port=int(os.getenv("PORT", 5000)), debug=True)
