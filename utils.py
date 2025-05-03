import json
from collections import deque
from datetime import datetime
from config import SESSION_FILE

def load_session_data():
    """Load session data from file or return an empty dictionary if file doesn't exist."""
    try:
        with open(SESSION_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_session_data(data):
    """Save session data to file."""
    with open(SESSION_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def blank_report():
    """Return a blank construction site report structure."""
    return {
        "site_name": "",
        "segment": "",
        "category": "",
        "company": [],
        "people": [],
        "role": "",
        "service": [],
        "tools": [],
        "activities": [],
        "issues": [],
        "time": "",
        "weather": "",
        "impression": "",
        "comments": "",
        "date_added": ""
    }

def enrich_with_date(data):
    """Add current date to the data if not present."""
    if "date_added" not in data or not data["date_added"]:
        data["date_added"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data

def summarize_data(data):
    """Generate a human-readable summary of the report data."""
    summary = []
    for key, value in data.items():
        if value and key != "date_added":
            if isinstance(value, list) and value:
                summary.append(f"{key.capitalize()}: {', '.join(str(item) if isinstance(item, str) else item.get('name', item.get('item', item.get('task', item.get('description', str(item))))) for item in value)}")
            elif value.strip():
                summary.append(f"{key.capitalize()}: {value}")
    return "\n".join(summary) if summary else "No data available."
