"""Force one brand tag into every clip's public text.

The tag is applied deterministically (not left to the LLM) so every clip row
carries it: title, caption and hashtags. The spoken quote is never touched —
it must stay the streamer's exact words, or the clip is a bluff.
"""

import re


def brand_text(text, tag, sep=" "):
    """Append `tag` unless the text already mentions it (case-insensitive)."""
    tag = (tag or "").strip()
    if not tag:
        return text
    text = (text or "").strip()
    if not text:
        return tag
    if re.search(rf"(?<!\w){re.escape(tag)}(?!\w)", text, re.I):
        return text
    return f"{text}{sep}{tag}"


def brand_hashtags(tags, tag, limit=6):
    """Make `#tag` the first hashtag, drop duplicates, keep at most `limit`."""
    tag = (tag or "").strip().lstrip("#")
    if not tag:
        return tags
    wanted = "#" + re.sub(r"\s+", "", tag)
    parts = [p for p in str(tags or "").split() if p.startswith("#")]
    if wanted.lower() not in {p.lower() for p in parts}:
        parts.insert(0, wanted)
    out, seen = [], set()
    for p in parts:
        if p.lower() in seen:
            continue
        seen.add(p.lower())
        out.append(p)
    return " ".join(out[:limit])


def apply_brand(entry, tag):
    """Brand one clip entry in place (title, caption, hashtags only)."""
    tag = (tag or "").strip()
    if not tag:
        return entry
    entry["title"] = brand_text(entry.get("title"), tag, " | ")
    entry["caption"] = brand_text(entry.get("caption"), tag, " — ")
    entry["hashtags"] = brand_hashtags(entry.get("hashtags"), tag)
    return entry
