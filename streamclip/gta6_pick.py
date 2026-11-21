"""GTA6 picker — the clip finding/choosing engine.

Primary picker adapted from **nocodehack/gta6-clipper v1.0.13**
(https://gitlab.com/nocodehack.io/gta6-clipper, MIT — see
`vendor/gta6-clipper/LICENSE` and `VENDOR.md`).

What is kept from upstream:
  * tag scoring (`LOUD`, `CHAT`, `HEAT`, clip commands, laughter, hype words)
  * 15–32 s windows with the peak in the last 60 %
  * setup→payoff callback discovery
  * hook selection (a whole local phrase, never an invented one)
  * the "no premise, no clip" rule

What is different because StreamClipFinder is live, not a finished VOD:
  * input is the in-memory transcript ring, not `clips/src/*.txt` files
  * `[LOUD]` / `[CHAT]` are derived from the live scorer's audio + chat
  * a window must contain a **verbatim quote**; otherwise it is a bluff and
    is dropped before the AI judge sees it
  * windows are capped by config (default 32 s, hard cap 90 s)

The upstream algorithms stay the source of truth. Change the port only when
the live data model requires it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, deque
from pathlib import Path

# ---------------------------------------------------------------- constants
# These mirror nopick.py. MAX_W is overridable for a longer story, but the
# shipped default is the upstream 32 s.
MIN_W, MAX_W, TARGET = 15, 32, 24
MIN_TALK = 14
BIN = 5.0                 # scorer bucket (seconds)
CHAT_BIN = 10.0           # gta6 chat bin (seconds)

EXC = re.compile(
    r"\b(oh my (god|gosh)|no way|bro|what the|what\?|holy|yo+|let'?s go+|bruh|"
    r"are you kidding|crazy|insane|scared|nervous)\b|!|\bwhat\b", re.I)
LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao|laugh\w*|dying|dead)\b", re.I)
BAD_START = re.compile(r"^(and|but|so|he|she|it|they|that|this|then|or)\b", re.I)
END = re.compile(r"[.?!]['\")]?\s*(\[[^\]]*\]\s*)*$")
CLAIM = re.compile(
    r"(?:i|we|you|he|she|they|it|this|that)(?:'s|'re|'m|\s+(?:is|are|am|was|"
    r"were|looks?|sounds?|seems?|feels?|(?:can'?t|cannot|don't|didn't|won't|"
    r"will|would|could|should)\s+\w+|called|told|knew|found|saw|won|lost|"
    r"died|crashed|added|removed|love|hate))\s+\S", re.I)
MOMW = dict(chase=2.0, funny=2.0, banner=2.0)

# Multilingual quote cues. Not used for the upstream score (that stays
# English-identical); they only help pick a verbatim line in Hindi/Hinglish.
MULTI_CUE = re.compile(
    r"\b(bhai|yaar|pagal|paagal|gajab|ghazab|zabardast|jabardast|kya baat|"
    r"kya kar|matlab|sach mein|sachmein|bakwas|khatam|arre|oye|dekh|dekhna|"
    r"ruko|chalo|dhoka|majboor)\b|"
    r"(पागल|गजब|ज़बरदस्त|जबरदस्त|क्या बात|क्या कर|मतलब|सच में|बकवास|खत्म|"
    r"अरे|ओए|यार|भाई|देखो|रुको|चलो|धोखा|हाहा)", re.I)

DANGLING = set(
    "a an the to and or but so yo oh what my of for with at in on from by as "
    "than because if when is are was were be been being am can could will "
    "would should must have has had do does did not very really absolutely".split())


# ------------------------------------------------------------------- scoring
def clipped(line):
    return sum(float(x) for x in re.findall(r"\[CLIPPED x(\d+(?:\.\d+)?)",
                                            line.get("txt", "")))


def moment(line):
    return sum(MOMW.get(t, 0.3) * float(v) for t, v in
               re.findall(r"\[MOMENT (\w+) (\d(?:\.\d+)?)\]",
                          line.get("txt", "")))


def lscore(line, cb=()):
    """Upstream nopick.lscore: tags + exclamations + laughter + callback."""
    p = line.get("plain", "")
    return (line.get("loud", 0) / 10 + line.get("chat", 0) / 3
            + clipped(line) * 1.5 + moment(line)
            + line.get("heat", 0) / 40
            + 0.7 * len(EXC.findall(p))
            + 0.8 * len(LAUGH.findall(p))
            + (1 if line.get("s") in cb else 0))


def heat(txt):
    """Upstream chat.heat: 0–100 noisy-or over every tag on the line."""
    p = []
    if m := re.search(r"\[LOUD \+(\d+)dB", txt):
        p.append(min(1, int(m[1]) / 15) * .6)
    if m := re.search(r"\[CHAT x([\d.]+)", txt):
        p.append(min(1, float(m[1]) / 6) * .8 + (.1 if '"' in txt else 0))
    if m := re.search(r"\[CLIPPED x(\d+) ([\d.]+)(k?) views", txt):
        v = float(m[2]) * (1000 if m[3] else 1)
        p.append(min(1, .35 + .15 * math.log10(max(v, 1) / 100)
                     + .05 * (int(m[1]) - 1)) * .9)
    if m := re.search(r"\[REPLAYED x(\d+)", txt):
        p.append(min(1, int(m[1]) / 8) * .8)
    if re.search(r"\[GAME (WASTED|BUSTED|MISSION FAILED|MISSION PASSED|"
                 r"HEIST PASSED|HEIST FAILED|VICTORY|ELIMINATED|GAME OVER|"
                 r"YOU DIED|DEFEAT|CHAMPION)", txt):
        p.append(.6)
    elif "[GAME " in txt:
        p.append(.2)
    q = 1
    for x in p:
        q *= 1 - max(0, min(x, .95))
    return round(100 * (1 - q))


def streamer(meta):
    """Upstream nopick.streamer, kept for metadata-shaped input."""
    t = meta.get("title", "")
    m = re.match(r"(.+?)\s+(reacts?|plays?)\b", t, re.I)
    return re.sub(r"\s+Live$", "",
                  (m.group(1) if m and not re.search(r"gta|vi\b", m.group(1), re.I)
                   else meta.get("channel", "")).strip())


# --------------------------------------------------------------------- hooks
def hook(win, name, title=""):
    """Upstream nopick.hook: choose a whole local phrase, never invent one."""
    name = " ".join((name or "").upper().split())

    def phrase(text):
        text = " ".join(text.strip(" .,!?:;\"()").split()).upper()
        if not text or re.search(r"[-–—…]|\.{2,}", text):
            return ""
        if GENERIC_HOOK.match(text):
            return ""               # "watch this" is not a hook
        words = text.split()
        if words[-1].strip("'\"").lower() in DANGLING:
            return ""
        result = text if not name or text.startswith(name + " ") \
            else f"{name} {text}"
        return result if len(result.split()) <= 6 else ""

    best, bs = "", -1
    peak = max(win, key=lscore) if win else None
    if peak:
        for line in win:
            if abs(line["s"] - peak["s"]) > 3:
                continue
            for c in re.split(r"(?<=[.?!,;])\s+",
                              line.get("plain", "").replace("’", "'")):
                text = c.strip(" .,!?;\"").lower()
                exclamation = re.fullmatch(
                    r"oh my (?:god|gosh)|no way|let'?s go+|holy (?:shit|crap|cow|hell)|"
                    r"wow+|bruh", text)
                claim = CLAIM.match(text)
                if not (exclamation or claim):
                    continue
                candidate = phrase(text)
                if not candidate:
                    continue
                score = bool(exclamation) * 2 + bool(c.rstrip().endswith("!")) \
                    + lscore(line)
                if score > bs:
                    best, bs = candidate, score
    if best:
        return best

    subject = re.escape(name) + r"\s+" if name else ""
    action = re.match(
        subject + r"((?:(?:never|does not|doesn't)\s+)?(?:reacts?\s+to|plays?|"
        r"watch(?:es)?|reviews?|tr(?:y|ies)))\s+(.+)", title or "", re.I)
    if action:
        verb, obj = action.groups()
        obj = re.split(r"\s+[|–—-]\s*|\s*[([]", obj)[0].strip(" .!?")
        obj = re.sub(r"\bGTA\s+(6|VI)\b", r"GTA\1", obj, flags=re.I)
        fallback = phrase(f"{verb} {obj}")
        if not fallback and re.match(r"GTA(?:6|VI)\b", obj, re.I):
            fallback = phrase(f"{verb} {obj.split()[0]}")
        if fallback:
            return fallback
    return phrase("WATCH THIS") or phrase("LOOK")


# -------------------------------------------------------------------- quotes
def _sentences(text):
    """Split a transcript segment into complete spoken sentences."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.?!।])\s+", text)
    return [p.strip() for p in parts if p.strip()]


GREETING = re.compile(
    r"^\s*(?:hi|hey|hello|yo|sup|what'?s up|good (?:morning|afternoon|evening|night)|"
    r"welcome(?:\s+back)?(?:\s+to\s+the\s+(?:stream|show))?|"
    r"here (?:you are|you go|we are|we go)(?:,?\s+(?:guys|chat|everyone|boys|folks))?|"
    r"namaste|namaskar|kya haal|kaise ho(?: sab)?|"
    r"नमस्ते|नमस्कार|हैलो|स्वागत|कैसे हो|क्या हाल)", re.I)
FILLER = re.compile(
    r"\b(?:thanks?(?:\s+you)?|shout\s?out|subscribe|follow(?:ing)?|"
    r"like the (?:stream|video)|donat(?:e|ion|ions)|subs?(?:cribe)?|bits|raid|host|"
    r"sponsor|discord|merch|link in (?:the )?description)\b|"
    r"धन्यवाद|शुक्रिया|सब्सक्राइब|फॉलो|डोनेशन", re.I)
GENERIC_HOOK = re.compile(
    r"^(?:watch this|look(?: at this)?|check this out|watch|listen)$", re.I)
SUPERLATIVE = re.compile(
    r"\b(never|always|ever|first|last|best|worst|biggest|smallest|fastest|"
    r"slowest|most|least|everyone|everybody|nobody|nothing|everything|"
    r"million|billion|trillion|hundred|thousand|times|x\d+)\b|"
    r"\d", re.I)


def _quote_ok(sentence, peak_line, line):
    """Is this sentence a real spoken payoff, not a greeting or filler?

    Live streams are full of loud hellos, thank-yous, and sub reads — they
    can top the chat/audio tags while saying nothing a stranger would clip.
    A quote needs a cue (reaction, claim, laughter, Hindi/Hinglish cue), a
    number/superlative, or a substantive statement; three words minimum.
    """
    text = " ".join((sentence or "").split())
    low = text.lower()
    words = re.findall(r"[^\W\d_]+", text, re.UNICODE)
    if len(words) < 3 or len(text) < 8:
        return False
    if GREETING.match(text):
        return False
    if FILLER.search(text) and len(words) <= 9:
        return False
    cue = bool(EXC.search(low) or CLAIM.match(low) or LAUGH.search(low)
               or MULTI_CUE.search(text) or SUPERLATIVE.search(text))
    if not cue and len(words) < 7:
        return False
    # "Watch this" / "look at this" is a promise, not the payoff.
    if re.fullmatch(r"(?:watch|look at|check) this[.!]?", low):
        return False
    return True


def _quote_value(line, sentence, peak_line=None):
    """How likely this sentence is the spoken payoff (higher = better)."""
    low = sentence.lower()
    if not _quote_ok(sentence, peak_line, line):
        return -1.0
    words = re.findall(r"[^\W\d_]+", sentence, re.UNICODE)
    score = 0.0
    if EXC.search(low):
        score += 3.0
    if CLAIM.match(low):
        score += 2.0
    if LAUGH.search(low):
        score += 2.0
    if MULTI_CUE.search(sentence):
        score += 2.0
    if SUPERLATIVE.search(sentence):
        score += 0.8
    if sentence.rstrip().endswith(("!", "?", "।")):
        score += 0.6
    if BAD_START.match(sentence):
        score -= 0.8
    # a quote that is only "oh my god" / "no way" is a reaction without a
    # premise; allow it only when a tag carries the context.
    if len(words) <= 3 and not (EXC.search(low) or MULTI_CUE.search(sentence)):
        score -= 1.0
    if peak_line is not None and line is peak_line:
        score += 0.6                  # the payoff line itself
    score += min(lscore(line), 8.0) * 0.35
    score += min(len(words), 24) / 24.0
    return score


def quote(win):
    """Return (verbatim quote, source line) from a candidate window.

    The quote is copied exactly from the transcript — no translation, no
    cleanup beyond whitespace. Hindi/Devanagari and Hinglish fall through to
    the peak line because their scripts do not match the English cues.
    """
    if not win:
        return "", None
    peak = max(win, key=lscore)
    best, best_score, best_line = "", -1.0, None
    for line in win:
        # the payoff is at/just before the peak; a later line is reaction
        if line["s"] > peak["s"] + 3.0:
            continue
        for sentence in _sentences(line.get("plain", "")):
            score = _quote_value(line, sentence, peak)
            if score > best_score:
                best, best_score, best_line = sentence, score, line
    if best and best_score > 0:
        return best, best_line
    # No English cue matched: keep the peak line verbatim if it has words
    # and is a real statement, never a greeting or a sub read.
    text = " ".join((peak.get("plain") or "").split())
    if _quote_ok(text, peak, peak):
        return text, peak
    return "", None


# ------------------------------------------------------------------- windows
def talk_seconds(win):
    """Seconds of speech in a window (Whisper segments are VAD-trimmed)."""
    return sum(max(0.0, float(l["e"]) - float(l["s"])) for l in win)


def windows(lines, cb=(), kept_fn=None, min_w=MIN_W, max_w=MAX_W,
            target=TARGET, min_talk=MIN_TALK):
    """Upstream nopick.windows: one candidate per peak line, peak near the end.

    `kept_fn(start, end)` defaults to `talk_seconds`; tests can pass the
    upstream word-timing function to prove parity.
    """
    kept_fn = kept_fn or (lambda _s, _e: talk_seconds(
        [l for l in lines if l["s"] >= _s and l["e"] <= _e]))
    out = []
    for k, p in enumerate(lines):
        sc = lscore(p, cb)
        if sc <= 0.5:
            continue
        e = p["e"]
        j = k + 1
        # take trailing reaction lines only while still ending on a sentence
        while (j < len(lines) and lines[j]["s"] - e < 1.5
               and lines[j]["e"] - e < 3
               and not END.search(lines[j - 1]["txt"].strip())):
            j += 1
        e = lines[j - 1]["e"] if not END.search(p["txt"].strip()) else p["e"]
        ks = [i for i in range(k + 1)
              if min_w <= e - lines[i]["s"] <= max_w
              and e - lines[i]["s"] <= target + 3]
        ks = [i for i in ks if not BAD_START.match(lines[i]["plain"])] or ks
        if not ks:
            continue
        i = min(ks, key=lambda i: abs(e - lines[i]["s"] - target))
        if p["s"] - lines[i]["s"] < 0.4 * (e - lines[i]["s"]):
            continue                       # peak must sit in the last 60 %
        win = [l for l in lines
               if l["s"] >= lines[i]["s"] and l["e"] <= e]
        if not win:
            continue
        if kept_fn(lines[i]["s"], e) < min_talk:
            continue                       # mostly silence: the cut is too short
        out.append((sc, lines[i]["s"], e, win))
    return out


def tags(win):
    """Upstream nopick.tags: compact tag list for a candidate window."""
    t = []
    for k, rx in (("LOUD", r"\[LOUD"), ("CHAT", r"\[CHAT"),
                  ("CLIP", r"\[CLIPPED"), ("REPL", r"\[REPLAYED"),
                  ("GAME", r"\[GAME")):
        if any(re.search(rx, l["txt"]) for l in win):
            t.append(k)
    t += sorted({m for l in win for m in re.findall(r"\[MOMENT (\w+)", l["txt"])})
    return ",".join(t) or "-"


def trim(win, head=10, tail=26):
    """Upstream nopick.trim: compact head/tail words for an AI candidate list."""
    w = " ".join(l["plain"] for l in win).split()
    return " ".join(w) if len(w) <= head + tail \
        else " ".join(w[:head]) + " ... " + " ".join(w[-tail:])


# ----------------------------------------------------------------- callbacks
def spike(line):
    return line.get("loud", 0) / 10 + line.get("chat", 0) / 3 \
        + line.get("heat", 0) / 40


def _cb_words(text):
    return [w[:7] for w in re.findall(r"[a-z']{4,}", (text or "").lower())
            if w not in CALLBACK_STOP and w.strip("'") not in CALLBACK_STOP]


CALLBACK_STOP = set("""that this with have from they them were been what when your about just like yeah okay know going gonna there here then than into over some more very really want
chat bro man dude shit fuck fucking oh oh, don't didn't can't it's i'm that's you're what's let's gotta wanna thing things right well come look need think because would could should still back
only even also much many make made good dont cant its youre thats whats lets people""".split())

CB_CLAIM = re.compile(
    r"\b(i will never|i'll never|never again|never gonna|i('m| am) never|"
    r"i guarantee|i promise|trust me|no way i|no chance|i bet|bet you|"
    r"watch (this|me|how)|easy|too easy|this time|i('m| am) (gonna|going to) win|"
    r"i can't lose|can't die|won't die|you('ll| will) see|i('ll| will) prove|"
    r"i called it|prediction|mark my words)\b", re.I)
CB_PAYOFF = re.compile(
    r"\b(told you|i said|remember (when|that)|called it|knew it|again|"
    r"here we go again|i lied|lied|wasted|busted|dead|died|die|fail|lost|lose|"
    r"never mind|nevermind)\b", re.I)
CB_STRONG = re.compile(
    r"i will never|i'll never|never again|i guarantee|trust me|no way i|"
    r"this time|watch (this|me|how)|you('ll| will) see|mark my words|"
    r"i called it|prediction", re.I)
CB_FAIL = re.compile(
    r"\b(died|dead|die|wasted|busted|arrested|failed|fail|crashed|crash|fell|"
    r"missed|lost|killed|exploded)\b", re.I)


def callbacks(lines, min_gap=300, max_gap=7200, top=10):
    """Upstream callbacks.find, in-memory: setup → later payoff pairs."""
    lines = list(lines)
    if len(lines) < 2:
        return []
    words = [set(_cb_words(l["plain"])) for l in lines]
    spikes = [spike(l) for l in lines]
    cues = [bool(CB_PAYOFF.search(l["plain"])) for l in lines]
    df = Counter(w for ws in words for w in ws)
    n = len(lines)
    idf = lambda w: math.log(n / df[w]) if df[w] else 0
    cand = {}

    def add(a, b, score, why):
        k = (a["i"], b["i"])
        if k not in cand or cand[k][0] < score:
            cand[k] = (score, why)
        elif why not in cand[k][1]:
            cand[k] = (cand[k][0] + 0.5, cand[k][1] + " + " + why)

    def local(l, w):
        return sum(w in _cb_words(x["plain"]) for x in lines
                   if abs(x["s"] - l["s"]) <= 120)

    near = [max((s for s, x in zip(spikes, lines)
                 if abs(x["s"] - l["s"]) <= 30), default=0) for l in lines]
    for ia, a in enumerate(lines):
        wa = {w for w in words[ia] if df[w] <= 8}
        claim = bool(CB_CLAIM.search(a["plain"]))
        q = a["plain"].rstrip().endswith("?")
        fail_a = CB_FAIL.search(a["plain"])
        for ib, b in enumerate(lines):
            gap = b["s"] - a["e"]
            if gap < min_gap or gap > max_gap:
                continue
            sp, cue = spikes[ib], cues[ib]
            shared = sorted(wa & words[ib], key=lambda w: -idf(w))
            sh = sum(idf(w) for w in shared[:3])
            rep = claim and (m := CB_STRONG.search(a["plain"])) \
                and re.search(re.escape(m.group(0)), b["plain"], re.I)
            if rep and sp == 0:
                add(a, b, 2.5 + sh + cue,
                    f"same claim again ('{m.group(0)}'), no tag on the payoff")
            elif claim and (shared or (cue and re.search(
                    r"told you|i said|called it|knew it|i lied",
                    b["plain"], re.I))) and sp > 0:
                add(a, b, 3 + sh + sp + cue * 1.5,
                    f"claim ('{CB_CLAIM.search(a['plain']).group(0)}') then "
                    f"{'cue ' + CB_PAYOFF.search(b['plain']).group(0) + ' ' if cue else ''}"
                    f"{shared[:3]}")
            elif q and len(shared) >= 2 and sp > 0:
                add(a, b, 1.5 + sh + sp,
                    f"question answered later, shared {shared[:3]}")
            elif fail_a and CB_FAIL.search(b["plain"]) and sp >= 2 \
                    and (cue or shared):
                add(a, b, 1.5 + sp + sh * 0.5 + cue,
                    f"same action again ({fail_a.group(0)} / "
                    f"{CB_FAIL.search(b['plain']).group(0)})")
            elif shared and sh >= 6 and sp >= 2 and df[shared[0]] <= 4:
                add(a, b, sh * 0.4 + sp, f"rare word returns {shared[:3]}")
            elif shared and df[shared[0]] <= 6 and len(shared[0]) >= 7 \
                    and near[ib] >= 2 and local(a, shared[0]) >= 2:
                add(a, b, idf(shared[0]) * 0.6 + near[ib],
                    f"topic returns at a peak {shared[:2]}")
    ranked = sorted(cand.items(), key=lambda kv: -kv[1][0])
    out, seen_a, seen_b = [], [], []
    for (ia, ib), (sc, why) in ranked:
        a, b = lines[ia], lines[ib]
        if sum(abs(a["s"] - x) < 180 for x in seen_a) >= 2 \
                or sum(abs(b["s"] - y) < 180 for y in seen_b) >= 2:
            continue
        seen_a.append(a["s"])
        seen_b.append(b["s"])
        out.append(dict(score=round(sc, 1),
                        setup=(a["s"], a["e"], a["plain"]),
                        payoff=(b["s"], b["e"], b["plain"]),
                        gap_min=round((b["s"] - a["e"]) / 60, 1),
                        why=why,
                        tags=f"payoff {'LOUD+%d ' % b['loud'] if b['loud'] else ''}"
                             f"{'CHAT x%g' % b['chat'] if b['chat'] else ''}".strip()))
        if len(out) == top:
            break
    return out


# ------------------------------------------------------------------ picking
def pick(lines, name="", title="", n=10, max_w=MAX_W, min_talk=MIN_TALK,
         min_score=0.0, require_quote=True, origin=0.0):
    """Choose the best non-overlapping windows from tagged transcript lines.

    Returns dicts with `s`, `e`, `peak`, `score`, `score_tag`, `tags`,
    `hook`, `quote`, `reason`, and `win`.
    """
    lines = [l for l in lines if l.get("plain")]
    if len(lines) < 6:
        return []
    for i, l in enumerate(lines):
        l["i"] = i
    found = callbacks(lines, top=10)
    cb = {r["payoff"][0] for r in found}
    cands = []
    for sc, s, e, win in windows(lines, cb, max_w=max_w, min_talk=min_talk):
        q, qline = quote(win)
        if require_quote and not q:
            continue                      # shouting with no words = bluff
        tag = tags(win)
        if require_quote and tag == "-":
            continue                      # no peak signal in the window
        peak_line = max(win, key=lscore)
        reason = f"gta6 {tag} peak {peak_line['s'] - origin:.0f}s"
        if any(abs(peak_line["s"] - r["payoff"][0]) < 2 for r in found):
            reason += " + callback payoff"
        cands.append({
            "s": round(s, 1), "e": round(e, 1),
            "peak": round(peak_line["s"], 1),
            "score": round(sc, 2), "score_tag": tag,
            "signals": {"loud": peak_line.get("loud", 0),
                        "chat": peak_line.get("chat", 0),
                        "heat": peak_line.get("heat", 0)},
            "tags": tag,
            "hook": hook(win, name, title),
            "quote": q,
            "reason": reason,
            "win": win,
        })
    cands.sort(key=lambda c: -c["score"])
    out = []
    for c in cands:
        if c["score"] < min_score:
            continue
        # no overlap with an already chosen window
        if any(min(c["e"], r["e"]) - max(c["s"], r["s"]) > 0 for r in out):
            continue
        out.append(c)
        if len(out) >= n:
            break
    return out


# --------------------------------------------------------- live tag annotator
def _confidence_ok(entry):
    """Drop Whisper hallucinations without killing quiet Hindi speech."""
    nsp = entry.get("no_speech_prob")
    alp = entry.get("avg_logprob")
    if nsp is not None and nsp >= 0.85:
        return False
    if alp is not None and alp <= -1.2 and (nsp is None or nsp >= 0.5):
        return False
    return True


def _audio_lift(scorer, s, e):
    """dB rise of the line's loudest second over its recent ambient level."""
    audio = [(t, mean, peak) for t, mean, peak in getattr(scorer, "audio", [])
             if s - 60 <= t <= e and mean > -80]
    if not audio:
        return 0
    peaks = [p for _t, _m, p in audio if p > -80]
    if not peaks:
        return 0
    peak = max(peaks)
    before = [m for t, m, _p in audio if t < s - 2]
    ambient = statistics.median(before) if len(before) >= 3 else \
        statistics.median([m for _t, m, _p in audio])
    jump_pool = [m for t, m, _p in audio if s - 20 <= t < s]
    jump = peak - statistics.median(jump_pool) if len(jump_pool) >= 2 else 0
    return int(max(0.0, peak - ambient, jump))


def _chat_ratio(scorer, s, e):
    """Chat rate on this line vs the previous five minutes (10 s bins)."""
    msgs = getattr(scorer, "msgs", {})
    b0, b1 = int(s // BIN), int(e // BIN)
    if not msgs:
        return 0.0
    cur = max(2.0 * msgs.get(b, 0.0) for b in range(b0, b1 + 1))
    if cur <= 0:
        return 0.0
    hist = [2.0 * msgs.get(b, 0.0) for b in range(b0 - 60, b0)]
    med = statistics.median(hist) if hist else 0.0
    ratio = cur / max(med, 1.0)
    if ratio >= 3.0 and cur >= 6:
        return round(ratio, 1)
    if med >= 20 and ratio >= 1.6 and cur >= med + 3 * math.sqrt(med):
        return round(ratio, 1)          # very busy chat: local spike
    return 0.0


def annotate(entries, scorer, now, delay=8.0):
    """Turn raw ring entries into upstream-shaped tagged lines.

    `entries` are dicts: `at`, `end`, `text`, optional `no_speech_prob` and
    `avg_logprob`. Times may be absolute epoch seconds; only differences
    matter.
    """
    out = []
    for e in entries:
        s, end = float(e.get("at", 0)), float(e.get("end", 0))
        if end <= s or end > now - delay:
            continue
        if not _confidence_ok(e):
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        plain = re.sub(r"\s*\[[A-Z]+[^\]]*\]", "", text).strip()
        if not plain:
            continue
        loud = _audio_lift(scorer, s, end)
        chat = _chat_ratio(scorer, s, end)
        txt = plain
        if loud >= 6:
            txt += f" [LOUD +{loud}dB]"
        if chat:
            txt += f" [CHAT x{chat:g}]"
        h = heat(txt)
        if h:
            txt += f" [HEAT {h}]"
        out.append({"i": len(out), "s": s, "e": end, "txt": txt,
                    "plain": plain, "loud": loud, "chat": chat, "heat": h})
    out.sort(key=lambda l: (l["s"], l["e"]))
    return out


# --------------------------------------------------------------- live picker
class LivePicker:
    """Scan the live transcript ring and emit finished gta6 candidates."""

    def __init__(self, opts, t0, scorer):
        self.opts = opts
        self.t0 = t0
        self.scorer = scorer
        self.last_scan = 0.0
        self.seen = deque(maxlen=256)
        self.scans = 0
        self.last_reason = ""

    def scan(self, ring, now, channel="", title=""):
        warmup = float(getattr(self.opts, "picker_warmup", 0.0))
        if warmup and now - self.t0 < warmup:
            return []                  # AI judge is still loading
        interval = float(getattr(self.opts, "picker_interval", 10.0))
        if now - self.last_scan < interval:
            return []
        self.last_scan = now
        self.scans += 1
        delay = float(getattr(self.opts, "picker_delay", 8.0))
        lines = annotate(list(ring), self.scorer, now, delay=delay)
        if len(lines) < 6:
            return []
        max_w = float(getattr(self.opts, "picker_max_seconds", MAX_W))
        max_w = max(MIN_W + 1, min(max_w, 90.0))
        min_score = float(getattr(self.opts, "picker_min_score", 2.5))
        top = int(getattr(self.opts, "picker_top", 2))
        picks = pick(lines, name=channel, title=title, n=top,
                     max_w=max_w, min_score=min_score, origin=self.t0)
        out = []
        for p in picks:
            if any(abs(p["peak"] - seen) < 30.0 for seen in self.seen):
                continue
            self.seen.append(p["peak"])
            self.last_reason = p["reason"]
            out.append({
                "peak": p["peak"],
                "span": (p["s"], p["e"]),
                "clip": (p["s"], p["e"]),
                "score": p["score"],
                "signals": p["signals"],
                "reason": p["reason"],
                "quote": p["quote"],
                "hook": p["hook"],
                "tags": p["tags"],
                "picker": "gta6",
            })
        return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="GTA6 clip finder/chooser")
    ap.add_argument("--transcript", help="JSON transcript ({segments:[{start,end,text}]})")
    ap.add_argument("--txt", help="tagged .txt timeline ([start-end] text [TAGS])")
    ap.add_argument("--channel", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--out", help="write picks JSON here")
    ap.add_argument("--candidates", action="store_true",
                    help="print a compact candidate list instead of picks")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        lines = [
            {"i": 0, "s": 0, "e": 4, "plain": "So we are looking at the car.",
             "txt": "So we are looking at the car.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 1, "s": 4.5, "e": 9, "plain": "Here is the thing about the price.",
             "txt": "Here is the thing about the price.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 2, "s": 9.5, "e": 14, "plain": "It costs ten million dollars.",
             "txt": "It costs ten million dollars.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 3, "s": 14.5, "e": 19, "plain": "Nobody can afford this.",
             "txt": "Nobody can afford this.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 4, "s": 19.5, "e": 23, "plain": "Oh my god, no way! He actually bought it!",
             "txt": "Oh my god, no way! He actually bought it! [LOUD +25dB] [CHAT x4 KEKW] [HEAT 80]",
             "loud": 25, "chat": 4, "heat": 80},
            {"i": 5, "s": 26, "e": 29, "plain": "That is the whole story.",
             "txt": "That is the whole story.", "loud": 0, "chat": 0, "heat": 0},
        ]
        r = pick(lines, name="Kai Cenat", title="Kai Cenat Reacts To GTA 6", n=3)
        assert len(r) == 1 and 15 <= r[0]["e"] - r[0]["s"] <= 28, r
        assert r[0]["e"] == 23.0 and r[0]["hook"].startswith("KAI CENAT"), r
        assert r[0]["quote"] == "Oh my god, no way!", r
        assert not pick([dict(l, txt=l["plain"], loud=0, chat=0, heat=0)
                         for l in lines], require_quote=True), "bluff must drop"
        hindi = [
            {"i": 0, "s": -14, "e": -10, "plain": "Welcome back to the stream.",
             "txt": "Welcome back to the stream.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 1, "s": -10, "e": -6, "plain": "We are testing the new update.",
             "txt": "We are testing the new update.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 2, "s": -6, "e": -2, "plain": "Chat is going crazy today.",
             "txt": "Chat is going crazy today.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 3, "s": -1.5, "e": 0, "plain": "Watch this.",
             "txt": "Watch this.", "loud": 0, "chat": 0, "heat": 0},
            {"i": 4, "s": 0, "e": 12, "plain": "भाई ये क्या हो गया, सच में?",
             "txt": "भाई ये क्या हो गया, सच में? [CHAT x5] [HEAT 55]",
             "loud": 0, "chat": 5, "heat": 55},
            {"i": 5, "s": 12.5, "e": 15, "plain": "That is what happened.",
             "txt": "That is what happened.", "loud": 0, "chat": 0, "heat": 0},
        ]
        hr = pick(hindi, require_quote=True)
        assert hr and hr[0]["quote"] == "भाई ये क्या हो गया, सच में?", hr
        assert heat("[LOUD +15dB] [CHAT x6] [HEAT 80]") >= 70
        print("selftest ok")
        sys.exit(0)

    if a.txt:
        raw = Path(a.txt).read_text(encoding="utf-8").splitlines()
        lines = []
        for line in raw:
            m = re.match(r"^\[(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\]\s*(.*)$", line.strip())
            if not m:
                continue
            s, e, txt = float(m[1]), float(m[2]), m[3]
            g = lambda p: next((float(x) for x in re.findall(p, txt)), 0.0)
            lines.append({"i": len(lines), "s": s, "e": e, "txt": txt,
                          "plain": re.sub(r"\s*\[[A-Z]+[^\]]*\]", "", txt).strip(),
                          "loud": g(r"\[LOUD \+(\d+)dB\]"),
                          "chat": g(r"\[CHAT x(\d+(?:\.\d+)?)"),
                          "heat": g(r"\[HEAT (\d+)\]")})
    elif a.transcript:
        doc = json.loads(Path(a.transcript).read_text(encoding="utf-8"))
        lines = []
        for seg in doc.get("segments", []):
            txt = (seg.get("text") or "").strip()
            if not txt:
                continue
            g = lambda p: next((float(x) for x in re.findall(p, txt)), 0.0)
            plain = re.sub(r"\s*\[[A-Z]+[^\]]*\]", "", txt).strip()
            lines.append({"i": len(lines), "s": float(seg["start"]),
                          "e": float(seg["end"]), "txt": txt, "plain": plain,
                          "loud": g(r"\[LOUD \+(\d+)dB\]"),
                          "chat": g(r"\[CHAT x(\d+(?:\.\d+)?)"),
                          "heat": g(r"\[HEAT (\d+)\]") or heat(txt)})
    else:
        ap.error("need --transcript or --txt (or --selftest)")

    if a.candidates:
        cb = {r["payoff"][0] for r in callbacks(lines, top=10)}
        rows = sorted(windows(lines, cb), key=lambda w: -w[0])[:a.top]
        print("# id start-end score tags | words (head ... tail)")
        for sc, s, e, win in rows:
            print(f"clip {s:.1f}-{e:.1f} {sc:.1f} {tags(win)} | {trim(win)}")
        sys.exit(0)

    picks = pick(lines, name=a.channel, title=a.title, n=a.top)
    doc = [{k: p[k] for k in ("start", "end", "score", "peak", "hook",
                              "quote", "tags", "reason")}
           for p in ({"start": q["s"], "end": q["e"], **q} for q in picks)]
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {len(doc)} picks -> {a.out}")
    else:
        print(text)
