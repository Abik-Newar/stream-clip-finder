"""Tests for the Non-realtime (finished YouTube video) clipper.

Run: PATH="$PWD/workdir/bin:$PATH" python3 tests/test_vod.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

from streamclip import vod  # noqa: E402
from streamclip.config import Options  # noqa: E402

RESULTS = []


def ok(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


# --------------------------------------------------------------- URL parsing
cases = [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&start=4",
     "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
    ("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/live/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("  youtu.be/dQw4w9WgXcQ  ", "dQw4w9WgXcQ"),
]
ok("normalize every video link shape",
   all(vod.normalize_url(raw)[0] == vid for raw, vid in cases), str(cases))
ok("normalize returns a canonical watch URL",
   vod.normalize_url("https://youtu.be/dQw4w9WgXcQ")[1]
   == "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

for bad in (
    "",
    "https://youtube.com/@handle",
    "https://youtube.com/playlist?list=PL123",
    "https://youtube.com/watch?v=abc",
    "https://vimeo.com/12345",
    "https://youtube.com/watch",
):
    try:
        vod.normalize_url(bad)
        ok(f"reject bad link {bad!r}", False)
    except ValueError:
        ok(f"reject bad link {bad!r}", True)

ok("safe_name", vod.safe_name("Kai Cenat / GTA!") == "Kai_Cenat_GTA")
ok("mmss", vod.mmss(125) == "2:05")

# ------------------------------------------------- language → model selection
ok("lang code strips regions", vod._lang_code("en-US") == "en")
ok("lang code accepts Hindi", vod._lang_code("hi") == "hi")
ok("lang code ignores und", vod._lang_code("und") == "")
ok("auto uses base for English", vod.choose_model("auto", "en") == "base")
ok("auto uses base for Spanish", vod.choose_model("auto", "es-MX") == "base")
ok("auto uses small for Hindi", vod.choose_model("auto", "hi") == "small")
ok("auto uses small for Arabic", vod.choose_model("auto", "ar") == "small")
ok("auto falls back to base", vod.choose_model("", "") == "base")
ok("explicit model wins for Hindi", vod.choose_model("small", "hi") == "small")
ok("explicit base wins for Hindi", vod.choose_model("base", "hi") == "base")

# ------------------------------------------------------- bot-check fallback
calls = []


def fake_once(args, timeout):
    calls.append(list(args))
    if len(calls) == 1:
        raise RuntimeError("YouTube bot-check — sign in")
    return "ok"


with patch.object(vod, "_ytdlp_once", side_effect=fake_once):
    out = vod._ytdlp(["https://youtu.be/dQw4w9WgXcQ"], timeout=5)
ok("bot-check retries with mweb", out == "ok" and len(calls) == 2)
ok("mweb retry keeps the URL last",
   calls[1][-1] == "https://youtu.be/dQw4w9WgXcQ"
   and "youtube:player_client=mweb" in calls[1])
def fake_other(args, timeout):
    calls.append(list(args))
    raise RuntimeError("yt-dlp failed: nope")


calls.clear()
with patch.object(vod, "_ytdlp_once", side_effect=fake_other):
    try:
        vod._ytdlp(["https://youtu.be/x"], timeout=5)
    except RuntimeError:
        pass
ok("a non-bot error is not retried", len(calls) == 1)


def always_bot(args, timeout):
    calls.append(list(args))
    raise RuntimeError("YouTube bot-check — sign in")


calls.clear()
with patch.object(vod, "_ytdlp_once", side_effect=always_bot):
    try:
        vod._ytdlp(["https://youtu.be/x"], timeout=5)
        ok("bot-check exhaustion raises", False)
    except RuntimeError as exc:
        ok("bot-check exhaustion raises", "bot-check" in str(exc))
ok("bot-check tries every client", len(calls) == 4)
ok("bot-check ends with web_safari",
   any("youtube:player_client=web_safari" in c for c in calls))


# ----------------------------------------------------------- tagged lines
segments = [
    {"start": 0, "end": 4, "text": "So we are looking at the car.",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 4.5, "end": 9, "text": "Here is the thing about the price.",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 9.5, "end": 14, "text": "It costs ten million dollars.",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 14.5, "end": 19, "text": "Nobody can afford this.",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 19.5, "end": 23,
     "text": "Oh my god, no way! He actually bought it!",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 26, "end": 29, "text": "That is the whole story.",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 30, "end": 33, "text": "hallucinated silence",
     "no_speech_prob": 0.95, "avg_logprob": -2.0},
]
db = [-40.0] * 20 + [-16.0] * 6 + [-40.0] * 10
lines = vod.build_lines(segments, db)
ok("confidence filter drops hallucination",
   all("hallucinated" not in l["plain"] for l in lines))
ok("every line is tagged for the picker",
   all(l["txt"] != l["plain"] for l in lines))
ok("loud line gets LOUD", any("[LOUD +" in l["txt"] for l in lines))
ok("plain talk gets a MOMENT tag",
   any("[MOMENT talk" in l["txt"] for l in lines))
ok("heat is bounded", all(0 <= l["heat"] <= 100 for l in lines))

hindi = vod.build_lines([
    {"start": 0, "end": 4, "text": "आज हम एक नई गाड़ी देखने जा रहे हैं।",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 4.5, "end": 8, "text": "इसकी कीमत बहुत ज्यादा है।",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 8.5, "end": 12, "text": "भाई ये क्या हो गया, सच में?",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 12.5, "end": 16, "text": "देखो ये कितना बड़ा धमाका हुआ!",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 16.5, "end": 20, "text": "मुझे नहीं पता था कि ऐसा होगा।",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
    {"start": 20.5, "end": 24, "text": "चलो अब आगे बढ़ते हैं।",
     "no_speech_prob": 0.1, "avg_logprob": -0.2},
], [-35.0] * 30)
ok("Hindi lines keep their script",
   any("भाई" in l["plain"] for l in hindi))


# -------------------------------------------------------------- picking
picks = vod.pick_moments(lines, title="Kai Cenat Reacts", channel="Kai Cenat",
                         max_clips=3, min_gap=0)
ok("picker finds a moment", len(picks) >= 1)
ok("pick has a verbatim quote",
   picks and picks[0]["quote"] and picks[0]["quote"] in
   " ".join(l["plain"] for l in picks[0]["win"]))
ok("pick is 15-32 s",
   picks and 15 <= picks[0]["e"] - picks[0]["s"] <= 32)
ok("pick reason is stream-relative",
   picks and "peak 20s" in picks[0]["reason"])
ok("pick reason says talk, not MOMENT",
   picks and "MOMENT" not in picks[0]["reason"])

hp = vod.pick_moments(hindi, title="GTA 6 Hindi", channel="GamerBhai",
                      max_clips=2, min_gap=0)
ok("Hindi pick keeps the quote", hp and any(
    "भाई" in p["quote"] or "देखो" in p["quote"] for p in hp), str(hp))

gap = vod.pick_moments(lines, max_clips=3, min_gap=300)
ok("min_gap keeps only one of two close peaks", len(gap) <= 1)


# -------------------------------------------------------------- judging
base_pick = picks[0]
opts = Options()
opts.ai_min_score = 4.5
opts.vod_override_min = 7.0

with patch.object(vod, "rank_clip", return_value=None):
    keep, verdict = vod.judge(base_pick, {"channel": "Kai Cenat"}, opts)
ok("AI unavailable keeps the quote-gated pick", keep and verdict is None)

with patch.object(vod, "rank_clip",
                  return_value={"keep": False, "score": 1.0}):
    keep, verdict = vod.judge(base_pick, {"channel": "Kai Cenat"}, opts)
ok("AI reject drops the pick", not keep and verdict is not None)

with patch.object(vod, "rank_clip",
                  return_value={"keep": False, "score": 8.0}):
    keep, verdict = vod.judge(base_pick, {"channel": "Kai Cenat"}, opts)
ok("AI override needs a strong quote",
   keep and verdict is None)

with patch.object(vod, "rank_clip",
                  return_value={"keep": True, "score": 7.0, "title": "T",
                                "caption": "C", "hashtags": "#x"}):
    keep, verdict = vod.judge(base_pick, {"channel": "Kai Cenat"}, opts)
ok("AI keep passes", keep and verdict and verdict["score"] == 7.0)


# ---------------------------------------------------------------- copy
meta = {"title": "Kai Cenat Reacts To GTA 6", "channel": "Kai Cenat"}
title, caption, tags = vod.copy_for(base_pick, None, meta)
ok("fallback copy uses the quote", base_pick["quote"] in caption)
ok("fallback copy has hashtags", tags.startswith("#"))
ok("fallback title is bounded", 0 < len(title) <= 90)

title, caption, tags = vod.copy_for(
    base_pick, {"title": "T", "caption": "This shocking must-watch moment!",
                "hashtags": "#x"}, meta)
ok("hype caption is replaced by the verbatim quote",
   "shocking" not in caption.lower() and base_pick["quote"] in caption)

from streamclip.brand import apply_brand  # noqa: E402

title, caption, tags = vod.copy_for(base_pick, None, meta)
branded = apply_brand({"title": title, "caption": caption,
                       "hashtags": tags}, opts.brand_tag)
ok("GTA6 is in the branded VOD title", "GTA6" in branded["title"])
ok("GTA6 is in the branded VOD caption", "GTA6" in branded["caption"])
ok("GTA6 is in the branded VOD hashtags", "#GTA6" in branded["hashtags"])
ok("the branded caption keeps the verbatim quote",
   base_pick["quote"] in branded["caption"])


# --------------------------------------------------------------- progress
with patch.object(vod.dashboard, "write_progress") as wp:
    st = vod.JobState(dry_run=True)
    st.write("done", "done", "dry run: 1 clip(s) ready", 100, min_gap=0)
    slot, doc = wp.call_args[0][0], wp.call_args[0][1]
ok("progress uses the non-realtime slot", slot == "non-realtime")
ok("progress carries the dry-run flag", doc.get("dry_run") is True)
ok("progress timestamps are ISO", "T" in str(doc.get("updated")))


# ---------------------------------------------------------------- cutting
ff = shutil.which("ffmpeg")
if not ff and Path("workdir/bin/ffmpeg").exists():
    ff = str(Path("workdir/bin/ffmpeg").resolve())
if ff:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.mp4"
        subprocess.run(
            [ff, "-nostdin", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:d=12",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             "-shortest", str(src)], check=True, timeout=120)
        with patch.object(vod, "_ffmpeg") as fake:
            def run(args, timeout=0):
                subprocess.run([ff, "-nostdin", "-y", "-loglevel", "error",
                                *args], check=True, timeout=timeout)
            fake.side_effect = run
            out, dur = vod.cut_clip(src, 2, 8, Path(td) / "clip.mp4", 60)
        ok("cut writes a playable clip", out.exists() and out.stat().st_size > 0)
        ok("cut duration is exact", abs(dur - 6.0) < 0.1)
else:
    ok("cut test skipped (no ffmpeg)", True)


failed = 0
for name, passed, detail in RESULTS:
    failed += 0 if passed else 1
    print(f"  {'PASS' if passed else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not passed else ""))
print()
print("ALL NON-REALTIME CHECKS PASS" if not failed
      else f"{failed} FAILURE(S) of {len(RESULTS)}")
sys.exit(1 if failed else 0)
