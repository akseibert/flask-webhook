import re
import logging
from config import FIELD_PATTERNS

logger = logging.getLogger(__name__)

def extract_site_report(text):
    """Extract structured data from text using regex patterns."""
    result = {}
    for field, pattern in FIELD_PATTERNS.items():
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            if field in ["people", "company", "tools", "service", "activities", "issues"]:
                items = [item.strip() for item in value.split(",") if item.strip()]
                result[field] = items if field in ["people", "activities"] else [{"name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description": item} for item in items]
            else:
                result[field] = value
            logger.info({"event": "extracted_field", "field": field, "value": value})
    return result

def extract_single_command(text):
    """Extract and process single commands like add or delete."""
    text = text.strip().lower()
    if text.startswith("add "):
        parts = text[4:].split(" ", 1)
        if len(parts) == 2:
            field, value = parts
            if field in FIELD_PATTERNS:
                logger.info({"event": "add_command", "field": field, "value": value})
                return {field: value.strip()}
    elif text.startswith("delete "):
        parts = text[7:].split(" ", 1)
        if len(parts) == 1 and parts[0] in FIELD_PATTERNS:
            logger.info({"event": "delete_command", "field": parts[0]})
            return {parts[0]: {"delete": True}}
        elif len(parts) == 2 and parts[0] in FIELD_PATTERNS:
            logger.info({"event": "delete_command", "field": parts[0], "value": parts[1]})
            return {parts[0]: {"delete": parts[1].strip()}}
    return extract_site_report(text)

def merge_structured_data(existing, new):
    """Merge new structured data into existing data."""
    for key, value in new.items():
        if isinstance(value, dict) and "delete" in value:
            if value["delete"] is True:
                if key in existing:
                    if isinstance(existing[key], list):
                        existing[key] = []
                    else:
                        existing[key] = ""
            elif isinstance(existing.get(key), list):
                existing[key] = [item for item in existing[key] if str(item).strip() != value["delete"]]
        elif isinstance(existing.get(key), list) and isinstance(value, list):
            existing[key].extend(value)
            existing[key] = list({str(item): item for item in existing[key]}.values())
        elif value:
            existing[key] = value
    return existing
