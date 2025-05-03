import re

# Environment variables
ENV_VARS = {
    "required": ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"],
    "optional": ["SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET", "SHAREPOINT_TENANT_ID", "SHAREPOINT_SITE_ID", "SHAREPOINT_LIST_ID"]
}

# File paths and thresholds
SESSION_FILE = "/opt/render/project/src/session_data.json"
PAUSE_THRESHOLD = 300  # 5 minutes in seconds
MAX_HISTORY = 10

# Regex patterns for field extraction
FIELD_PATTERNS = {
    "site_name": re.compile(r'^(?:add\s+)?site\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:segment|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "segment": re.compile(r'(?:add\s+)?segment\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|category|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "category": re.compile(r'(?:add\s+)?category\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|company|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "company": re.compile(r'(?:add\s+)?company\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|people|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "people": re.compile(r'(?:add\s+)?people\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|role|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "role": re.compile(r'(?:add\s+)?role\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|service|tool|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "service": re.compile(r'(?:add\s+)?service\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|tool|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "tool": re.compile(r'(?:add\s+)?tool\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|activity|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "activity": re.compile(r'(?:add\s+)?activity\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|issue|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "issue": re.compile(r'(?:add\s+)?issue\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|time|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "time": re.compile(r'(?:add\s+)?time\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|weather|impression|comments)\s*:)|$)', re.IGNORECASE),
    "weather": re.compile(r'(?:add\s+)?weather\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|impression|comments)\s*:)|$)', re.IGNORECASE),
    "impression": re.compile(r'(?:add\s+)?impression\s*[:,]?\s*(.+?)(?=(?:\s*,\s*(?:site|segment|category|company|people|role|service|tool|activity|issue|time|weather|comments)\s*:)|$)', re.IGNORECASE),
    "comments": re.compile(r'(?:add\s+)?comments\s*[:,]?\s*(.+?)$', re.IGNORECASE),
}
