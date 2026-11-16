"""Optional LLM second opinion on candidate clips (OpenAI-compatible API)."""

import json
import os


def rank_clip(transcript_text, api_key=None, base_url="https://api.openai.com/v1"):
    """Return {"keep": bool, "title": str} — or None if unavailable/failed."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key or not transcript_text.strip():
        return None
    try:
        import requests
    except ImportError:
        return None

    prompt = (
        "You are a stream clip judge. Decide if this transcript excerpt contains "
        "a genuinely funny/epic/highlight moment worth posting as a short clip.\n"
        'Answer ONLY with JSON: {"keep": true/false, "title": "3-6 word title"}\n\n'
        f"Transcript:\n{transcript_text[-3000:]}"
    )
    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2},
            timeout=60)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        content = content[content.find("{"): content.rfind("}") + 1]
        return json.loads(content)
    except Exception:
        return None
