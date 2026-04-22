import json
import os
import requests

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
AI_DEBUG = os.getenv("AI_DEBUG", "0") == "1"


def log(message):
    print(f"[AI.py] {message}")


def debug(message):
    if AI_DEBUG:
        print(f"[AI.py][debug] {message}")


def parse_ai_content(raw_content):
    text = (raw_content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except Exception as e:
        log(f"Failed to parse AI content as JSON array: {e}")
        debug(f"Raw AI content (first 1000 chars): {text[:1000]}")
        return []

    if isinstance(parsed, list):
        return parsed
    return []


def normalize_changes(changes):
    normalized = []
    for item in changes:
        service = item.get("service")
        key = item.get("key")
        if not service or not key:
            continue

        value = item.get("value")
        # `changes.json` may contain scalar values encoded as JSON strings.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                pass

        normalized.append({
            "service": service,
            "key": key,
            "value": value
        })
    return normalized


def call_ai(changes):
    prompt = f"""
You are a Helm values update assistant.

TASK:
Take input changes and return only safe value updates in the same schema.

RULES:
- Keep this exact output schema per row: service, key, value
- Do not add extra fields
- Do not return markdown
- Return valid JSON array only

OUTPUT FORMAT:
[
  {{
        "service": "channel-service",
        "key": "image.tag",
    "value": "M-0.0.2"
  }}
]

INPUT:
{json.dumps(changes)}
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0
        }
    }

    log(f"Calling Google Gemini API with {len(changes)} change(s)")

    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
            params={"key": GOOGLE_API_KEY},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60
        )
    except requests.RequestException as e:
        log(f"Google Gemini request failed: {e}")
        return []

    log(f"Google Gemini response status: {r.status_code}")
    if not r.ok:
        log("Google Gemini response was not OK; falling back to empty output")
        debug(f"Response text (first 1000 chars): {r.text[:1000]}")
        return []

    try:
        response_json = r.json()
    except ValueError as e:
        log(f"Google Gemini response is not valid JSON: {e}")
        debug(f"Response text (first 1000 chars): {r.text[:1000]}")
        return []

    content = (
        response_json.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "[]")
    )
    debug(f"AI raw message content (first 1000 chars): {str(content)[:1000]}")

    parsed = parse_ai_content(content)
    log(f"Parsed AI output row count: {len(parsed)}")
    return parsed


def fallback(changes):
    return normalize_changes(changes)


def main():
    with open("changes.json") as f:
        changes = json.load(f)

    normalized_changes = normalize_changes(changes)
    log(f"Loaded changes.json rows: {len(changes)}")
    log(f"Normalized change rows: {len(normalized_changes)}")
    debug(f"Normalized changes sample: {json.dumps(normalized_changes[:3], ensure_ascii=True)}")

    if GOOGLE_API_KEY:
        log("Using AI for mapping + replacement decisions")
        output = call_ai(normalized_changes)

        # Safety: if AI output is not usable, fallback to normalized input.
        if not isinstance(output, list):
            log("AI output invalid type; falling back to normalized changes")
            output = normalize_changes(changes)
    else:
        log("GOOGLE_API_KEY is missing, using fallback mode")
        output = fallback(normalized_changes)

    with open("ai_output.json", "w") as f:
        json.dump(output, f, indent=2)

    log(f"Wrote ai_output.json with {len(output)} row(s)")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()