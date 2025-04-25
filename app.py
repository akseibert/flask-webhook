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
from contextlib import contextmanager
from typing import Dict, List, Any

# --- Initialize logging ---
logging.basicConfig(
    filename="/opt/render/project/src/app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())

# --- Initialize OpenAI client ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = Flask(__name__)

# --- Session data persistence ---
SESSION_FILE = "/opt/render/project/src/session_data.json"

def load_session_data() -> Dict[str, Any]:
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Failed to load session data: {e}")
        return {}

def save_session_data(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed to save session data: {e}")

@contextmanager
def session_manager():
    data = load_session_data()
    yield data
    save_session_data(data)

# --- Field configuration ---
FIELD_CONFIG = {
    "site_name": {"scalar": True, "icon": "🏗️"},
    "segment": {"scalar": True, "icon": "🛠️"},
    "category": {"scalar": True, "icon": "📋"},
    "time": {"scalar": True, "icon": "⏰"},
    "weather": {"scalar": True, "icon": "🌦️"},
    "impression": {"scalar": True, "icon": "😊"},
    "comments": {"scalar": True, "icon": "💬"},
    "date": {"scalar": True, "icon": "📆"},
    "company": {"key": "name", "format": lambda x: x.get("name", ""), "icon": "🏢"},
    "people": {"key": "name", "format": lambda x: f"{x.get('name', '')} ({x.get('role', '')})", "icon": "👷"},
    "service": {"key": "task", "format": lambda x: f"{x.get('task', '')} ({x.get('company', '') or 'None'})", "icon": "🔧"},
    "tools": {"key": "item", "format": lambda x: f"{x.get('item', '')} ({x.get('company', '') or 'None'})", "icon": "🛠️"},
    "activities": {"key": None, "format": lambda
