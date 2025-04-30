import json
import logging
from difflib import SequenceMatcher
from typing import Dict, Any, Optional

logger = logging.getLogger("TelegramBotFix")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

def fuzzy_match(a: str, b: str, threshold: float = 0.8) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold

def delete_entry(data: Dict[str, Any], field: str, value: Optional[str] = None) -> Dict[str, Any]:
    try:
        logger.info({"event": "delete_entry", "field": field, "value": value})

        if field in ["company", "roles", "tools", "service", "issues"]:
            if value:
                def keep_item(item):
                    for key in ["name", "description", "item", "task"]:
                        field_value = item.get(key)
                        if field_value and fuzzy_match(field_value, value):
                            return False
                    return True
                data[field] = [item for item in data.get(field, []) if isinstance(item, dict) and keep_item(item)]
            else:
                data[field] = []

        elif field == "people":
            if value:
                data[field] = [p for p in data.get(field, []) if not fuzzy_match(p, value)]
                data["roles"] = [r for r in data.get("roles", []) if not fuzzy_match(r.get("name", ""), value)]
            else:
                data[field] = []
                data["roles"] = []

        elif field == "activities":
            if value:
                data[field] = [a for a in data.get(field, []) if not fuzzy_match(a, value)]
            else:
                data[field] = []

        elif field in ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]:
            if not value or fuzzy_match(value, field):
                data[field] = ""

        logger.info({"event": "data_after_deletion", "data": json.dumps(data, indent=2)})
        return data
    except Exception as e:
        logger.error({"event": "delete_entry_error", "field": field, "error": str(e)})
        raise
