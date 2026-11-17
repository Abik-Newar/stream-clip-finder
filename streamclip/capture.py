"""Real-time capture worker.

Records a live stream in short segments, listens to chat, analyses loudness
and transcripts, and cuts + uploads a clip whenever the combined hype score
crosses the threshold.

Usage:
    python -m streamclip.capture --platform twitch --channel shroud
    python -m streamclip.capture --platform kick --channel xqc --no-upload
"""

import argparse
import logging
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


def spawn_reader(url):
    """streamlink keeps the Twitch/Kick playlist fresh and skips ad breaks."""
    return subprocess.Popen(
        ["streamlink", url, "best", "--twitch-disable-ads", "--stdout"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def start_segmenter(seg_dir, seg_time, reader=None, source=None, split=None):
    """ffmpeg splits the continuous stream into seg_00001.ts, seg_00002.ts, …
    Segment i covers stream-relative [(i-1)*seg_time, i*seg_time).

    Modes:
      reader  — stdin from a streamlink process
      split   — (video_url, audio_url) merged by ffmpeg (-map both inputs)
      source  — a single manifest URL read directly
    """
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


def _stop(proc):
    """Terminate a child process reliably: SIGTERM → grace wait → SIGKILL."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def run(platform, channel, opts: Options, channels=None) -> int:
    channels = channels or []
    cfg_entry = next(
        (c for c in channels
         if c.get("platform") == platform
         and str(c.get("name", "")).lstrip("@").lower()
         == channel.lstrip("@").lower()),
        None)
    if cfg_entry is not None and cfg_entry.get("enabled") is False:
        log.info("%s/%s is disabled in channels.yml — not capturing", platform, channel)
        return 0

    live, reader_spec = livecheck.probe(platform, channel)
    if not live:
        log.info("%s/%s is not live — nothing to do", platform, channel)
        return 0
    kind, url = reader_spec
    log.info("%s/%s is live (source: %s)", platform, channel, kind)

    t0 = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_channel = re.sub(r"[^\w-]+", "_", channel)
    run_dir = Path("workdir") / f"{platform}_{safe_channel}_{stamp}"
    seg_dir = run_dir / "segments"
    seg_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.jsonl"

    scorer = scoring.Scorer(window=opts.window, baseline=opts.baseline)
    ring = deque(maxlen=300)

    chat = make_chat(platform, channel, scorer.add_chat)
    if chat:
        chat.start()
    else:
        log.info("no chat source for %s — scoring uses audio + transcript", platform)

    reader = spawn_reader(url) if kind == "streamlink" else None
    ffmpeg = start_segmenter(seg_dir, opts.segment_time, reader=reader,
                             source=url if kind == "url" else None,
                             split=url if kind == "ffmpeg2" else None)
    log.info("capturing into %s", seg_dir)

    tq = queue.Queue(maxsize=64)
    if opts.transcription:
        threading.Thread(target=transcribe_worker,
                         args=(tq, t0, opts.segment_time, scorer, ring,
                               opts.whisper_model),
                         daemon=True).start()

    pending = []            # (t_start, t_end, score) awaiting their tail
    last_clip_at = 0.0
    last_idx = 0
    stop = threading.Event()

    def on_signal(_s, _f):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def cut(t_start, t_end, score_val, trigger):
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
                 "trigger": trigger,
                 "stream_t": [round(rel_start, 1), round(rel_end, 1)],
                 "duration": round(actual_dur, 1),
                 "created": datetime.now(timezone.utc).isoformat()}
        upload.append_manifest(manifest, entry)
        if opts.no_upload:
            log.info("kept %s (%.0fs, %s)", out, actual_dur, trigger)
            return
        try:
            dest = upload.upload_clip(out, channel,
                                      opts.drive_remote, opts.drive_folder)
            upload.upload_manifest(manifest, channel,
                                   opts.drive_remote, opts.drive_folder)
            log.info("uploaded %s (%.0fs, %s) → %s", name, actual_dur, trigger, dest)
        except Exception as exc:
            log.error("upload failed: %s (clip kept at %s)", exc, out)

    while not stop.is_set():
        time.sleep(5)
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

        # 4) score and maybe trigger — adaptive clip span, strictly < 90 s
        if now - t0 >= opts.min_uptime and not pending:
            value, detail = scorer.evaluate(now)
            command_hit = (opts.clip_command_trigger
                           and scorer.recent_clip_commands(now) > 0)
            enough_history = last_idx * opts.segment_time >= opts.min_clip_seconds
            if ((value >= opts.threshold or command_hit) and enough_history
                    and now - last_clip_at >= opts.cooldown):
                trigger = "clip command" if command_hit else "score"
                # clip start: before the spike (chat reacts late), default
                # lookback as floor, adaptive reach-back up to the 90 s cap
                spike = scorer.spike_start(now)
                earliest = now - opts.clip_length
                if spike:
                    earliest = min(earliest, spike - opts.pad_before)
                t_start = max(earliest, now - opts.max_clip_seconds + opts.tail)
                t_start = min(t_start, now - opts.min_clip_seconds)
                t_end = now + opts.tail
                pending.append((t_start, t_end, value, trigger))
                last_clip_at = now
                log.info("clip triggered (%s, score %.2f, %.0fs back → %s)",
                         trigger, value, now - t_start, detail)

        # 5) stop conditions
        if reader is not None and reader.poll() is not None:
            log.info("stream source ended")
            break
        if ffmpeg.poll() is not None:
            log.info("ffmpeg exited (stream over?)")
            break

    # flush any unfinished clips against whatever is still on disk
    for t_start, t_end, s, trig in pending:
        cut(t_start, min(t_end, t0 + last_idx * opts.segment_time), s, trig)
    _stop(reader)
    _stop(ffmpeg)
    if chat:
        chat.stop()
    if opts.transcription:
        tq.put(None)
    log.info("capture finished after %.0fs", time.time() - t0)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Real-time clip capture worker")
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
