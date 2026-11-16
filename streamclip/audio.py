"""Per-segment loudness via ffmpeg volumedetect."""

import re
import subprocess

_MEAN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
_MAX = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")
_SILENCE = -91.0


def loudness(path):
    """Return (mean_db, max_db) or None if the segment has no audio."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    out = r.stderr or ""
    mean = _MEAN.search(out)
    if not mean:
        return None
    mx = _MAX.search(out)
    return float(mean.group(1)), (float(mx.group(1)) if mx else _SILENCE)
