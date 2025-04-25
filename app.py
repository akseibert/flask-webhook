# The bug is likely in the extraction and processing of issues. Here's the fix:

# First, check the `extract_site_report` function where GPT processes the text
# The prompt looks good, but the issue might be in how issues are being processed

# Let's examine the `delete_from_report` function where there might be an issue:
def delete_from_report(structured_data, target):
    """Process a deletion request and return updated structured data."""
    updated_data = structured_data.copy()
    target = target.strip().lower()
    
    # Scalar fields
    scalar_fields = ["site_name", "segment", "category", "time", "weather", "impression", "comments", "date"]
    for field in scalar_fields:
        if target == field or target.startswith(f"{field} "):
            updated_data[field] = ""
            logger.info(f"Deleted scalar field: {field}")
            return updated_data

    # List fields
    list_fields = {
        "company": "name",
        "people": "name",
        "tools": "item",
        "service": "task",
        "activities": None,  # Direct string comparison
        "issues": "description"  # This looks correct
    }
    
    for field, key in list_fields.items():
        if target.startswith(f"{field} ") or (key and target.startswith(f"{key} ")):
            value = target[len(f"{field} "):].strip() if target.startswith(f"{field} ") else target[len(f"{key} "):].strip()
            if not value:
                continue
            updated_list = updated_data.get(field, [])
            if key:
                updated_list = [item for item in updated_list if item.get(key, "").lower() != value.lower()]
            else:
                # For activities (simple strings)
                updated_list = [item for item in updated_list if item.lower() != value.lower()]
            updated_data[field] = updated_list
            logger.info(f"Deleted from {field}: {value}")
            return updated_data
    
    return updated_data

# The issue might be in the `merge_structured_data` function. Let's modify it to properly handle issues:
def merge_structured_data(existing, new):
    merged = existing.copy()
    for key, value in new.items():
        if key in ["company", "people", "tools", "service", "activities", "issues"]:
            # Append to lists, avoiding duplicates
            existing_list = merged.get(key, [])
            new_items = value if isinstance(value, list) else []
            
            # Special handling for issues to ensure proper structure
            if key == "issues":
                for item in new_items:
                    # Ensure each issue has at least a description field
                    if isinstance(item, dict) and "description" in item:
                        # Check if this issue already exists
                        exists = False
                        for existing_item in existing_list:
                            if (isinstance(existing_item, dict) and 
                                existing_item.get("description") == item.get("description")):
                                exists = True
                                # Update existing issue with new fields if available
                                if "caused_by" in item and item["caused_by"]:
                                    existing_item["caused_by"] = item["caused_by"]
                                if "has_photo" in item:
                                    existing_item["has_photo"] = item["has_photo"]
                                break
                        
                        if not exists:
                            existing_list.append(item)
            else:
                # Default handling for other list types
                for item in new_items:
                    if item not in existing_list:
                        existing_list.append(item)
                        
            merged[key] = existing_list
        else:
            # Update scalar fields if not empty
            if value:
                merged[key] = value
    return merged

# Additional check in the webhook handler to ensure issues are properly processed
# In the webhook route, make sure to log the extracted data before and after processing:

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        # ... existing code ...
        
        # When handling first extraction:
        if not sess["awaiting_correction"]:
            extracted = extract_site_report(text)
            logger.info(f"Raw extracted data: {extracted}")  # Add this logging
            
            # Ensure issues are properly formatted
            if "issues" in extracted and extracted["issues"]:
                for i, issue in enumerate(extracted["issues"]):
                    if not isinstance(issue, dict):
                        extracted["issues"][i] = {"description": str(issue)}
                    elif "description" not in issue:
                        # If there's no description, use the first available field or skip
                        if issue:
                            first_key = next(iter(issue))
                            extracted["issues"][i] = {"description": issue[first_key]}
                        else:
                            # Remove malformed issues
                            extracted["issues"].pop(i)
            
            # Continue with existing code...
