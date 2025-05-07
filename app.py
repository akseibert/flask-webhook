import re

# Enhanced category pattern to handle multilingual inputs
category_pattern = re.compile(r'^(?:Category|Kategorie|CATEGORY)\s+(.+)', re.IGNORECASE)

# Service pattern for broad tasks
service_pattern = re.compile(r'^(?:Add\s+)?(?:services|service)\s+(.+upgrade|.+\s+repairs|.+\s+installation)', re.IGNORECASE)

# Activity pattern for specific actions
activity_pattern = re.compile(r'^(?:Add\s+)?(?:activities|activity)\s+(removing|installing|laying|building)\s+(.+)', re.IGNORECASE)

# Weather pattern for updates
weather_pattern = re.compile(r'^(?:Weather|weather)\s+(.+)', re.IGNORECASE)

def extract_fields(input_text):
    fields = {}
    if match := category_pattern.match(input_text):
        fields['category'] = match.group(1).strip()
    elif match := service_pattern.match(input_text):
        fields['service'] = [{'task': match.group(1).strip()}]
    elif match := activity_pattern.match(input_text):
        fields['activities'] = [f"{match.group(1)} {match.group(2)}".strip()]
    elif match := weather_pattern.match(input_text):
        fields['weather'] = match.group(1).strip()
    else:
        fields['comments'] = input_text.strip()
    return fields

# Example usage
test_inputs = [
    "Kategorie Mängelerfassung",
    "Add services laying foundation",
    "Weather sunny",
    "Category Mangel",
    "Add activities installing kitchens"
]

for input_text in test_inputs:
    result = extract_fields(input_text)
    print(f"Input: {input_text} -> Extracted: {result}")
