"""Optional LLM second opinion on candidate clips.

Works with any OpenAI-compatible chat/completions endpoint:

  • Ollama (default) — open-source models running on the GitHub Actions
    runner itself (qwen2.5:3b-instruct by default). No API key, no account,
    no cost — the workflow starts the server and caches the weights.
  • Anything else via AI_BASE_URL / AI_MODEL env vars (+ OPENAI_API_KEY).

Best-effort by design: on any failure rank_clip returns None and the clip
is published untitled — an AI outage must never cost us a clip.
"""

import json
import logging
import os

log = logging.getLogger("ai")


def _endpoint():
    """Resolve (base_url, model, api_key) or (None, None, None) when off."""
    base = os.environ.get("AI_BASE_URL")
    model = os.environ.get("AI_MODEL")
    if base:                                   # explicit endpoint (e.g. Ollama)
        return (base.rstrip("/"), model or "qwen2.5:3b-instruct",
                os.environ.get("OPENAI_API_KEY") or "ollama")
    key = os.environ.get("OPENAI_API_KEY")
    if key:                                    # OpenAI / other cloud provider
        return "https://api.openai.com/v1", model or "gpt-4o-mini", key
    return None, None, None                    # AI ranking disabled


def rank_clip(transcript_text, channel="", platform=""):
    """Return {"keep": bool, "title": str, "caption": str, "hashtags": [...]}
    — or None if unavailable/failed."""
    base, model, key = _endpoint()
    if not base or not transcript_text.strip():
        return None
    try:
        import requests
    except ImportError:
        return None

    prompt = (
        "You are a viral clip judge for a live-stream highlight tool. "
        "Decide if this transcript excerpt contains a genuinely funny, epic, "
        "surprising or hype moment that works as a SHORT STANDALONE clip. "
        "Judge like TikTok/Reels editors: strong hook in the first seconds, "
        "a self-contained payoff, real emotion. Be strict: filler, silence "
        "and inside jokes that need context are NOT clips.\n"
        "If it is a clip, also write the social copy:\n"
        "- title: 3-6 word clickable title\n"
        "- caption: 1-2 catchy sentences with a hook, no spoilers\n"
        "- hashtags: 4-6 lowercase tags (mix of platform + game/genre + "
        "viral tags like #clips #streamer #gaming)\n"
        "Answer ONLY with JSON, no extra text:\n"
        '{"keep": true|false, "title": "...", "caption": "...", '
        '"hashtags": ["#tag1", "#tag2"]}\n\n'
        f"Channel: {channel or 'unknown'} ({platform or 'unknown'} platform)\n"
        f"Transcript:\n{transcript_text[-3000:]}"
    )
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3,
                  "max_tokens": 320},
            timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        content = content[content.find("{"): content.rfind("}") + 1]
        verdict = json.loads(content)
        tags = verdict.get("hashtags")
        if isinstance(tags, str):
            tags = tags.split()
        if isinstance(tags, list):
            tags = ["#" + t.strip().lstrip("#") for t in tags if str(t).strip()]
            verdict["hashtags"] = " ".join(tags)
        return verdict
    except Exception as exc:
        log.info("AI ranking unavailable (%s) — publishing untitled", exc)
        return None
