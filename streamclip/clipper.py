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
    list_path = None
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
        if list_path:
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


def probe_duration(path):
    """Actual duration of a media file in seconds (0.0 if unknown)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    # ffprobe may be missing — fall back to ffmpeg's duration line
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                            "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def cut_clip(segments_dir, rel_start, rel_end, seg_time, out_path,
             max_duration=90.0):
    """Cut exactly [rel_start, rel_end] (stream-relative) and re-encode to MP4.

    Segment i covers [(i-1)*seg_time, i*seg_time). Exact boundaries are
    applied with concat-demuxer inpoint/outpoint so the result matches the
    requested span (no 10 s segment slack), and `max_duration` is enforced
    afterwards so a clip can never exceed the hard cap.
    """
    files = _segment_range(segments_dir, rel_start, rel_end, seg_time)
    if not files:
        raise FileNotFoundError("no segments found in the clip range")

    out_path = Path(out_path)
    list_file = out_path.with_suffix(".txt")
    lines = []
    for f in files:
        fs = (int(f.stem.split("_")[1]) - 1) * seg_time
        lines.append(f"file '{f.resolve()}'")
        if fs < rel_start:                      # trim the head of the first file
            lines.append(f"inpoint {rel_start - fs:.3f}")
        if fs + seg_time > rel_end:            # trim the tail of the last file
            lines.append(f"outpoint {min(seg_time, max(0.0, rel_end - fs) + 0.4):.3f}")
    list_file.write_text("\n".join(lines) + "\n")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
           str(out_path)]
    try:
        subprocess.run(cmd, check=True, timeout=900)
    finally:
        list_file.unlink(missing_ok=True)

    # Hard duration guarantee
    dur = probe_duration(out_path)
    if dur > max_duration + 1.0:
        trimmed = out_path.with_name(out_path.stem + "_t.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(out_path), "-t", f"{max_duration:.3f}",
             "-c", "copy", "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart", str(trimmed)],
            check=True, timeout=300)
        trimmed.replace(out_path)
        dur = probe_duration(out_path)
    return out_path, dur
