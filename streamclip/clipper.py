"""Assemble a clip from the rolling segment buffer with ffmpeg."""

import re
import subprocess
import tempfile
from pathlib import Path


def _segment_range(segments_dir, rel_start, rel_end, seg_time):
    """Segments covering [rel_start, rel_end] (index i covers [(i-1)t, i*t))."""
    segments_dir = Path(segments_dir)
    first = max(1, int(rel_start // seg_time) + 1)
    last = int(rel_end // seg_time) + 1
    files = []
    for i in range(first, last + 1):
        f = segments_dir / f"seg_{i:05d}.ts"
        if f.exists():
            files.append(f)
    return files


def snap_to_cuts(segments_dir, rel_start, rel_end, seg_time,
                 threshold=0.3, max_shift=3.0, max_clip=90.0):
    """Snap clip boundaries to visual scene cuts (edit anchors).

    Research (HotClip/ClipMe): a scene cut makes a clean clip start/end
    instead of cutting mid-action. Runs cheap scene detection (4 fps, tiny
    frames) over just the clip's segments, then snaps start/end to the
    nearest cut within `max_shift` seconds. Never lets the clip exceed
    `max_clip` seconds.
    """
    files = _segment_range(segments_dir, rel_start - 2, rel_end + 2, seg_time)
    if not files or len(files) < 2:
        return rel_start, rel_end

    concat_t0 = (int(files[0].stem.split("_")[1]) - 1) * seg_time
    cuts = []
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lf:
            lf.write("".join(f"file '{f.resolve()}'\n" for f in files))
            list_path = lf.name
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-f", "concat", "-safe", "0",
             "-i", list_path, "-an",
             "-vf", f"fps=4,scale=160:-2,select=gt(scene,{threshold}),showinfo",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
        for m in re.finditer(r"pts_time:(\d+(?:\.\d+)?)", r.stderr or ""):
            cuts.append(concat_t0 + float(m.group(1)))
    except Exception:
        cuts = []
    finally:
        Path(list_path).unlink(missing_ok=True)

    if not cuts:
        return rel_start, rel_end

    def snap(x, lo, hi):
        cands = [c for c in cuts if lo <= c <= hi]
        return min(cands, key=lambda c: abs(c - x)) if cands else x

    rel_start = snap(rel_start, rel_start - 1.0, rel_start + max_shift)
    rel_end = snap(rel_end, rel_end - max_shift, rel_end + max_shift)
    if rel_end - rel_start > max_clip:
        rel_start = rel_end - max_clip
    return rel_start, rel_end


def cut_clip(segments_dir, rel_start, rel_end, seg_time, out_path):
    """Concatenate segments covering stream-relative [rel_start, rel_end] and
    re-encode to MP4. Segment i covers [(i-1)*seg_time, i*seg_time)."""
    files = _segment_range(segments_dir, rel_start, rel_end, seg_time)
    if not files:
        raise FileNotFoundError("no segments found in the clip range")

    list_file = out_path.with_suffix(".txt")
    list_file.write_text("".join(f"file '{f.resolve()}'\n" for f in files))

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
           str(out_path)]
    try:
        subprocess.run(cmd, check=True, timeout=900)
    finally:
        list_file.unlink(missing_ok=True)
    return out_path
