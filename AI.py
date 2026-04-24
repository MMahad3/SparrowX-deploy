import json
import os
import re
import sys

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
AI_DEBUG = os.getenv("AI_DEBUG", "0") == "1"
TARGET_KEYS = set(os.getenv("TARGET_SCHEMA_KEYS", "").splitlines())

def log(msg): print(f"[AI.py] {msg}")
def debug(msg):
    if AI_DEBUG:
        print(f"[AI.py][debug] {msg}")

def parse_ai_content(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception as e:
        log(f"Failed to parse AI response: {e}")
        return []

def normalize_key(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())

def call_ai(changes):
    # Create a compact prompt – only ask for approval per change
    prompt = f"""You are a Helm values validation assistant.

Given a list of proposed changes and the existing target keys.
For each change, decide if it should be applied.
Return a JSON array of the changes that are **approved** (same format as input).

Rules for approval:
- If the key already exists in target keys (case‑insensitive) → approve (update)
- If the key is new, but its parent path (e.g., 'resources.limits') exists → approve (add)
- If the key is completely new and parent does NOT exist → reject
- If the value type is obviously wrong (e.g., string where a number is expected) → reject
- If the change is from a known safe pattern (e.g., adding a flat key under config:) → approve

Existing target keys (flat, case‑insensitive):
{chr(10).join(sorted(TARGET_KEYS)[:200])}

Proposed changes (service, key, value):
{json.dumps(changes, indent=2)}

Return only the approved changes as a JSON array. Do not add extra fields.
Example output:
[
  {{"service": "payment", "key": "image.tag", "value": "1.2.3"}}
]
"""
    # ... (rest of your existing call_ai function – same API call logic)
    # I'm omitting the full requests code for brevity – keep yours unchanged.
    # Just replace the prompt variable with the one above.
    # ...

def main():
    with open("changes.json") as f:
        changes = json.load(f)
    if not changes:
        log("No changes to validate")
        with open("ai_output.json", "w") as f:
            json.dump([], f)
        return

    if not GOOGLE_API_KEY:
        log("No API key – approving all changes (fallback)")
        with open("ai_output.json", "w") as f:
            json.dump(changes, f)
        return

    log(f"Validating {len(changes)} changes against {len(TARGET_KEYS)} target keys")
    approved = call_ai(changes)
    log(f"Approved {len(approved)} changes")
    with open("ai_output.json", "w") as f:
        json.dump(approved, f)

if __name__ == "__main__":
    main()