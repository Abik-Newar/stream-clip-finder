"""Non-realtime clipper — clip one finished YouTube video on demand.

The live capture path is pinned to a channel and watches a stream; this path
takes one YouTube video URL, downloads it, transcribes it, picks clip-worthy
moments with the gta6 picker, and publishes up to N clips. It never watches,
auto-dispatches, or scans anything by itself.

The dashboard calls this "Non-realtime": one finished video in, a handful of
boards rows out.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import dashboard, gta6_pick, transcribe, upload
from .ai import hype_free, rank_clip
from .brand import apply_brand
from .config import load_config

log = logging.getLogger("vod")

SLOT = "non-realtime"
VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_HOSTS = {"youtube.com", "youtu.be"}
TAG_RE = re.compile(r"\s*\[[A-Z]+[^\]]*\]")


# ------------------------------------------------------------------ youtube

def normalize_url(raw: str) -> tuple[str, str]:
    """Return (video_id, canonical watch URL) or raise ValueError.

    Accepts the links users actually copy: watch, youtu.be, shorts, live,
    embed, m. and music. Rejects channels, playlists, and other sites.
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError("paste a YouTube video link")
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    u = urlparse(s)
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    if host.startswith("music."):
        host = host[6:]
    if host not in YT_HOSTS:
        raise ValueError("only YouTube video links are accepted")
    parts = [p for p in u.path.split("/") if p]
    vid = ""
    if host == "youtu.be":
        vid = parts[0] if parts else ""
    elif parts and parts[0] == "watch":
        vid = parse_qs(u.query).get("v", [""])[0]
    elif parts and parts[0] in ("shorts", "live", "embed", "v"):
        vid = parts[1] if len(parts) > 1 else ""
    if not VIDEO_RE.match(vid):
        raise ValueError(
            "that is not a YouTube video link (use a watch, shorts, "
            "youtu.be, or live video link)")
    return vid, f"https://www.youtube.com/watch?v={vid}"


def _cookies() -> list[str]:
    """Decode the YT_COOKIES secret into a cookies file, when present."""
    raw = os.environ.get("YT_COOKIES", "").strip()
    if not raw:
        return []
    try:
        path = Path.home() / ".scf_yt_cookies.txt"
        path.write_bytes(base64.b64decode(raw))
        path.chmod(0o600)
        return ["--cookies", str(path)]
    except Exception as exc:
        log.warning("YT_COOKIES could not be decoded: %s", exc)
        return []


def _ytdlp(args: list[str], timeout: int = 300) -> str:
    cmd = ["yt-dlp", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp is not installed") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp timed out") from None
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        tail = "\n".join(err.splitlines()[-8:])
        low = err.lower()
        if "sign in to confirm" in low or "not a bot" in low or "bot check" in low:
            raise RuntimeError(
                "YouTube bot-check — set the YT_COOKIES repository secret "
                "to a base64 cookies.txt and retry")
        raise RuntimeError(f"yt-dlp failed: {tail or 'unknown error'}")
    return r.stdout


def probe(url: str) -> dict:
    """Read metadata without downloading the video."""
    out = _ytdlp(["--dump-single-json", "--no-playlist", "--no-warnings",
                  "--skip-download", *_cookies(), url], timeout=180)
    line = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    try:
        doc = json.loads(line)
    except ValueError:
        raise RuntimeError("YouTube returned no video metadata") from None
    if not isinstance(doc, dict):
        raise RuntimeError("YouTube returned no video metadata")
    if doc.get("_type") not in (None, "video"):
        raise RuntimeError("that link is not a single video")
    if doc.get("is_live") or doc.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError(
            "that YouTube link is live — use a YouTube live agent tile instead")
    try:
        duration = float(doc.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        raise RuntimeError("could not read the video length")
    channel = str(doc.get("uploader") or doc.get("channel") or "YouTube").strip()
    title = str(doc.get("title") or "YouTube video").strip()
    vid = str(doc.get("id") or "").strip()
    return {
        "id": vid if VIDEO_RE.match(vid) else "",
        "title": title,
        "channel": channel,
        "duration": duration,
        "url": str(doc.get("webpage_url") or url),
    }


# -------------------------------------------------------------------- media

def _ffmpeg(args: list[str], timeout: int = 1800) -> None:
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out") from None
    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg failed: {tail or 'unknown error'}")


def download(url: str, out_dir: Path) -> Path:
    """Download a 720p-or-better MP4 into `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = ("bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
           "b[height<=720][ext=mp4]/b[height<=720]/b")
    _ytdlp(["-f", fmt, "--merge-output-format", "mp4",
            "--no-playlist", "--no-warnings", "--newline",
            "--retries", "3", "--fragment-retries", "5",
            "--socket-timeout", "30",
            *_cookies(),
            "-o", str(out_dir / "source.%(ext)s"), url],
           timeout=3600)
    files = [p for p in out_dir.glob("source.*") if p.is_file()]
    if not files:
        raise RuntimeError("YouTube download produced no file")
    return max(files, key=lambda p: p.stat().st_size)


def extract_audio(video: Path, wav: Path) -> None:
    _ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(wav)], timeout=1800)


def rms_db(wav: Path) -> list[float]:
    """Per-second dBFS of a 16 kHz mono PCM WAV."""
    out: list[float] = []
    try:
        import audioop
    except ImportError:                 # Python 3.13+
        audioop = None
    with wave.open(str(wav), "rb") as wf:
        rate = wf.getframerate()
        width = wf.getsampwidth()
        if width != 2:
            raise RuntimeError("unexpected audio sample width")
        frames_per_bin = max(1, rate)
        while True:
            frames = wf.readframes(frames_per_bin)
            if not frames:
                break
            if audioop is not None:
                rms = audioop.rms(frames, width)
            else:
                from array import array
                samples = array("h")
                samples.frombytes(frames)
                if sys.byteorder == "big":
                    samples.byteswap()
                rms = math.sqrt(sum(x * x for x in samples) / len(samples)) \
                    if samples else 0
            db = 20 * math.log10(rms / 32768) if rms else -90.0
            out.append(round(max(-90.0, min(0.0, db)), 1))
    return out


# ----------------------------------------------------------------- picking

def _lift(db: list[float], start: float, end: float) -> int:
    """Local loudness rise for one transcript line, in dB."""
    if not db:
        return 0
    b0 = max(0, int(start))
    b1 = min(len(db) - 1, int(max(start, end)))
    if b1 < b0:
        return 0
    peak = max(db[b0:b1 + 1])
    before = db[max(0, b0 - 60):b0]
    ambient = statistics.median(before) if len(before) >= 3 \
        else statistics.median(db)
    jump = db[max(0, b0 - 20):b0]
    local = statistics.median(jump) if len(jump) >= 2 else ambient
    return max(0, min(30, int(round(max(peak - ambient, peak - local)))))


def _heat(text: str, loud: int = 0) -> int:
    """Text + loudness heat for a VOD line (no live chat exists)."""
    words = re.findall(r"[^\W\d_]+", text, re.UNICODE)
    heat = 18.0
    heat += min(loud, 20) * 0.7
    heat += 10 * min(len(gta6_pick.EXC.findall(text)), 3)
    heat += 10 * min(len(gta6_pick.LAUGH.findall(text)), 2)
    heat += 8 * min(len(gta6_pick.MULTI_CUE.findall(text)), 2)
    heat += 6 * min(len(gta6_pick.SUPERLATIVE.findall(text)), 2)
    if gta6_pick.CLAIM.match(text):
        heat += 5
    heat += min(len(words), 30) / 3.0
    return max(0, min(100, int(round(heat))))


def build_lines(segments: list[dict], db: list[float]) -> list[dict]:
    """Turn Whisper segments into upstream-shaped tagged lines."""
    lines: list[dict] = []
    for seg in segments:
        if not transcribe.confident(seg):
            continue
        text = " ".join(str(seg.get("text") or "").split())
        if not text:
            continue
        try:
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        plain = TAG_RE.sub("", text).strip()
        if not plain:
            continue
        words = re.findall(r"[^\W\d_]+", plain, re.UNICODE)
        loud = _lift(db, start, end)
        heat = _heat(plain, loud)
        txt = plain
        if loud >= 5:
            txt += f" [LOUD +{loud}dB]"
        if len(words) >= 6:
            txt += " [MOMENT talk 1.0]"
        txt += f" [HEAT {heat}]"
        lines.append({"i": len(lines), "s": round(start, 2),
                      "e": round(end, 2), "txt": txt, "plain": plain,
                      "loud": loud, "chat": 0, "heat": heat})
    lines.sort(key=lambda l: (l["s"], l["e"]))
    for i, line in enumerate(lines):
        line["i"] = i
    return lines


def pick_moments(lines: list[dict], title: str = "", channel: str = "",
                 max_clips: int = 4, min_gap: int = 45,
                 min_score: float = 0.8, max_w: float = 32.0,
                 min_talk: float = 10.0) -> list[dict]:
    """Run the gta6 picker, then keep the best spaced moments."""
    if len(lines) < 6:
        return []
    picks = gta6_pick.pick(
        lines, name=channel, title=title, n=max(max_clips * 4, 8),
        max_w=max(16.0, min(90.0, max_w)), min_talk=min_talk,
        min_score=min_score, require_quote=True, origin=0.0)
    out: list[dict] = []
    for pick in picks:
        if any(abs(pick["peak"] - other["peak"]) < min_gap for other in out):
            continue
        # "MOMENT" is an internal tag; users understand "talk".
        pick["reason"] = pick.get("reason", "").replace("MOMENT", "talk")
        out.append(pick)
        if len(out) >= max(1, max_clips):
            break
    return out


def cut_clip(video: Path, start: float, end: float, out: Path,
             max_seconds: float = 90.0) -> tuple[Path, float]:
    """Cut [start, end] out of a finished video and re-encode to MP4."""
    start = max(0.0, float(start))
    end = min(float(end), start + max_seconds)
    duration = max(0.5, end - start)
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(["-ss", f"{start:.3f}", "-i", str(video),
             "-t", f"{duration:.3f}",
             "-map", "0:v:0?", "-map", "0:a:0?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(out)], timeout=900)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no clip")
    return out, duration


def safe_name(text: str, limit: int = 48) -> str:
    return (re.sub(r"[^\w-]+", "_", str(text or "")).strip("_")[:limit]
            or "youtube")


def mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _transcript_window(pick: dict) -> str:
    lines = pick.get("win") or []
    return "\n".join(
        f"[{mmss(l['s'])}] {l.get('plain', '')}" for l in lines
    )[-3500:]


# ------------------------------------------------------------------- judging

def _strong_quote(text: str) -> bool:
    text = str(text or "")
    return bool(gta6_pick.EXC.search(text) or gta6_pick.LAUGH.search(text)
                or gta6_pick.MULTI_CUE.search(text)
                or gta6_pick.SUPERLATIVE.search(text))


def judge(pick: dict, meta: dict, opts) -> tuple[bool, dict | None]:
    """AI gate for one pick. Returns (keep, verdict)."""
    verdict = rank_clip(_transcript_window(pick), meta.get("channel", ""),
                        "youtube", wait_s=90, hint=pick.get("quote"))
    if verdict is None:
        log.info("AI unavailable — keeping the quote-gated pick")
        return True, None
    try:
        score = float(verdict.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    rejected = verdict.get("keep") is False or bool(
        score and score < float(getattr(opts, "ai_min_score", 4.5)))
    if not rejected:
        return True, verdict
    floor = float(getattr(opts, "vod_override_min", 7.0))
    if score >= floor and _strong_quote(pick.get("quote", "")):
        log.info("AI override: score %.1f with a strong quote — keeping",
                 score)
        return True, None
    log.info("AI rejected the pick (score %.1f < %.1f) — skipping",
             score, float(getattr(opts, "ai_min_score", 4.5)))
    return False, verdict


def copy_for(pick: dict, verdict: dict | None, meta: dict) -> tuple[str, str, str]:
    """Title/caption/hashtags for one pick, without invented hype."""
    quote = str(pick.get("quote") or "").strip()
    title = caption = hashtags = ""
    if verdict:
        title = str(verdict.get("title") or "").strip()
        caption = str(verdict.get("caption") or "").strip()
        hashtags = str(verdict.get("hashtags") or "").strip()
        if caption and not hype_free(caption, quote):
            log.info("AI caption added hype — using the verbatim quote")
            caption = ""
    if title.upper() in ("WATCH THIS", "LOOK"):
        title = ""
    if not title:
        title = str(pick.get("hook") or "").strip()
    if not title:
        title = " ".join(quote.split()[:8])
    if not title:
        title = str(meta.get("title") or "YouTube moment")
    if not caption:
        source = str(meta.get("title") or "").strip()
        caption = f"{quote} — from {source}" if quote and source else quote
    if not caption:
        caption = str(meta.get("title") or "YouTube moment")
    if not hashtags:
        hashtags = "#YouTube"
    return (" ".join(title.split())[:90],
            " ".join(caption.split())[:300], hashtags)


# ------------------------------------------------------------------ progress

class JobState:
    def __init__(self, dry_run: bool = False) -> None:
        self.meta: dict = {}
        self.clips = 0
        self.last_clip = ""
        self.last_error = ""
        self.dry_run = dry_run
        self.run_id = os.environ.get("GITHUB_RUN_ID", "")

    def write(self, state: str, stage: str, detail: str, percent: float,
              min_gap: float = 10.0) -> None:
        doc = {
            "slot": SLOT,
            "state": state,
            "stage": stage,
            "detail": detail,
            "percent": round(max(0.0, min(100.0, percent)), 1),
            "run_id": self.run_id,
            "updated": datetime.now(timezone.utc).isoformat(),
            "video_id": self.meta.get("id", ""),
            "title": self.meta.get("title", ""),
            "channel": self.meta.get("channel", ""),
            "duration": self.meta.get("duration", 0),
            "clips": self.clips,
            "last_clip": self.last_clip,
            "last_error": self.last_error,
            "dry_run": self.dry_run,
        }
        dashboard.write_progress(SLOT, doc, min_gap=min_gap)


# --------------------------------------------------------------------- flow

def run(url: str, opts, max_clips: int = 4, max_minutes: int = 90,
        keep_temp: bool = False) -> int:
    state = JobState(dry_run=bool(getattr(opts, "no_upload", False)))
    run_dir = Path("workdir") / "vod" / \
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        vid, canonical = normalize_url(url)
        state.write("processing", "probe", "reading video metadata", 2)
        meta = probe(canonical)
        meta["id"] = meta.get("id") or vid
        meta["url"] = canonical
        state.meta = meta
        if meta["duration"] > max(1, max_minutes) * 60:
            raise RuntimeError(
                f"video is {meta['duration'] / 60:.0f} min — the limit is "
                f"{max_minutes} min; use a shorter video")
        log.info("non-realtime: %s (%s, %.0fs) — %s", meta["title"],
                 meta["channel"], meta["duration"], canonical)

        state.write("processing", "download", "downloading the video", 4)
        source = download(canonical, run_dir)
        log.info("non-realtime: downloaded %s (%.1f MB)", source.name,
                 source.stat().st_size / 1e6)

        state.write("processing", "audio", "extracting audio", 8)
        wav = run_dir / "audio.wav"
        extract_audio(source, wav)
        db = rms_db(wav)
        log.info("non-realtime: audio ready (%d s)", len(db))

        state.write("processing", "transcribe", "transcribing", 10)
        model = transcribe.Transcriber(opts.whisper_model)

        def on_transcribe(end: float, total: float) -> None:
            pct = 10 + (55 * min(1.0, end / total) if total else 0)
            state.write("processing", "transcribe",
                        f"transcribing {mmss(end)} / {mmss(total)}", pct)

        segments = model.transcribe(wav, progress=on_transcribe)
        log.info("non-realtime: %d transcript segments", len(segments))
        lines = build_lines(segments, db)
        if len(lines) < 6:
            raise RuntimeError("not enough confident speech to pick a clip")

        state.write("processing", "pick", "finding the best moments", 68)
        max_w = float(getattr(opts, "picker_max_seconds", 32.0))
        picks = pick_moments(
            lines, title=meta["title"], channel=meta["channel"],
            max_clips=max_clips, min_gap=int(getattr(opts, "vod_min_gap", 45)),
            max_w=max_w,
            min_talk=min(14.0, float(getattr(opts, "picker_min_talk", 14.0))))
        if not picks:
            state.write("done", "done",
                        "no clip-worthy moment found", 100, min_gap=0)
            log.info("non-realtime: no clip-worthy moment found")
            return 0
        log.info("non-realtime: %d candidate moment(s): %s", len(picks),
                 ", ".join(f"{mmss(p['s'])}-{mmss(p['e'])} "
                           f"({p['score']:.1f})" for p in picks))

        for i, pick in enumerate(picks):
            base = 72 + (20 * i / max(1, len(picks)))
            state.write("processing", "cut",
                        f"cutting clip {i + 1}/{len(picks)}", base)
            keep, verdict = judge(pick, meta, opts)
            if not keep:
                continue
            title, caption, hashtags = copy_for(pick, verdict, meta)
            start, end = float(pick["s"]), float(pick["e"])
            name = (f"{safe_name(meta['channel'])}_nr{meta['id']}_"
                    f"{int(round(start)):05d}.mp4")
            out = run_dir / name
            out, actual = cut_clip(source, start, end, out,
                                   max_seconds=float(opts.max_clip_seconds))
            entry = {
                "platform": "youtube",
                "channel": meta["channel"],
                "file": name,
                "title": title,
                "caption": caption,
                "hashtags": hashtags,
                "quote": pick.get("quote") or None,
                "score": round(float(pick.get("score") or 0), 2),
                "trigger": (f"non-realtime {mmss(start)} · "
                            f"{meta['title'][:70]}"),
                "stream_t": [round(start, 1), round(end, 1)],
                "duration": round(actual, 1),
                "created": datetime.now(timezone.utc).isoformat(),
                "source": "non-realtime",
                "video_id": meta["id"],
                "video_url": meta["url"],
                "video_title": meta["title"],
            }
            apply_brand(entry, opts.brand_tag)
            if opts.no_upload:
                state.clips += 1
                state.last_clip = name
                log.info("kept %s (%.0fs, non-realtime)", out, actual)
                continue
            try:
                dest = upload.upload_clip(
                    out, safe_name(meta["channel"]), opts.drive_remote,
                    opts.drive_folder)
                dashboard.publish_clip(entry, dest)
            except Exception as exc:
                state.last_error = f"upload failed: {exc}"
                log.error("upload failed: %s (clip kept at %s)", exc, out)
                continue
            state.clips += 1
            state.last_clip = name
            log.info("uploaded %s (%.0fs, non-realtime) → %s",
                     name, actual, dest)

        if state.clips:
            if state.dry_run:
                detail = (f"dry run: {state.clips} clip(s) ready "
                          f"(not uploaded) from {meta['title'][:80]}")
            else:
                detail = (f"published {state.clips} clip(s) from "
                          f"{meta['title'][:80]}")
            state.write("done", "done", detail, 100, min_gap=0)
            log.info("non-realtime: %s %d clip(s)",
                     "dry run kept" if state.dry_run else "published",
                     state.clips)
        else:
            state.write("done", "done",
                        "no clip passed the quality gate", 100, min_gap=0)
            log.info("non-realtime: no clip passed the quality gate")
        if not keep_temp:
            shutil.rmtree(run_dir, ignore_errors=True)
        return 0
    except Exception as exc:
        state.last_error = str(exc)
        state.write("error", "error", str(exc), 100, min_gap=0)
        log.error("non-realtime failed: %s", exc)
        if not keep_temp:
            log.info("working files kept at %s", run_dir)
        return 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Clip one finished YouTube video (non-realtime)")
    ap.add_argument("--url", required=True, help="YouTube video link")
    ap.add_argument("--max-clips", type=int, default=None)
    ap.add_argument("--max-minutes", type=int, default=None)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--keep-temp", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _, opts = load_config()
    if a.no_upload:
        opts.no_upload = True
    clips = a.max_clips if a.max_clips is not None \
        else int(getattr(opts, "vod_max_clips", 4))
    minutes = a.max_minutes if a.max_minutes is not None \
        else int(getattr(opts, "vod_max_minutes", 90))
    clips = max(1, min(10, clips))
    minutes = max(1, min(180, minutes))
    sys.exit(run(a.url, opts, clips, minutes, a.keep_temp))


if __name__ == "__main__":
    main()
