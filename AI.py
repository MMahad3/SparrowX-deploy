import json
import os
import requests

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


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
    except Exception:
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

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0
        }
    )

    response_json = r.json()
    content = response_json.get("choices", [{}])[0].get("message", {}).get("content", "[]")
    return parse_ai_content(content)


def fallback(changes):
    return normalize_changes(changes)


def main():
    with open("changes.json") as f:
        changes = json.load(f)

    normalized_changes = normalize_changes(changes)

    if OPENAI_API_KEY:
        print("Using AI for mapping + replacement decisions")
        output = call_ai(normalized_changes)

        # Safety: if AI output is not usable, fallback to normalized input.
        if not isinstance(output, list):
            output = normalize_changes(changes)
    else:
        print("Fallback mode")
        output = fallback(normalized_changes)

    with open("ai_output.json", "w") as f:
        json.dump(output, f, indent=2)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()