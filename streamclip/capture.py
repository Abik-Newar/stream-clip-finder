"""Capture agent (supervisor) — manual mode.

The agent is pinned to a *slot*. It watches exactly the channel the user
pasted on the dashboard (a workflow input or an override file) and exits
when that live ends; it never picks a channel by itself. Other agents are
avoided via platform-blind creator claims, so the same person can never be
captured twice (Twitch/Kick simulcasts included).

Flow:
    dashboard link ──► claim exactly that channel ──► capture until it ends
"""

import argparse
import base64
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import audio, clipper, dashboard, livecheck, score as scoring, transcribe, upload
from .ai import rank_clip
from .brand import apply_brand
from .chat import make_chat
from .config import Options, load_config

log = logging.getLogger("capture")

RUN_TITLE = "clip-capture|{platform}|{channel}|{slot}"


# --------------------------------------------------------------- run claiming

def _parse_title(title):
    """'clip-capture|twitch|jynxzi|twitch-1' → ('twitch', 'jynxzi', 'twitch-1')."""
    if not title or not title.startswith("clip-capture|"):
        return None
    parts = title.split("|")
    if len(parts) < 3:
        return None
    platform, channel = parts[1], parts[2]
    slot = parts[3] if len(parts) > 3 else "manual"
    return platform, channel, slot


AGENT_STATE_DIR = "dashboard-data/agent-state"


def _identity(platform, channel):
    """One creator = one claim, regardless of platform.

    Big streamers simulcast the exact same show on Twitch and Kick (xQc,
    summit1g, …). Keying claims by "platform/channel" let one agent capture
    the Twitch feed while another captured the identical Kick feed — every
    moment clipped twice. Handles are therefore compared platform-blind."""
    return str(channel or "").strip().lstrip("@").lower()


def _active_runs_and_slots(my_run_id):
    """Capturing runs → (claims from titles, {slot: set(run_ids)}).

    Only `in_progress` runs count. A `queued` run has not claimed or
    captured anything yet — and GitHub can leave a re-queued job pending
    for a long time, so trusting it made one stuck run block a creator
    forever (seen live: a queued kick-1 held xQc while nobody captured).
    The list is cached for 45 s: every agent polls it, and the shared
    installation token has a 5 000/h budget."""
    global _RUNS_CACHE
    if not os.environ.get("GITHUB_REPOSITORY"):
        return set(), {}
    now = time.time()
    if _RUNS_CACHE["data"] is not None and now - _RUNS_CACHE["at"] < 45:
        data = _RUNS_CACHE["data"]
    else:
        try:
            r = subprocess.run(
                ["gh", "run", "list", "--workflow", "capture.yml",
                 "--limit", "30",
                 "--json", "databaseId,status,displayTitle"],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return set(), {}
            data = json.loads(r.stdout or "[]")
        except Exception:
            return set(), {}
        _RUNS_CACHE = {"at": now, "data": data}
    claimed, active_slots = set(), {}
    for run in data:
        if str(run.get("databaseId")) == str(my_run_id):
            continue
        if (run.get("status") or "") != "in_progress":
            continue
        parsed = _parse_title(run.get("displayTitle") or "")
        if not parsed:
            continue
        platform, channel, slot = parsed
        if channel and channel != "auto":
            claimed.add(_identity(platform, channel))
        active_slots.setdefault(slot, set()).add(str(run.get("databaseId")))
    return claimed, active_slots


_RUNS_CACHE = {"at": 0.0, "data": None}


_CLAIM_FRESH_S = 300           # fresh claims trusted for 5 min (run-list lag)
_STATE_LIST = {"at": 0.0, "names": []}
_STATE_DOCS: dict[str, dict] = {}
KNOWN_SLOTS = [f"{p}-{i}" for p in ("twitch", "kick", "youtube")
               for i in (1, 2, 3)] + ["manual"]


def _agent_state_names():
    """File names in agent-state/, cached 2 min (one API call per agent)."""
    now = time.time()
    if _STATE_LIST["names"] and now - _STATE_LIST["at"] < 120:
        return _STATE_LIST["names"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{AGENT_STATE_DIR}",
             "-H", "Accept: application/vnd.github+json"],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            entries = json.loads(r.stdout or "[]")
            names = [e.get("name", "") for e in entries
                     if e.get("name", "").endswith(".json")]
            if names:
                _STATE_LIST.update(at=now, names=names)
                return names
    except Exception:
        pass
    # API unavailable or rate-limited: fall back to the built-in slots so
    # duplicate protection keeps working from the cached claim docs.
    return _STATE_LIST["names"] or [f"{s}.json" for s in KNOWN_SLOTS]


def _read_agent_state():
    """All agent-state claim files → dicts with slot/platform/channel/since.

    Reads use ETag conditional requests: unchanged files answer 304, which
    GitHub does not charge against the API quota. That keeps nine agents
    polling claims 24/7 inside the 5 000/h installation budget."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return []
    token = dashboard._token()
    if not token:
        return []
    docs = []
    for name in _agent_state_names():
        path = f"{AGENT_STATE_DIR}/{name}"
        status, meta = dashboard.get_file(path, token)
        if status not in (200, 304) or not isinstance(meta, dict):
            continue
        content = meta.get("content")
        if content is None:
            continue
        try:
            doc = json.loads(base64.b64decode(content).decode())
        except Exception:
            continue
        if isinstance(doc, dict):
            docs.append({
                "slot": name[:-5],
                "platform": doc.get("platform"),
                "channel": str(doc.get("channel") or "").strip()
                           .lstrip("@").lower(),
                "since": str(doc.get("since") or ""),
                "run_id": str(doc.get("run_id") or ""),
            })
    return docs


def _verified_claims(active_slots, my_slot):
    """Channels held by other agents that still count as claimed.

    A claim counts when its run is active. The run-list API lags a few
    seconds behind a just-started agent, so a FRESH claim is also trusted
    even when its run is not listed yet — that closes the 3-second race
    that let two Kick agents claim the same channel. Older claims from dead
    runs expire, so crashed agents don't block channels forever."""
    out = {}
    now = datetime.now(timezone.utc)
    for doc in _read_agent_state():
        slot = doc["slot"]
        if slot == my_slot:
            continue
        matched = False
        if active_slots:
            if slot in active_slots and (
                    not doc["run_id"]
                    or doc["run_id"] in active_slots[slot]):
                matched = True
        if not matched:
            try:
                t = datetime.fromisoformat(doc["since"].replace("Z", "+00:00"))
                fresh = (now - t).total_seconds() <= _CLAIM_FRESH_S
            except (ValueError, TypeError):
                fresh = False
            if not fresh:
                continue
        if doc["channel"]:
            out[_identity(doc["platform"], doc["channel"])] = {
                "slot": slot, "since": doc["since"], "run_id": doc["run_id"]}
    return out


def _agent_state_claims(active_slots, my_slot):
    """Channels other live agents published to dashboard-data/agent-state/."""
    return set(_verified_claims(active_slots, my_slot).keys())


def _claims_snapshot(my_run_id, my_slot):
    """(claimed channel keys, claim details) — titles + agent-state files."""
    claimed, active_slots = _active_runs_and_slots(my_run_id)
    details = _verified_claims(active_slots, my_slot)
    claimed |= set(details.keys())
    return claimed, details


def _claim_lost(my_run_id, my_slot, platform, channel):
    """The rival slot that verifiably claimed this channel first, or None.

    Used twice, as two independent safety layers so that two agents can
    NEVER capture the same channel:
      • watchdog  — called every ~60 s mid-capture; aborts our capture
        early when a rival holds the channel with an earlier claim
        (the exact race that produced duplicate clips before).
      • shadow-drop — called before a clip is uploaded; drops the clip
        instead of publishing a duplicate of the rival's work.
    Only claims by OTHER slots with an EARLIER `since` count — if we
    claimed the channel first, it is ours."""
    key = _identity(platform, channel)
    _, details = _claims_snapshot(my_run_id, my_slot)
    rival = details.get(key)
    if not rival or not rival.get("since"):
        return None
    mine = None
    for doc in _read_agent_state():
        if doc["slot"] == my_slot:
            mine = doc["since"]
            break
    if mine and (rival["since"], rival["slot"]) >= (mine, my_slot):
        return None                       # we were first (or tie-break won)
    return rival["slot"]


def rename_run(platform, channel, slot):
    """Tell other agents we moved: the run title is our claim."""
    if not os.environ.get("GITHUB_REPOSITORY"):
        return
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return
    title = RUN_TITLE.format(platform=platform, channel=channel, slot=slot)
    subprocess.run(
        ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}",
         "--method", "PATCH", "--field", f"name={title}"],
        capture_output=True, text=True, timeout=60)


# ------------------------------------------------------------------ processes

def _stop(proc):
    """Terminate a child reliably: SIGTERM → grace wait → SIGKILL."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def spawn_reader(url):
    """streamlink keeps the Twitch/Kick playlist fresh and skips ad breaks."""
    return subprocess.Popen(
        ["streamlink", url, "best", "--twitch-disable-ads", "--stdout"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def start_segmenter(seg_dir, seg_time, reader=None, source=None, split=None):
    """ffmpeg splits the continuous stream into seg_00001.ts, seg_00002.ts, …
    Segment i covers stream-relative [(i-1)*seg_time, i*seg_time)."""
    if split:
        vurl, aurl = split
        inputs = ["-i", vurl, "-i", aurl]
        mapping = ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        inputs = ["-i", "pipe:0" if reader else source]
        mapping = ["-map", "0"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           *inputs, *mapping, "-c", "copy",
           "-f", "segment", "-segment_time", str(seg_time),
           "-segment_start_number", "1",
           "-reset_timestamps", "1", "-segment_format", "mpegts",
           str(seg_dir / "seg_%05d.ts")]
    stdin = reader.stdout if reader else subprocess.DEVNULL
    return subprocess.Popen(cmd, stdin=stdin,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def transcribe_worker(q, t0, seg_time, scorer, ring, model_name, progress=None):
    tr = None
    while True:
        item = q.get()
        if item is None:
            return
        idx, path = item
        try:
            if tr is None:
                tr = transcribe.Transcriber(model_name)
            base = t0 + (idx - 1) * seg_time
            for seg in tr.transcribe(path):
                text = (seg.get("text") or "").strip()
                if not text:
                    continue
                at = base + seg["start"]
                result = transcribe.analyze(text)
                scorer.add_keywords(at, result["score"],
                                    command=result["command"])
                tag = "★ CLIP COMMAND" if result["command"] else "transcript"
                ring.append((at, tag, text))
                log.info("%s: %s", tag, text[:120])
        except Exception as exc:  # keep the worker alive
            log.warning("transcription failed: %s", exc)
        finally:
            if progress is not None:
                progress[0] = max(progress[0], idx)
            q.task_done()


# ------------------------------------------------------------ channel picking

def build_pool(channels, platform, channel):
    """Ordered pool for THIS agent's platform only (config first, then the
    channel it was dispatched with)."""
    pool, seen = [], set()
    for c in channels or []:
        if c.get("enabled") is False:
            continue
        p, n = c.get("platform"), c.get("name")
        if not p or not n or p != platform:
            continue
        key = (p, n.lstrip("@").lower())
        if key in seen:
            continue
        seen.add(key)
        pool.append((p, n))
    key = (platform, (channel or "").lstrip("@").lower())
    if channel and key not in seen:
        pool.insert(0, (platform, channel))
    return pool


def choose_target(pool, ended_at, opts, my_run_id, retry_after=180,
                  my_slot=None):
    """Pick the next live, unclaimed channel from the pool.

    Two agents probing at the same moment can both see "no claim" and pick
    the same channel (seen live: two Kick agents cut the same moment 2 s
    apart). So a pick is committed as a claim immediately, then verified:
    if another agent claimed the same channel earlier, we back off and try
    the next candidate."""
    claimed, _ = _claims_snapshot(my_run_id, my_slot)
    now = time.time()
    for p, n in pool:
        key = _identity(p, n)
        if now - ended_at.get(key, 0) < retry_after:
            continue                       # it just ended — let it rest
        if key in claimed:
            continue
        try:
            live, spec = livecheck.probe(p, n)
        except Exception as exc:
            log.debug("probe %s/%s failed: %s", p, n, exc)
            continue
        if not live:
            log.debug("%s/%s is offline", p, n)
            continue
        # commit our claim, then check we were first to the channel
        my_since = dashboard.publish_agent_state(my_slot or "slot", p, n,
                                                 my_run_id)
        if not my_since and my_slot:
            # Never capture without a committed claim: if the API is down,
            # two agents could otherwise pick the same creator and cut
            # duplicate clips. Waiting is always safer than duplicating.
            log.warning("%s: claim write failed — not taking %s/%s",
                        my_slot, p, n)
            continue
        if my_since:
            time.sleep(3)                  # let a racing agent commit too
            claimed, details = _claims_snapshot(my_run_id, my_slot)
            rival = details.get(key)
            if rival and rival["since"] and (
                    (rival["since"], rival["slot"])
                    < (my_since, my_slot or "slot")):
                log.info("%s/%s: %s claimed it first — next candidate",
                         p, n, rival["slot"])
                claimed.add(key)
                continue
        return (p, n, spec)
    return None


# ------------------------------------------------------------- quality gates

ARENA_MARGIN = 1.20          # a new candidate must clearly beat the held one


class SelectionGate:
    """Quality gates between raw peaks and real clips (v4).

    Research-backed rules so storage only fills with the best moments:
      • strong-cut bar    — score ≥ strong_factor·threshold cuts at once
      • best-of-lull arena — marginal candidates wait in an arena; a better
                            one displaces them; the survivor is cut once the
                            lull settles (grace_period) — one clip per lull
                            instead of five mediocre ones
      • quota + cooldown  — max_per_hour rolling cap and minimum spacing
      • overlap dedupe    — spans overlapping a recent cut are rejected (IoU)
      • auto-calibration  — threshold decays during long droughts and rises
                            under quota pressure (disabled when the
                            threshold is locked via SCF_THRESHOLD)
    """

    def __init__(self, opts, t0, on_handle=None):
        self.opts = opts
        self.cut_times = deque()
        self.spans = []
        self.arena = None
        self.last_clip_at = t0
        self.last_raise = t0
        self.last_decay = t0
        self.on_handle = on_handle or (lambda peak: None)
        self.locked = bool((os.environ.get("SCF_THRESHOLD") or "").strip())

    # ---------------------------------------------------------------- parts

    def _reserve(self, span):
        self.spans.append((span[0], span[1]))
        self.spans = self.spans[-8:]

    def _iou_blocked(self, span):
        a0, a1 = span
        for b0, b1 in self.spans:
            inter = min(a1, b1) - max(a0, b0)
            if inter <= 0:
                continue
            union = max(a1, b1) - min(a0, b0)
            if union > 0 and inter / union > self.opts.overlap_iou:
                return True
        return False

    def _hour_count(self, now):
        while self.cut_times and now - self.cut_times[0] >= 3600:
            self.cut_times.popleft()
        return len(self.cut_times)

    def _raise_threshold(self, now, why):
        if self.locked or now - self.last_raise < 600:
            return
        self.opts.threshold = min(self.opts.threshold_ceiling,
                                  self.opts.threshold * 1.06)
        self.last_raise = now
        log.info("auto-threshold ↑ %.2f (%s pressure)", self.opts.threshold, why)

    def _accept(self, cand, now):
        if now - self.last_clip_at < self.opts.cooldown:
            self._raise_threshold(now, "cooldown")
            return False
        if self._hour_count(now) >= self.opts.max_per_hour:
            self._raise_threshold(now, "quota")
            log.info("clip blocked: hourly quota (%s/h) reached",
                     self.opts.max_per_hour)
            return False
        if self._iou_blocked(cand["clip"]):
            log.info("clip blocked: overlaps a recent cut")
            return False
        self.cut_times.append(now)
        self._reserve(cand["clip"])
        self.last_clip_at = now
        if self.arena is cand:
            self.arena = None
        self.on_handle(cand["peak"])
        return True

    def _hold(self, cand, now):
        """Park a candidate in the best-of-lull arena (deduped, replaceable)."""
        self.on_handle(cand["peak"])
        self.arena = {"clip": cand["clip"], "score": cand["score"],
                      "reason": cand["reason"], "peak": cand["peak"],
                      "born": now}

    # ----------------------------------------------------------- main entry

    def observe(self, cands, now, enough=True):
        """Feed this tick's candidates; returns the ones accepted for cut."""
        if not enough:
            return []
        # quiet stretch — loosen slowly so droughts don't last forever
        if not self.locked and now - self.last_clip_at >= 2700 \
                and now - self.last_decay >= 1200:
            self.opts.threshold = max(self.opts.threshold_floor,
                                      self.opts.threshold * self.opts.quiet_decay)
            self.last_decay = now
            log.info("auto-threshold ↓ %.2f (quiet stretch)", self.opts.threshold)

        accepted = []
        for cand in cands[:3]:
            span = cand["clip"]
            if span[1] - span[0] < 10:
                continue
            if self._iou_blocked(span):
                continue
            if cand["score"] >= self.opts.threshold * self.opts.strong_factor:
                if self._accept(cand, now):
                    log.info("clip accepted: %.2f strong-cut | %s",
                             cand["score"], cand["reason"])
                    accepted.append(cand)
                else:
                    self._hold(cand, now)     # retry when the gates open
                break
            if self.arena is None or \
                    cand["score"] > self.arena["score"] * ARENA_MARGIN:
                self._hold(cand, now)

        if self.arena and now - self.arena["born"] >= self.opts.grace_period:
            held = self.arena
            if self._accept(held, now):
                log.info("clip accepted: %.2f best-of-lull | %s",
                         held["score"], held["reason"])
                accepted.append(held)
                self.arena = None
        return accepted


# ------------------------------------------------------------------ duplicates

def _recent_same_channel(name_prefix, gap_s=480):
    """True if a clip row for the same channel was published within the last
    `gap_s` seconds — another agent (or a claim race) already cut this."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return False
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/dashboard-data/clips",
             "-H", "Accept: application/vnd.github+json"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False
        entries = json.loads(r.stdout or "[]")
    except Exception:
        return False
    for entry in entries:
        name = entry.get("name", "")
        if not name.startswith(f"{name_prefix}_") or not name.endswith(".json"):
            continue
        ts = name[len(name_prefix) + 1:-5]         # "20261117_055047"
        try:
            t = datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if abs((datetime.now(timezone.utc) - t).total_seconds()) <= gap_s:
            return True
    return False


# ------------------------------------------------------------------ capturing

def capture_channel(platform, channel, reader_spec, opts, stop,
                    my_run_id="", my_slot=""):
    """Capture one channel until its live ends.

    Returns 'ended', 'stopped', or 'lost_claim' (a rival agent verifiably
    claimed this channel first — the caller fails over to another one)."""
    kind, url = reader_spec
    t0 = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_channel = re.sub(r"[^\w-]+", "_", channel)
    run_dir = Path("workdir") / f"{platform}_{safe_channel}_{stamp}"
    seg_dir = run_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "manifest.jsonl"

    scorer = scoring.Scorer(window=opts.window, baseline=opts.baseline)
    ring = deque(maxlen=300)

    stats = {"chat_messages": 0, "candidates": 0, "accepted": 0,
             "clips": 0, "last_score": None, "last_ai": None,
             "last_clip": None, "last_error": None}

    def _chat(at, user, text):
        stats["chat_messages"] += 1
        scorer.add_chat(at, user, text)

    chat = make_chat(platform, channel, _chat)
    if chat:
        chat.start()
    else:
        log.info("no chat source for %s — audio + transcript only", platform)

    reader = spawn_reader(url) if kind == "streamlink" else None
    ffmpeg = start_segmenter(seg_dir, opts.segment_time, reader=reader,
                             source=url if kind == "url" else None,
                             split=url if kind == "ffmpeg2" else None)
    log.info("capturing %s/%s into %s", platform, channel, seg_dir)

    tq = queue.Queue(maxsize=64)
    progress = [0]                     # last segment index Whisper finished
    if opts.transcription:
        threading.Thread(target=transcribe_worker,
                         args=(tq, t0, opts.segment_time, scorer, ring,
                               opts.whisper_model, progress),
                         daemon=True).start()

    pending = []
    gate = SelectionGate(opts, t0, on_handle=scorer.note_cut)
    last_idx = 0
    result = "ended"
    redirect = None
    last_claim_check = time.time()
    last_override_check = time.time()

    def transcript_window(rel_start, rel_end):
        """Only the lines around this moment — keeps the judge focused."""
        lo, hi = t0 + rel_start - 8, t0 + rel_end + 6
        lines = [(at, tag, text) for at, tag, text in list(ring)
                 if lo <= at <= hi]
        if len(lines) < 3:             # transcription lag — use the latest
            lines = list(ring)[-40:]
        return "\n".join(
            f"[{int(at) // 60:02d}:{int(at) % 60:02d}] {tag}: {text}"
            for at, tag, text in lines)

    def cut(t_start, t_end, score_val, reason, peak_t=None, accepted_at=None):
        """Cut, judge, upload and publish one clip.

        `peak_t` (absolute epoch) and `accepted_at` let the log show the real
        moment→board latency, which is what the 3–5 minute target measures.
        """
        peak_t = peak_t if peak_t is not None else t_start
        accepted_at = accepted_at or time.time()
        perf = {}
        rel_start, rel_end = t_start - t0, t_end - t0
        mark = time.perf_counter()
        if opts.scene_snap:
            try:
                rel_start, rel_end = clipper.snap_to_cuts(
                    seg_dir, rel_start, rel_end, opts.segment_time,
                    max_clip=opts.max_clip_seconds)
            except Exception as exc:
                log.warning("scene snap skipped: %s", exc)
        name = f"{safe_channel}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.mp4"
        out = run_dir / name
        try:
            out, actual_dur = clipper.cut_clip(
                seg_dir, rel_start, rel_end, opts.segment_time, out,
                max_duration=opts.max_clip_seconds)
        except Exception as exc:
            log.warning("clip cut failed: %s", exc)
            return
        if _recent_same_channel(safe_channel):
            out.unlink(missing_ok=True)
            log.info("duplicate dropped: %s already cut a moment seconds ago",
                     channel)
            return
        rival = _claim_lost(my_run_id, my_slot, platform, channel)
        if rival:
            out.unlink(missing_ok=True)
            log.info("duplicate dropped: agent %s owns %s (earlier claim)",
                     rival, channel)
            return
        perf["cut"] = time.perf_counter() - mark
        title = caption = hashtags = quote = None
        if opts.ai_ranking:
            # Whisper lags a few seconds behind the video — wait until the
            # payoff itself is transcribed so the judge reads the actual
            # crazy line, not the setup. 12 s is plenty for base on 4 cores;
            # the old 30 s wait was pure dead time on the critical path.
            need = int(max(rel_start, rel_end) // opts.segment_time) + 1
            deadline = time.time() + 12
            while progress[0] < need and time.time() < deadline \
                    and not stop.is_set():
                time.sleep(0.25)
            perf["whisper_wait"] = time.perf_counter() - mark - perf["cut"]
            mark = time.perf_counter()
            verdict = rank_clip(transcript_window(rel_start, rel_end),
                                channel, platform, wait_s=30)
            perf["ai"] = time.perf_counter() - mark
            if verdict is None:
                if score_val < opts.threshold * opts.strong_factor:
                    out.unlink(missing_ok=True)
                    log.info("clip dropped: AI unavailable and signal %.2f is "
                             "not strong enough (no bluff) | peak→drop %.0fs "
                             "(cut %.1fs, whisper %.1fs, AI %.1fs)", score_val,
                             time.time() - peak_t, perf.get("cut", 0),
                             perf.get("whisper_wait", 0), perf.get("ai", 0))
                    return
                stats["last_ai"] = "unavailable"
                log.info("AI unavailable — keeping strong-signal clip")
            else:
                vscore = float(verdict.get("score") or 0)
                quote = (verdict.get("quote") or "").strip()
                stats["last_ai"] = (f"keep={verdict.get('keep')} "
                                    f"score={vscore:.1f}")
                log.info("AI verdict: keep=%s score=%.1f quote=%r",
                         verdict.get("keep"), vscore, quote or None)
                rejected = verdict.get("keep") is False or bool(
                    vscore and vscore < opts.ai_min_score)
                # Yield override: a genuinely strong signal (≥1.4×threshold)
                # with a real transcribed quote is posted even when the 3B
                # local judge shrugs. The gate exists to block BLUFFS — a clip
                # with no quotable line — not loud, real moments. This is what
                # turns "one clip an hour" into a flowing board.
                strong = score_val >= opts.threshold
                if rejected and not (strong and quote):
                    stats["last_error"] = f"AI rejected ({vscore:.1f})"
                    out.unlink(missing_ok=True)
                    log.info("AI rejected the clip (score %.1f < %.1f) — "
                             "not interesting enough | peak→drop %.0fs "
                             "(cut %.1fs, whisper %.1fs, AI %.1fs)",
                             vscore, opts.ai_min_score, time.time() - peak_t,
                             perf.get("cut", 0), perf.get("whisper_wait", 0),
                             perf.get("ai", 0))
                    return
                if rejected:
                    stats["last_ai"] += " → override"
                    log.info("AI override: signal %.2f with a real quote — "
                             "keeping the clip", score_val)
                    # The judge's copy is negative by definition here: it
                    # called its own clip "awkward". Never publish that.
                    # Use the verbatim line + neutral context instead.
                    title = None
                    caption = (f"{quote} — caught live on {channel}."
                               if quote else None)
                    hashtags = None
                else:
                    title = verdict.get("title")
                    caption = verdict.get("caption")
                    hashtags = verdict.get("hashtags")
        # Never show a blank card: with no AI copy, use the verbatim quote.
        if not title:
            words = (quote or "").split()
            title = " ".join(words[:8]) or f"{channel} live moment"
        if not caption:
            caption = quote or f"{channel} live moment."
        if title and opts.title_template:
            try:
                title = opts.title_template.format(
                    channel=channel, platform=platform, title=title)
            except (KeyError, IndexError, ValueError):
                title = f"{title}"   # malformed template → keep the raw title
        # Brand tag is forced onto every clip (title, caption, hashtags).
        # The quote stays verbatim — it is evidence, not copy.
        title = title or f"{channel} live moment"
        caption = caption or f"{channel} live moment."
        entry = {"platform": platform, "channel": channel, "file": name,
                 "title": title, "caption": caption, "hashtags": hashtags,
                 "quote": quote,
                 "score": round(score_val, 2),
                 "trigger": reason,
                 "stream_t": [round(rel_start, 1), round(rel_end, 1)],
                 "duration": round(actual_dur, 1),
                 "created": datetime.now(timezone.utc).isoformat()}
        apply_brand(entry, opts.brand_tag)
        upload.append_manifest(manifest, entry)
        if opts.no_upload:
            log.info("kept %s (%.0fs, %s)", out, actual_dur, reason)
            return
        try:
            mark = time.perf_counter()
            dest = upload.upload_clip(out, channel,
                                      opts.drive_remote, opts.drive_folder)
            perf["upload"] = time.perf_counter() - mark
            mark = time.perf_counter()
            # publish FIRST — the board is what the user watches; the
            # manifest merge (download + re-upload) is bookkeeping.
            dashboard.publish_clip(entry, dest)
            perf["publish"] = time.perf_counter() - mark
            stats["clips"] += 1
            stats["last_clip"] = name
            log.info("uploaded %s (%.0fs, %s) → %s", name, actual_dur, reason, dest)
            log.info("latency: peak→board %.0fs | wait %.0fs, cut %.1fs, "
                     "whisper %.1fs, AI %.1fs, upload %.1fs, publish %.1fs",
                     time.time() - peak_t, accepted_at - peak_t,
                     perf.get("cut", 0), perf.get("whisper_wait", 0),
                     perf.get("ai", 0), perf.get("upload", 0),
                     perf.get("publish", 0))
        except Exception as exc:
            stats["last_error"] = f"upload failed: {exc}"
            log.error("upload failed: %s (clip kept at %s)", exc, out)
            return
        # bookkeeping only — never let a manifest merge look like a failed clip
        try:
            upload.upload_manifest(manifest, channel,
                                   opts.drive_remote, opts.drive_folder)
        except Exception as exc:
            log.warning("manifest upload failed: %s", exc)

    while not stop.is_set():
        stop.wait(2)
        now = time.time()

        # 0) claim watchdog — never two agents on one channel: if a rival
        #    verifiably claimed this channel BEFORE us, abort now and let
        #    the supervisor fail over to a different channel.
        if now - last_claim_check >= 60:
            last_claim_check = now
            rival = _claim_lost(my_run_id, my_slot, platform, channel)
            if rival:
                log.info("%s/%s: agent %s claimed it first — aborting "
                         "duplicate capture", platform, channel, rival)
                result = "lost_claim"
                break

        # 0b) dashboard redirect — the user pasted a link + Watch on this
        #     slot. Switch in place (~20 s) instead of restarting the job.
        if my_slot and now - last_override_check >= 20:
            last_override_check = now
            ov = dashboard.take_override(my_slot)
            if ov and ov.get("platform") == platform:
                want = str(ov.get("channel") or "").lstrip("@").lower()
                if want and want != channel.lstrip("@").lower():
                    log.info("%s: dashboard redirect → %s/%s", my_slot,
                             platform, want)
                    redirect = want
                    result = "redirect"
                    break

        # 1) collect fully written segments (complete once the next exists)
        while True:
            idx = last_idx + 1
            f = seg_dir / f"seg_{idx:05d}.ts"
            nxt = seg_dir / f"seg_{idx + 1:05d}.ts"
            if not f.exists() or not nxt.exists():
                break
            last_idx = idx
            t_seg = t0 + (idx - 1) * opts.segment_time
            try:
                lr = audio.loudness(f)
            except Exception:
                lr = None
            if lr:
                scorer.add_audio(t_seg, lr[0], lr[1])
            else:
                scorer.add_audio(t_seg, -91.0, -91.0)
            if opts.transcription and not tq.full():
                tq.put((idx, f))

        # 2) retention — drop segments older than the keep_buffer
        cutoff = last_idx - opts.keep_buffer // opts.segment_time
        if cutoff > 1:
            for f in seg_dir.glob("seg_*.ts"):
                try:
                    if int(f.stem.split("_")[1]) < cutoff:
                        f.unlink(missing_ok=True)
                except (IndexError, ValueError):
                    pass

        # 3) finish pending clips whose tail has now been recorded
        coverage_end = t0 + last_idx * opts.segment_time
        for item in list(pending):
            t_start, t_end, s, trig, peak, accepted = item
            if coverage_end >= t_end:
                cut(t_start, t_end, s, trig, peak, accepted)
                pending.remove(item)

        # 4) peak-based selection with quality gates (v4)
        if now - t0 >= opts.min_uptime:
            cands = scorer.candidates(
                now,
                threshold=opts.threshold,
                min_signals=opts.min_signals,
                prominence=opts.peak_prominence,
                min_distance=opts.peak_min_distance,
                pre_pad=opts.pre_pad,
                reaction_tail=opts.reaction_tail,
                min_clip=opts.min_clip_seconds,
                max_clip=opts.max_clip_seconds)
            stats["candidates"] += len(cands)
            if cands:
                stats["last_score"] = round(
                    max(c["score"] for c in cands), 2)
            enough = last_idx * opts.segment_time >= opts.min_clip_seconds
            for cand in gate.observe(cands, now, enough):
                stats["accepted"] += 1
                pending.append((cand["clip"][0], cand["clip"][1],
                                cand["score"], cand["reason"],
                                cand["peak"], time.time()))
                log.info("clip selected: %.2f | %s | %+.0fs..%+.0fs",
                         cand["score"], cand["reason"],
                         cand["clip"][0] - t0, cand["clip"][1] - t0)

        # 4b) heartbeat — one tiny commit a minute, so a live run is visible
        dashboard.write_progress(my_slot, {
            "slot": my_slot, "platform": platform, "channel": channel,
            "run_id": my_run_id, "state": "watching",
            "updated": datetime.now(timezone.utc).isoformat(),
            "seconds": round(now - t0), "segments": last_idx,
            "chat_connected": bool(chat),
            "chat_messages": stats["chat_messages"],
            "threshold": round(opts.threshold, 2),
            "candidates": stats["candidates"],
            "accepted": stats["accepted"], "clips": stats["clips"],
            "arena_score": round(gate.arena["score"], 2)
                           if gate.arena else None,
            "last_candidate_score": stats["last_score"],
            "last_ai": stats["last_ai"], "last_clip": stats["last_clip"],
            "last_error": stats["last_error"],
        })

        # 5) did the live end?
        if reader is not None and reader.poll() is not None:
            log.info("%s/%s live ended (source closed)", platform, channel)
            break
        if ffmpeg.poll() is not None:
            log.info("%s/%s live ended (ffmpeg closed)", platform, channel)
            break

    dashboard.write_progress(my_slot, {
        "slot": my_slot, "platform": platform, "channel": channel,
        "run_id": my_run_id, "state": result,
        "updated": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.time() - t0), "segments": last_idx,
        "chat_connected": bool(chat),
        "chat_messages": stats["chat_messages"],
        "candidates": stats["candidates"], "accepted": stats["accepted"],
        "clips": stats["clips"], "last_ai": stats["last_ai"],
        "last_clip": stats["last_clip"], "last_error": stats["last_error"],
    }, min_gap=0)

    if result == "lost_claim":
        log.info("dropping %s buffered clip(s) — the rival agent owns this "
                 "channel now", len(pending))
        pending.clear()
    elif result == "redirect":
        log.info("dropping %s buffered clip(s) — dashboard redirected us",
                 len(pending))
        pending.clear()
    elif gate.arena:
        a = gate.arena
        pending.append((a["clip"][0], a["clip"][1], a["score"], a["reason"],
                        a["peak"], time.time()))
        log.info("flushing best-of-lull candidate at stream end (%.2f)",
                 a["score"])
    for t_start, t_end, s, trig, peak, accepted in pending:
        cut(t_start, min(t_end, t0 + last_idx * opts.segment_time), s, trig,
            peak, accepted)
    _stop(reader)
    _stop(ffmpeg)
    if chat:
        chat.stop()
    if opts.transcription:
        tq.put(None)
    if stop.is_set():
        result = "stopped"
    log.info("finished %s/%s after %.0fs", platform, channel, time.time() - t0)
    return result, redirect


# ---------------------------------------------------------------- supervisor

def _override_target(channels, platform, opts, ended_at, my_run_id, slot):
    """Take a pending dashboard redirect, if any, as the next target."""
    ov = dashboard.take_override(slot) if slot else None
    if not ov or ov.get("platform") != platform or not ov.get("channel"):
        return None
    forced = str(ov["channel"])
    log.info("dashboard redirect → %s/%s", platform, forced)
    return choose_target(build_pool(channels, platform, forced), ended_at,
                         opts, my_run_id, my_slot=slot)


def _wait_for_target(channels, platform, pool, opts, ended_at, my_run_id,
                     slot, stop):
    """Idle in-process for up to idle_retry_seconds, so a channel that goes
    live (or a pasted link) is grabbed without waiting for a new workflow."""
    wait_s = max(30, int(opts.idle_retry_seconds))
    deadline = time.time() + wait_s
    while not stop.is_set() and time.time() < deadline:
        target = _override_target(channels, platform, opts, ended_at,
                                  my_run_id, slot) or \
            choose_target(pool, ended_at, opts, my_run_id, my_slot=slot)
        if target:
            return target
        left = int(deadline - time.time())
        log.info("no live %s channel yet — retrying in 30 s (%ss left)",
                 platform, max(0, left))
        stop.wait(30)
    return None


def _wait_for_link(platform, slot, opts, stop):
    """Manual mode: wait for the dashboard to hand this agent a link.

    Nothing is ever picked automatically. The tile shows "waiting for a
    pasted link"; Watch writes an override and the agent starts within
    ~10 s. Returns the channel, or "" when the wait times out."""
    wait_s = max(60, int(opts.idle_retry_seconds))
    deadline = time.time() + wait_s
    log.info("agent %s waiting for a pasted %s link (%ss)", slot, platform,
             wait_s)
    while not stop.is_set() and time.time() < deadline:
        ov = dashboard.take_override(slot) if slot else None
        if ov and ov.get("platform") == platform and ov.get("channel"):
            log.info("dashboard link → %s/%s", platform, ov["channel"])
            return str(ov["channel"])
        stop.wait(10)
    return ""


def _claim_manual(platform, channel, opts, my_run_id, slot, stop,
                  wait_s=None):
    """Claim one explicit channel and wait for it to be live.

    Never substitutes a different channel: the pasted link is the job.
    Returns (platform, channel, spec) or None on timeout/stop."""
    channel = str(channel or "").strip().lstrip("@")
    if not channel or channel == "auto":
        return None
    key = _identity(platform, channel)
    deadline = time.time() + (wait_s if wait_s is not None
                              else max(60, int(opts.idle_retry_seconds)))
    while not stop.is_set():
        claimed, _ = _claims_snapshot(my_run_id, slot)
        if key not in claimed:
            try:
                live, spec = livecheck.probe(platform, channel)
            except Exception as exc:
                log.debug("probe %s/%s failed: %s", platform, channel, exc)
                live, spec = False, None
            if live:
                my_since = dashboard.publish_agent_state(
                    slot or "slot", platform, channel, my_run_id)
                if not my_since:
                    log.warning("%s: claim write failed — retrying", slot)
                else:
                    stop.wait(3)      # let a racing agent commit too
                    _, details = _claims_snapshot(my_run_id, slot)
                    rival = details.get(key)
                    mine = (my_since, slot or "slot")
                    if not (rival and rival.get("since") and
                            (rival["since"], rival["slot"]) < mine):
                        return (platform, channel, spec)
                    log.info("%s/%s: %s claimed it first — waiting",
                             platform, channel, rival["slot"])
            else:
                log.info("%s/%s is offline — retrying in 20 s",
                         platform, channel)
        else:
            log.info("%s/%s is monitored by another agent — waiting",
                     platform, channel)
        if time.time() >= deadline:
            return None
        stop.wait(20)
    return None


def run(platform, channel, opts: Options, channels=None, slot="manual") -> int:
    """Manual-only supervisor: watch the link this agent was given.

    There is no pool scan and no automatic selection. A link comes from the
    dashboard (override) or the workflow input. When that live ends the
    agent exits — the user decides what to watch next."""
    stop = threading.Event()

    def on_signal(_s, _f):
        log.info("stop signal received — shutting down")
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    my_run_id = os.environ.get("GITHUB_RUN_ID", "")
    dashboard.reset_agent_state(slot, my_run_id)

    # A link pasted on the dashboard wins over the workflow input.
    ov = dashboard.take_override(slot) if slot else None
    if ov and ov.get("platform") == platform and ov.get("channel"):
        channel = str(ov["channel"])
        log.info("dashboard link at start → %s/%s", platform, channel)

    if not channel or str(channel).strip().lower() == "auto":
        channel = _wait_for_link(platform, slot, opts, stop)
        if not channel:
            log.info("no link given to %s — exiting until you press Watch",
                     slot)
            dashboard.release_agent_state(slot, my_run_id)
            return 0

    while not stop.is_set():
        target = _claim_manual(platform, channel, opts, my_run_id, slot, stop)
        if target is None:
            log.info("%s/%s was not available — agent exiting", platform,
                     channel)
            break
        p, n, spec = target
        log.info("▸ agent %s watching %s/%s", slot, p, n)
        rename_run(p, n, slot)
        if not dashboard.publish_agent_state(slot, p, n, my_run_id):
            log.warning("%s: claim refresh failed — capture continues", slot)

        result, redirect = capture_channel(p, n, spec, opts, stop,
                                           my_run_id=my_run_id, my_slot=slot)
        dashboard.release_agent_state(slot, my_run_id)
        if result == "stopped":
            break

        # A dashboard redirect during capture is the only reason to switch
        # to another channel without a new workflow run.
        if result == "redirect" and redirect:
            channel = redirect
            log.info("%s/%s → new pasted link %s", slot, platform, channel)
            stop.wait(3)
            continue

        log.info("%s/%s ended — agent exiting (manual mode)", p, n)
        break

    dashboard.release_agent_state(slot, my_run_id)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Capture agent")
    ap.add_argument("--platform", required=True,
                    choices=["twitch", "kick", "youtube"])
    ap.add_argument("--channel", default="",
                    help="Channel to watch (manual mode; empty = wait for a link)")
    ap.add_argument("--slot", default="manual",
                    help="Agent slot id (e.g. twitch-1); run title carries it")
    ap.add_argument("--config", default="config/channels.yml")
    ap.add_argument("--no-upload", action="store_true",
                    help="keep clips in workdir/ instead of uploading")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _channels, opts = load_config(args.config)
    if args.no_upload:
        opts.no_upload = True
    return run(args.platform, args.channel, opts, _channels, args.slot)


if __name__ == "__main__":
    sys.exit(main())
