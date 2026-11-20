"""Optional LLM judge on candidate clips.

Works with any OpenAI-compatible chat/completions endpoint:

  • Ollama (default) — open-source models running on the GitHub Actions
    runner itself (qwen2.5:3b-instruct by default). No API key, no account,
    no cost — the workflow starts the server and caches the weights.
  • Anything else via AI_BASE_URL / AI_MODEL env vars (+ OPENAI_API_KEY).

The bar is deliberately brutal: a clip must contain a real SPOKEN payoff —
a shocking statement, wild take, confession, threat, absurd story, huge
reveal or insane reaction. Normal gameplay commentary and filler are
dropped, so the board never shows bluff clips.

Best-effort by design: on any failure rank_clip returns None and the caller
decides (strong signal spikes may still publish, weak ones are dropped).
"""

import json
import logging
import os
import re
import time

log = logging.getLogger("ai")

PROMPT = """You are a ruthless viral-clip judge for live-stream highlights.
Read the transcript excerpt and answer ONE question: does it contain a moment
where the streamer actually SAYS something that would stop a stranger from
scrolling?

LANGUAGE — READ THIS FIRST: the transcript may be in English, Hindi,
Hinglish (Roman Hindi), Devanagari, Spanish, Arabic or any other language.
A great line in ANY language counts. Never reject a clip just because it is
not English. Copy the quote EXACTLY as spoken, in its original language and
script — never translate it. Write the title and caption in the SAME
language/script as the quote (for Hindi speech, natural Hinglish is perfect).
Hashtags can stay in English for reach.

A clip passes ONLY if it has a real spoken payoff, for example:
- a shocking, wild or controversial statement / hot take
- a confession, threat, insane claim or absurd story
- a huge reveal, plot twist or genuinely hilarious line
- an extreme reaction to something that just happened
(Hindi/Hinglish examples: "bhai ye kya ho gaya", "main pagal ho jaunga",
"ye sach mein hua hai", "dekh dekh kya karta hai" — only when it lands.)

REJECT (keep=false) normal gameplay commentary, greetings, thanking subs,
reading donations, filler, silence, and jokes that need missing context.
Never invent anything. If you cannot quote the payoff line from the
transcript, it is not a clip.

Score virality 1-10 (10 = would blow up the internet). Be honest: most
stream talk is 1-3. Only 7+ deserves to be posted.

Answer ONLY with JSON, no markdown:
{"keep": true|false,
 "score": 1-10,
 "quote": "the exact craziest line, copied from the transcript",
 "title": "3-6 word clickable title (same language as the quote)",
 "caption": "1-2 catchy sentences, hook first, no spoilers (same language)",
 "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4"]}

Channel: {channel} ({platform} platform)
{hint}Transcript (stream-relative timestamps):
{transcript}
"""

HINT = """The clip finder marked this line as the spoken payoff (verbatim, do not
translate or rewrite it): {hint}
"""


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


def _clean(value, limit=300):
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip('"').strip("'")
    return text[:limit]


def _parse(content, channel=""):
    """Extract and validate the JSON verdict from an LLM reply."""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    verdict = json.loads(content[start:end + 1])

    keep = verdict.get("keep")
    keep = True if keep is None else bool(keep)
    try:
        score = float(verdict.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(10.0, score))

    tags = verdict.get("hashtags")
    if isinstance(tags, str):
        tags = tags.split()
    if isinstance(tags, list):
        tags = ["#" + str(t).strip().lstrip("#").replace(" ", "")
                for t in tags if str(t).strip().lstrip("#")]
        tags = tags[:6]

    return {
        "keep": keep,
        "score": round(score, 1),
        "quote": _clean(verdict.get("quote"), 240),
        "title": _clean(verdict.get("title"), 90) or channel,
        "caption": _clean(verdict.get("caption"), 320),
        "hashtags": " ".join(tags) if tags else "",
    }


def _build_prompt(channel, platform, transcript, hint=""):
    """Fill the template without str.format — the JSON schema in the prompt
    contains literal braces, which format() would treat as fields."""
    hint_text = HINT.replace("{hint}", str(hint or "")[:240]) if hint else ""
    text = (PROMPT.replace("{hint}", hint_text)
                  .replace("{channel}", str(channel or "unknown"))
                  .replace("{platform}", str(platform or "unknown"))
                  .replace("{transcript}", str(transcript or "")[-3500:]))
    return text


def rank_clip(transcript_text, channel="", platform="", wait_s=45, hint=None):
    """Judge a clip. Returns a validated verdict dict, or None if unavailable."""
    base, model, key = _endpoint()
    if not base or not transcript_text.strip():
        return None
    try:
        import requests
    except ImportError:
        return None

    prompt = _build_prompt(channel, platform, transcript_text, hint or "")
    deadline = time.time() + max(0, wait_s)
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2,
                      "max_tokens": 400},
                timeout=120)
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            verdict = _parse(content, channel)
            if verdict:
                return verdict
            raise ValueError("unparseable verdict")
        except Exception as exc:
            # the runner may still be installing Ollama — retry briefly
            if time.time() < deadline:
                log.info("AI not ready (%s) — retrying in 3 s", exc)
                time.sleep(3)
                continue
            log.info("AI ranking unavailable (%s) — no verdict", exc)
            return None
