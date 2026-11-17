"""Capture agent (supervisor).

The agent is pinned to a *slot*, not to a single stream: it monitors its
assigned channel continuously until that live ends, then automatically
switches to the next live channel from the pool and keeps going. Other agents
are avoided via run-title claims, so two agents never capture the same
stream.

Flow:
    pick next live + unclaimed channel ──► capture until it ends
              ▲                                    │
              └────────────── switch ──────────────┘
"""

import argparse
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

from . import audio, clipper, livecheck, score as scoring, transcribe, upload
from .ai import rank_clip
from .chat import make_chat
from .config import Options, load_config

log = logging.getLogger("capture")

RUN_TITLE = "clip-capture|{platform}|{channel}"


# --------------------------------------------------------------- run claiming

def _parse_title(title):
    """'clip-capture|twitch|jynxzi' → ('twitch', 'jynxzi')."""
    if not title or not title.startswith("clip-capture|"):
        return None
    parts = title.split("|")
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def _other_claimed_channels(my_run_id):
    """Channels currently held by other capture agents."""
    if not os.environ.get("GITHUB_REPOSITORY"):
        return set()
    try:
        r = subprocess.run(
            ["gh", "run", "list", "--workflow", "capture.yml", "--limit", "30",
             "--json", "databaseId,status,displayTitle"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return set()
        data = json.loads(r.stdout or "[]")
    except Exception:
        return set()
    claimed = set()
    for run in data:
        if str(run.get("databaseId")) == str(my_run_id):
            continue
        if (run.get("status") or "") not in ("in_progress", "queued"):
            continue
        parsed = _parse_title(run.get("displayTitle") or "")
        if parsed:
            claimed.add(parsed)
    return claimed


def rename_run(platform, channel):
    """Tell other agents we moved: the run title is our claim."""
    if not os.environ.get("GITHUB_REPOSITORY"):
        return
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return
    title = RUN_TITLE.format(platform=platform, channel=channel)
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


def transcribe_worker(q, t0, seg_time, scorer, ring, model_name):
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
                text = seg["text"]
                result = transcribe.analyze(text)
                scorer.add_keywords(base + seg["start"], result["score"],
                                    command=result["command"])
                tag = "★ CLIP COMMAND" if result["command"] else "transcript"
                mm = int(base + seg["start"]) // 60
                ring.append(f"[{mm:02d}:xx] {tag}: {text}")
                log.info("%s: %s", tag, text[:120])
        except Exception as exc:  # keep the worker alive
            log.warning("transcription failed: %s", exc)
        finally:
            q.task_done()


# ------------------------------------------------------------ channel picking

def build_pool(channels, platform, channel):
    """Ordered pool of channels this agent may take (config first, then the
    channel it was dispatched with)."""
    pool, seen = [], set()
    for c in channels or []:
        if c.get("enabled") is False:
            continue
        p, n = c.get("platform"), c.get("name")
        if not p or not n:
            continue
        key = (p, n.lstrip("@").lower())
        if key in seen:
            continue
        seen.add(key)
        pool.append((p, n))
    key = (platform, channel.lstrip("@").lower())
    if key not in seen:
        pool.insert(0, (platform, channel))
    return pool


def choose_target(pool, ended_at, opts, my_run_id, retry_after=180):
    """Pick the next live, unclaimed channel from the pool."""
    claimed = _other_claimed_channels(my_run_id)
    now = time.time()
    for p, n in pool:
        key = (p, n.lstrip("@").lower())
        if key in claimed:
            continue
        if now - ended_at.get(key, 0) < retry_after:
            continue                       # it just ended — let it rest
        try:
            live, spec = livecheck.probe(p, n)
        except Exception as exc:
            log.debug("probe %s/%s failed: %s", p, n, exc)
            continue
        if live:
            return (p, n, spec)
        log.debug("%s/%s is offline", p, n)
    return None


# ------------------------------------------------------------------ capturing

def capture_channel(platform, channel, reader_spec, opts, stop):
    """Capture one channel until its live ends. Returns 'ended' or 'stopped'."""
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

    chat = make_chat(platform, channel, scorer.add_chat)
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
    if opts.transcription:
        threading.Thread(target=transcribe_worker,
                         args=(tq, t0, opts.segment_time, scorer, ring,
                               opts.whisper_model),
                         daemon=True).start()

    pending = []
    last_clip_at = 0.0
    last_idx = 0
    result = "ended"

    def cut(t_start, t_end, score_val, reason):
        rel_start, rel_end = t_start - t0, t_end - t0
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
        title = None
        if opts.ai_ranking:
            verdict = rank_clip("\n".join(ring))
            if verdict:
                log.info("AI verdict: %s", verdict)
                if verdict.get("keep") is False:
                    out.unlink(missing_ok=True)
                    log.info("AI dropped the clip")
                    return
                title = verdict.get("title")
        entry = {"platform": platform, "channel": channel, "file": name,
                 "title": title, "score": round(score_val, 2),
                 "trigger": reason,
                 "stream_t": [round(rel_start, 1), round(rel_end, 1)],
                 "duration": round(actual_dur, 1),
                 "created": datetime.now(timezone.utc).isoformat()}
        upload.append_manifest(manifest, entry)
        if opts.no_upload:
            log.info("kept %s (%.0fs, %s)", out, actual_dur, reason)
            return
        try:
            dest = upload.upload_clip(out, channel,
                                      opts.drive_remote, opts.drive_folder)
            upload.upload_manifest(manifest, channel,
                                   opts.drive_remote, opts.drive_folder)
            log.info("uploaded %s (%.0fs, %s) → %s", name, actual_dur, reason, dest)
        except Exception as exc:
            log.error("upload failed: %s (clip kept at %s)", exc, out)

    while not stop.is_set():
        stop.wait(5)
        now = time.time()

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
            t_start, t_end, s, trig = item
            if coverage_end >= t_end:
                cut(t_start, t_end, s, trig)
                pending.remove(item)

        # 4) peak-based selection
        if now - t0 >= opts.min_uptime and not pending:
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
            enough = last_idx * opts.segment_time >= opts.min_clip_seconds
            if cands and enough and now - last_clip_at >= opts.cooldown:
                cand = cands[0]
                t_start, t_end = cand["clip"]
                if t_end - t_start >= 10 and t_start > t0 - opts.segment_time:
                    pending.append((t_start, t_end, cand["score"], cand["reason"]))
                    last_clip_at = now
                    scorer.note_cut(cand["peak"])
                    log.info("clip selected: %.2f | %s | %+.0fs..%+.0fs",
                             cand["score"], cand["reason"],
                             t_start - t0, t_end - t0)

        # 5) did the live end?
        if reader is not None and reader.poll() is not None:
            log.info("%s/%s live ended (source closed)", platform, channel)
            break
        if ffmpeg.poll() is not None:
            log.info("%s/%s live ended (ffmpeg closed)", platform, channel)
            break

    for t_start, t_end, s, trig in pending:
        cut(t_start, min(t_end, t0 + last_idx * opts.segment_time), s, trig)
    _stop(reader)
    _stop(ffmpeg)
    if chat:
        chat.stop()
    if opts.transcription:
        tq.put(None)
    if stop.is_set():
        result = "stopped"
    log.info("finished %s/%s after %.0fs", platform, channel, time.time() - t0)
    return result


# ---------------------------------------------------------------- supervisor

def run(platform, channel, opts: Options, channels=None) -> int:
    stop = threading.Event()

    def on_signal(_s, _f):
        log.info("stop signal received — shutting down")
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    pool = build_pool(channels, platform, channel)
    log.info("agent pool: %s", ", ".join(f"{p}/{n}" for p, n in pool))
    my_run_id = os.environ.get("GITHUB_RUN_ID", "")
    ended_at = {}
    first = True

    while not stop.is_set():
        target = choose_target(pool, ended_at, opts, my_run_id)
        if target is None:
            if first:
                log.info("no live channel in the pool yet — waiting")
                first = False
            stop.wait(30)
            continue
        p, n, spec = target
        log.info("▸ agent taking %s/%s", p, n)
        rename_run(p, n)
        result = capture_channel(p, n, spec, opts, stop)
        if result == "stopped":
            break
        ended_at[(p, n.lstrip("@").lower())] = time.time()
        log.info("%s/%s ended — switching to the next live channel", p, n)
        stop.wait(5)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Capture agent")
    ap.add_argument("--platform", required=True,
                    choices=["twitch", "kick", "youtube"])
    ap.add_argument("--channel", required=True)
    ap.add_argument("--config", default="config/channels.yml")
    ap.add_argument("--no-upload", action="store_true",
                    help="keep clips in workdir/ instead of uploading")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _channels, opts = load_config(args.config)
    if args.no_upload:
        opts.no_upload = True
    return run(args.platform, args.channel, opts, _channels)


if __name__ == "__main__":
    sys.exit(main())
