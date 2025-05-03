import re
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

def extract_single_command(text):
    try:
        result = {}
        normalized_text = re.sub(r'[.!?]\s*$', '', text.strip())
        cleaned_text = re.sub(r'^\s*(add|delete|remove|s)\s+', '', normalized_text, flags=re.IGNORECASE).strip()

        # Handle deletion commands
        delete_match = re.match(r'^(delete|remove)\s+(\w+)(?:\s+(.+))?$', cleaned_text, re.IGNORECASE)
        if delete_match:
            _, field, value = delete_match.groups()
            field = field.lower()
            valid_fields = ["company", "people", "tools", "service", "activities", "issues", "roles"]
            single_value_fields = ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]
            if field in valid_fields:
                if value:
                    result[field] = {"delete": value.strip()}
                else:
                    result[field] = {"delete": True}  # Clear entire field
            elif field in single_value_fields:
                result[field] = {"delete": True}
            logger.info({"event": "delete_command", "field": field, "value": value})
            return result

        # Handle tool inputs
        tool_match = re.match(r'^tools?\s*[:,]?\s*(.+)$', cleaned_text, re.IGNORECASE)
        if tool_match:
            tools = [t.strip() for t in re.split(r',|and', tool_match.group(1)) if t.strip()]
            result["tools"] = [{"item": t} for t in tools]
            logger.info({"event": "extracted_field", "field": "tools", "value": tools})
            return result

        # Handle company inputs
        company_match = re.match(r'^companies?\s*[:,]?\s*(.+)$', cleaned_text, re.IGNORECASE)
        if company_match:
            companies = [c.strip() for c in re.split(r',|and', company_match.group(1)) if c.strip()]
            result["company"] = [{"name": c} for c in companies]
            logger.info({"event": "extracted_field", "field": "company", "value": companies})
            return result

        # Handle service inputs
        service_match = re.match(r'^services?\s*[:,]?\s*(.+)$', cleaned_text, re.IGNORECASE)
        if service_match:
            services = [s.strip() for s in re.split(r',|and', service_match.group(1)) if s.strip()]
            result["service"] = [{"task": s} for s in services]
            logger.info({"event": "extracted_field", "field": "service", "value": services})
            return result

        # Handle date inputs
        date_match = re.match(r'^day\s+(yesterday|today|tomorrow)$', cleaned_text, re.IGNORECASE)
        if date_match:
            day = date_match.group(1).lower()
            today = datetime.now()
            if day == "yesterday":
                result["date"] = (today - timedelta(days=1)).strftime("%d-%m-%Y")
            elif day == "today":
                result["date"] = today.strftime("%d-%m-%Y")
            elif day == "tomorrow":
                result["date"] = (today + timedelta(days=1)).strftime("%d-%m-%Y")
            logger.info({"event": "extracted_field", "field": "date", "value": result["date"]})
            return result

        # Handle people and roles
        role_match = re.match(r'^(\w+)\s+as\s+(\w+)$', cleaned_text, re.IGNORECASE)
        if role_match:
            name, role = role_match.groups()
            result["people"] = [name]
            result["roles"] = [{"name": name, "role": role}]
            logger.info({"event": "extracted_field", "field": "roles", "name": name, "role": role})
            return result

        # Handle issues
        issue_match = re.match(r'^issues?\s*[:,]?\s*(.+)$', cleaned_text, re.IGNORECASE)
        if issue_match:
            issues = [i.strip() for i in re.split(r',|and', issue_match.group(1)) if i.strip()]
            result["issues"] = [{"description": i} for i in issues]
            logger.info({"event": "extracted_field", "field": "issues", "value": issues})
            return result

        # Handle impression
        impression_match = re.match(r'^impression\s+(.+)$', cleaned_text, re.IGNORECASE)
        if impression_match:
            result["impression"] = impression_match.group(1).strip()
            logger.info({"event": "extracted_field", "field": "impression", "value": result["impression"]})
            return result

        # Fallback for unrecognized inputs (simplified, assuming GPT client is available)
        logger.warning({"event": "unrecognized_input", "text": cleaned_text})
        return result

    except Exception as e:
        logger.error({"event": "extract_single_command_error", "input": text, "error": str(e)})
        raise

def merge_structured_data(existing_data, new_data):
    merged = existing_data.copy()
    for field, value in new_data.items():
        if field == "tools" and "tool" in merged:
            merged["tools"].extend(merged.pop("tool"))  # Migrate "tool" to "tools"
        if isinstance(value, dict) and "delete" in value:
            if value["delete"] is True:
                merged[field] = [] if field in ["company", "tools", "service", "issues", "roles"] else ""
            else:
                if field in ["company", "tools", "service", "issues", "roles"]:
                    merged[field] = [item for item in merged.get(field, []) if item.get("name", item.get("item", item.get("task", item.get("description", "")))) != value["delete"]]
                elif field == "people":
                    merged[field] = [p for p in merged.get(field, []) if p != value["delete"]]
                    merged["roles"] = [r for r in merged.get("roles", []) if r["name"] != value["delete"]]
        elif field in ["company", "tools", "service", "issues", "roles", "people"]:
            merged[field] = merged.get(field, []) + value
        else:
            merged[field] = value
    return merged
