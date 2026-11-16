"""Check whether a channel is live and how to capture it.

Everything is normalised to one of three capture modes:
  ("streamlink", url)        — streamlink reads the playlist (handles ads +
                               rotation on Twitch/Kick) and prints a
                               continuous MPEG-TS stream
  ("ffmpeg2", (vurl, aurl))  — ffmpeg merges two split HLS tracks (YouTube)
  ("url", url)               — ffmpeg reads a single muxed manifest URL

Live checks:
  twitch  : streamlink (no API keys needed)
  kick    : streamlink, falling back to yt-dlp
  youtube : yt-dlp (streamlink dropped YouTube support years ago)
"""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path


def streamlink_json(url):
    """Return the best stream info dict, or None if offline/absent."""
    if not shutil.which("streamlink"):
        return None
    try:
        r = subprocess.run(["streamlink", "--json", url],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return (data.get("streams") or {}).get("best")


def _yt_dlp_cmd():
    cmd = ["yt-dlp"]
    cookies = os.environ.get("YT_COOKIES")
    if cookies:
        try:
            path = Path.home() / ".scf_yt_cookies.txt"
            path.write_bytes(base64.b64decode(cookies))
            cmd += ["--cookies", str(path)]
        except Exception:
            pass
    return cmd


def yt_dlp_urls(url):
    """Media URLs via yt-dlp.

    Returns a list: [muxed_url] for single-file streams, or
    [video_url, audio_url] when the live only offers split HLS tracks
    (typical for YouTube lives)."""
    try:
        r = subprocess.run(_yt_dlp_cmd() + ["-g", "-f", "bv*+ba/b", url],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return [l for l in r.stdout.splitlines() if l.startswith("http")] or None


def probe(platform: str, channel: str):
    """Return (is_live, reader_spec). reader_spec = (kind, url) or (None, None)."""
    if platform == "twitch":
        s = streamlink_json(f"https://twitch.tv/{channel}")
        return (True, ("streamlink", s["url"])) if s else (False, None)

    if platform == "kick":
        s = streamlink_json(f"https://kick.com/{channel}")
        if s:
            return True, ("streamlink", s["url"])
        lines = yt_dlp_urls(f"https://kick.com/{channel}")
        if not lines:
            return False, None
        if len(lines) >= 2:
            return True, ("ffmpeg2", (lines[0], lines[1]))
        return True, ("url", lines[0])

    if platform == "youtube":
        if channel.startswith("@"):
            page = f"https://www.youtube.com/{channel}/live"
        elif channel.startswith("UC"):
            page = f"https://www.youtube.com/channel/{channel}/live"
        else:
            page = f"https://www.youtube.com/{channel}/live"
        lines = yt_dlp_urls(page)
        if not lines:
            return False, None
        if len(lines) >= 2:
            return True, ("ffmpeg2", (lines[0], lines[1]))
        return True, ("url", lines[0])

    raise ValueError(f"unknown platform: {platform}")


def is_live(platform: str, channel: str) -> bool:
    live, _ = probe(platform, channel)
    return live
