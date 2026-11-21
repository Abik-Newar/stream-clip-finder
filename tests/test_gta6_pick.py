"""Tests for the gta6 clip finder/chooser integration.

Run: PYTHONPATH=. python3 workdir/test_gta6_pick.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "vendor/gta6-clipper")

from streamclip import ai, gta6_pick as g  # noqa: E402
from streamclip.config import Options  # noqa: E402
from streamclip.score import Scorer  # noqa: E402

RESULTS = []


def ok(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


BASE = 1_700_000_000.0


def line(i, s, e, text, loud=0, chat=0, heat=0, tags=""):
    txt = text + (f" {tags}" if tags else "")
    return {"i": i, "s": s, "e": e, "plain": text, "txt": txt,
            "loud": loud, "chat": chat, "heat": heat or g.heat(txt)}


# --------------------------------------------------------- upstream parity
try:
    import nopick as upstream  # noqa: E402
    import chat as upstream_chat  # noqa: E402

    cases = [
        ([{"s": 10, "e": 11, "plain": "What a yo!", "txt": "What a yo!",
           "loud": 30, "chat": 0, "heat": 0},
          {"s": 11, "e": 12, "plain": "No way!", "txt": "No way!",
           "loud": 20, "chat": 0, "heat": 0}],
         "IShowSpeed", "IShowSpeed Plays GTA 6", "ISHOWSPEED NO WAY"),
        ([{"s": 10, "e": 11, "plain": "This is unbe-!", "txt": "This is unbe-!",
           "loud": 40, "chat": 0, "heat": 0}],
         "xQc", "xQc Reacts To GTA 6", "XQC REACTS TO GTA6"),
    ]
    ok("hook parity", all(
        upstream.hook(win, name, title) == g.hook(win, name, title) == want
        for win, name, title, want in cases))

    rows = [line(0, 0, 4, "So we are looking at the car."),
            line(1, 4.5, 9, "Here is the thing about the price."),
            line(2, 9.5, 14, "It costs ten million dollars."),
            line(3, 14.5, 19, "Nobody can afford this."),
            line(4, 19.5, 23, "Oh my god, no way!",
                 loud=25, chat=4, heat=80, tags="[LOUD +25dB] [CHAT x4 KEKW]"),
            line(5, 26, 29, "That is the whole story.")]
    u = [(round(sc, 6), s, e) for sc, s, e, _w in
         upstream.windows(rows, set())]
    m = [(round(sc, 6), s, e) for sc, s, e, _w in
         g.windows(rows, set(), kept_fn=lambda s, e: upstream.kept([], s, e))]
    ok("windows parity", u == m, f"{u} != {m}")

    ok("heat parity", all(upstream_chat.heat(x) == g.heat(x) for x in [
        "[LOUD +15dB]", "[CHAT x6]", "[CLIPPED x3 5.6k views]",
        "[REPLAYED x5]", "[LOUD +12dB] [CHAT x4] [HEAT 65]", ""]))
except Exception as exc:  # pragma: no cover - vendor path may be absent
    ok("upstream parity", False, str(exc))


# ------------------------------------------------------------ basic picks
def english_lines():
    return [
        line(0, 250, 257, "Let me show you the new car."),
        line(1, 257, 264, "It costs ten million dollars."),
        line(2, 264, 271, "Nobody can afford this."),
        line(3, 271, 278, "Wait, wait, watch this."),
        line(4, 278, 289, "Oh my god, no way! He actually bought it!",
             loud=29, chat=12, heat=92,
             tags="[LOUD +29dB] [CHAT x12] [HEAT 92]"),
        line(5, 289, 296, "That is the whole story."),
        line(6, 300, 307, "Anyway, back to the stream."),
    ]


rows = english_lines()
picks = g.pick(rows, name="Kai Cenat", title="Kai Cenat Reacts To GTA 6",
               origin=250)
ok("english pick", len(picks) == 1, str(picks))
if picks:
    p = picks[0]
    dur = p["e"] - p["s"]
    ok("window length 15-32s", 15 <= dur <= 32, f"{dur:.1f}s")
    ok("peak in last 60%",
       p["peak"] - p["s"] >= 0.4 * (p["e"] - p["s"]))
    ok("peak line has tags", "LOUD" in p["tags"] and "CHAT" in p["tags"],
       p["tags"])
    ok("quote is verbatim", p["quote"] in " ".join(
        l["plain"] for l in rows), p["quote"])
    ok("quote is the payoff", p["quote"].startswith("Oh my god"), p["quote"])
    ok("hook names the streamer", p["hook"].startswith("KAI CENAT"),
       p["hook"])
    ok("hook is short", len(p["hook"].split()) <= 6, p["hook"])
    ok("reason uses stream-relative time", "peak 28s" in p["reason"],
       p["reason"])

# No tag = no peak. A quotation alone must not create a bluff clip.
quiet = [dict(l, txt=l["plain"], loud=0, chat=0, heat=0) for l in rows]
ok("bluff rejected", g.pick(quiet) == [])

# A loud hello is not a payoff: greetings, thanks and sub reads are the
# classic "high chat, zero story" bluff on live streams.
greet = [
    line(0, 250, 257, "Welcome back to the stream everyone."),
    line(1, 257, 264, "Thanks for the subs and donations, chat."),
    line(2, 264, 271, "Here you are, guys!"),
    line(3, 271, 278, "Here you go!"),
    line(4, 278, 289, "Hello hello, what is up chat!",
         loud=25, chat=9, heat=90,
         tags="[LOUD +25dB] [CHAT x9] [HEAT 90]"),
    line(5, 289, 296, "Anyway, back to the stream."),
]
ok("greeting peak rejected", g.pick(greet) == [], str(g.pick(greet)))

mixed = [
    line(0, 250, 257, "Welcome back to the stream everyone."),
    line(1, 257, 264, "So the new update dropped this morning."),
    line(2, 264, 271, "The price is ten million dollars."),
    line(3, 271, 278, "Nobody can afford this."),
    line(4, 278, 289, "Oh my god, no way! He actually bought it!",
         loud=28, chat=11, heat=93,
         tags="[LOUD +28dB] [CHAT x11] [HEAT 93]"),
    line(5, 289, 296, "Thanks for watching, guys."),
]
mp = g.pick(mixed)
ok("mixed window skips greeting", bool(mp) and mp[0]["quote"].startswith(
    "Oh my god"), str(mp[0]["quote"] if mp else None))
ok("generic hook is empty",
   g.hook([line(9, 300, 307, "watch this!", loud=20)], "jerma985", "") == "")

# --------------------------------------------------------------- Hindi
hindi = [
    line(0, 250, 257, "Aaj kuch naya dikhata hoon."),
    line(1, 257, 264, "Ye update bahut bada hai."),
    line(2, 264, 271, "Log kya kahenge."),
    line(3, 271, 278, "Ruko, dekhna."),
    line(4, 278, 289, "भाई ये क्या हो गया, सच में?",
         chat=8, heat=70, tags="[CHAT x8] [HEAT 70]"),
    line(5, 289, 296, "Ye sach mein ho gaya."),
    line(6, 300, 307, "Chalo aage badhte hain."),
]
hp = g.pick(hindi, name="Rachitroo", title="Rachitroo Plays GTA 6")
ok("hindi pick", len(hp) == 1, str(hp))
if hp:
    ok("hindi quote preserved",
       hp[0]["quote"] == "भाई ये क्या हो गया, सच में?", hp[0]["quote"])
    ok("hindi quote not translated",
       all(ord(c) < 128 for c in "bhai ye kya") and
       any("\u0900" <= c <= "\u097f" for c in hp[0]["quote"]))

# ------------------------------------------------------- live annotations
sc = Scorer(baseline=300)
t = BASE
while t < BASE + 280:
    sc.add_chat(t, f"u{int(t // 17) % 9}", "hello")
    sc.add_audio(t, -30.0, -20.0)
    t += 5
for i in range(80):
    sc.add_chat(BASE + 275 + i * 0.2, f"v{i}", "POG")
    sc.add_audio(BASE + 275 + i * 0.2, -16.0, -1.0)

ring = [{"at": BASE + l["s"], "end": BASE + l["e"],
         "text": l["plain"], "tag": "transcript"} for l in english_lines()]
ring.append({"at": BASE + 302, "end": BASE + 306,
             "text": "hallucinated whisper line", "tag": "transcript",
             "no_speech_prob": 0.95, "avg_logprob": -1.4})

opts = Options()
opts.picker = "gta6"
lp = g.LivePicker(opts, BASE, sc)
first = lp.scan(ring, BASE + 310, "Kai Cenat", "")
ok("live scan finds pick", len(first) == 1, str(first))
if first:
    ok("live candidate has quote/hook",
       bool(first[0]["quote"]) and bool(first[0]["hook"]))
    ok("live candidate is picker-tagged", first[0]["picker"] == "gta6")
    ok("live reason is stream-relative", "peak 278s" in first[0]["reason"],
       first[0]["reason"])
    ok("live candidate window sane",
       15 <= first[0]["clip"][1] - first[0]["clip"][0] <= 32)
second = lp.scan(ring, BASE + 321, "Kai Cenat", "")
ok("live scan dedupes peak", second == [], str(second))

annotated = g.annotate(ring, sc, BASE + 310, delay=8)
ok("confidence filter drops hallucination",
   all("hallucinated" not in l["plain"] for l in annotated))
peak_line = next((l for l in annotated if "Oh my god" in l["plain"]), None)
ok("annotate adds LOUD", peak_line and "[LOUD" in peak_line["txt"],
   peak_line["txt"] if peak_line else "missing")
ok("annotate adds CHAT", peak_line and "[CHAT" in peak_line["txt"],
   peak_line["txt"] if peak_line else "missing")
ok("annotate adds HEAT", peak_line and "[HEAT" in peak_line["txt"],
   peak_line["txt"] if peak_line else "missing")

# --------------------------------------------------------- gate metadata
from streamclip.capture import SelectionGate  # noqa: E402

opts2 = Options()
opts2.picker = "legacy"
opts2.threshold = 100.0       # force the arena path
gate = SelectionGate(opts2, BASE)
cand = {"clip": (BASE + 10, BASE + 45), "score": 3.0, "reason": "gta6",
        "peak": BASE + 20, "quote": "He actually bought it!",
        "hook": "KAI CENAT BOUGHT IT", "tags": "LOUD,CHAT",
        "picker": "gta6"}
gate.observe([cand], BASE + 5)
ok("gate holds marginal picker clip",
   gate.arena is not None and gate.arena.get("quote") == cand["quote"],
   str(gate.arena))
accepted = gate.observe([], BASE + 100)
ok("gate keeps picker metadata on flush",
   len(accepted) == 1 and accepted[0].get("hook") == cand["hook"],
   str(accepted))

# ------------------------------------------------------------- AI hint
hinted = ai._build_prompt("chan", "kick", "transcript", "भाई ये क्या हो गया")
plain = ai._build_prompt("chan", "kick", "transcript")
ok("AI prompt includes picker quote", "भाई ये क्या हो गया" in hinted)
ok("AI prompt without hint stays clean", "भाई ये क्या हो गया" not in plain)

failed = 0
for name, passed, detail in RESULTS:
    failed += 0 if passed else 1
    print(f"  {'PASS' if passed else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not passed else ""))
print()
print("ALL GTA6 PICKER CHECKS PASS" if not failed
      else f"{failed} FAILURE(S) of {len(RESULTS)}")
sys.exit(1 if failed else 0)
