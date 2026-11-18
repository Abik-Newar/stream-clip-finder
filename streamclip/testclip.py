"""Record one short test clip from a live channel and publish it to ClipDesk.

This is a pipeline check, not a highlight finder: it grabs the next N seconds
from the live edge, uploads the file through the same Drive path as the agents,
and writes a normal dashboard task. It never touches agent claims and never
stops a running capture.

Usage:
    python -m streamclip.testclip --platform kick --channel abhi9av
    python -m streamclip.testclip --platform youtube --channel @SoulAman
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import dashboard, livecheck, upload


UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _ffmpeg_record(spec, seconds, out, reencode):
    """Record `seconds` from a live reader spec into `out`."""
    kind, url = spec
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if kind == "ffmpeg2":
        video, audio = url
        cmd += ["-user_agent", UA, "-i", video,
                "-user_agent", UA, "-i", audio,
                "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-user_agent", UA, "-i", url]
    cmd += ["-t", str(seconds)]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
            str(out)]
    subprocess.run(cmd, timeout=seconds + 90, check=False)


def _duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=30)
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", required=True,
                    choices=["twitch", "kick", "youtube"])
    ap.add_argument("--channel", required=True,
                    help="Channel/handle (YouTube keeps the @)")
    ap.add_argument("--seconds", type=int, default=20,
                    help="Clip length (default 20)")
    args = ap.parse_args(argv)

    channel = args.channel.strip()
    if not channel:
        print("channel is empty", file=sys.stderr)
        return 1

    live, spec = livecheck.probe(args.platform, channel)
    if not live or not spec:
        print(f"{args.platform}/{channel} is not live or not extractable",
              file=sys.stderr)
        return 1

    safe = re.sub(r"[^\w-]+", "_", channel).strip("_") or "test"
    name = f"{safe}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.mp4"
    out_dir = Path("workdir") / "testclips"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / name

    print(f"recording {args.seconds}s of {args.platform}/{channel} → {out}")
    _ffmpeg_record(spec, args.seconds, out, reencode=False)
    duration = _duration(out)
    if duration < 5:
        print(f"stream copy gave {duration:.1f}s — retrying with re-encode")
        _ffmpeg_record(spec, args.seconds, out, reencode=True)
        duration = _duration(out)
    if duration < 3:
        print(f"recording failed ({duration:.1f}s) — is the stream live?",
              file=sys.stderr)
        return 1

    dest = upload.upload_clip(out, channel)
    entry = {
        "platform": args.platform,
        "channel": channel,
        "file": name,
        "title": f"TEST CLIP — {channel}",
        "caption": (f"Pipeline test: {duration:.0f}s recorded live from "
                    f"{args.platform}/{channel}."),
        "hashtags": "#clips #test #streamer",
        "quote": "Test clip — captured, uploaded and linked from the live stream.",
        "score": 1.0,
        "trigger": "manual pipeline test",
        "stream_t": [0.0, round(duration, 1)],
        "duration": round(duration, 1),
        "created": datetime.now(timezone.utc).isoformat(),
    }
    dashboard.publish_clip(entry, dest)
    print(f"uploaded {name} ({duration:.0f}s) → {dest}")
    print(f"dashboard task: {Path(name).stem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
