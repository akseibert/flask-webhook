import re
# Assume other imports and existing code remain unchanged

def extract_single_command(text):
    # Check for delete commands first to prioritize them
    delete_match = re.match(r'^delete\s+(\w+)(?:\s+(.+))?$', text, re.IGNORECASE)
    if delete_match:
        field = delete_match.group(1).lower()
        value = delete_match.group(2).strip() if delete_match.group(2) else None
        return {"delete": {"field": field, "value": value}}

    # Handle add commands, ensuring "add" is not included in the value
    add_match = re.match(r'^add\s+(\w+)\s+(.+)$', text, re.IGNORECASE)
    if add_match:
        field = add_match.group(1).lower()
        value = add_match.group(2).strip()
        if field in ["company", "tools", "service", "activities", "issues"]:
            return {field: [{"name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description": value}]}
        elif field in ["people"]:
            return {"people": [value]}
        elif field in ["weather", "impression", "comments", "time"]:
            return {field: value}
        # Assume other field-specific logic remains unchanged

    # Existing logic for other commands (unchanged)
    # ...

def webhook():
    # Assume existing setup code (session loading, etc.) remains unchanged
    # ...

    if "delete" in result:
        field = result["delete"]["field"]
        value = result["delete"]["value"]
        if field in ["company", "tools", "service", "activities", "issues"]:
            if value:
                sess["structured_data"][field] = [item for item in sess["structured_data"].get(field, []) if item.get("name" if field == "company" else "item" if field == "tools" else "task" if field == "service" else "description") != value]
            else:
                sess["structured_data"][field] = []
        elif field in ["people"]:
            if value:
                sess["structured_data"]["people"] = [p for p in sess["structured_data"].get("people", []) if p != value]
                # Also remove from roles if tied to people
                sess["structured_data"]["roles"] = [r for r in sess["structured_data"].get("roles", []) if r.get("name") != value]
            else:
                sess["structured_data"]["people"] = []
                sess["structured_data"]["roles"] = []
        elif field in ["roles"]:
            if value:
                sess["structured_data"]["roles"] = [r for r in sess["structured_data"].get("roles", []) if r.get("name") != value]
            else:
                sess["structured_data"]["roles"] = []
        elif field in ["weather", "impression", "comments", "time"]:
            sess["structured_data"][field] = ""  # Clear non-list fields
        save_session_data(session_data)
        tpl = summarize_data(sess["structured_data"])
        send_telegram_message(chat_id, f"Deleted {field}" + (f": {value}" if value else "") + f"\n\nHere’s the updated report:\n\n{tpl}")
    else:
        # Existing logic for non-delete commands (unchanged)
        # ...

    # Assume remaining webhook code (unchanged)
    # ...
