"""Config loading for StreamClipFinder."""

import os
from dataclasses import dataclass

import yaml


@dataclass
class Options:
    # Clip geometry
    clip_length: int = 45          # default lookback before the peak (floor)
    tail: int = 15                 # seconds recorded after the peak
    max_clip_seconds: int = 90     # hard cap — "any duration under 90 seconds"
    min_clip_seconds: int = 25     # clips shorter than this are not worth it
    pad_before: float = 3.0        # extra seconds before the detected spike
    segment_time: int = 10         # ffmpeg segment length (s)

    # Detection
    threshold: float = 2.5         # fused hype z-score that a peak must reach
    cooldown: int = 90             # min seconds between clips
    window: int = 60               # scoring window (s)
    baseline: int = 900            # rolling baseline window (s)
    min_uptime: int = 120          # ignore the first N seconds of a capture
    clip_command_trigger: bool = True  # "clip it" chat spam / voice commands
    scene_snap: bool = True        # snap cut points to visual scene cuts

    # Peak selection (v3)
    min_signals: int = 2           # independent signals that must agree
    peak_prominence: float = 1.0   # how far a peak must stand out
    peak_min_distance: float = 30  # seconds between distinct peaks
    pre_pad: float = 3.0           # context seconds before the spike
    reaction_tail: float = 8.0     # post-play reaction seconds after the peak

    # Storage
    keep_buffer: int = 900         # seconds of segments kept on disk

    # Intelligence
    transcription: bool = True
    whisper_model: str = "base"
    ai_ranking: bool = False

    # Google Drive
    drive_remote: str = "gdrive"
    drive_folder: str = "StreamClips"
    no_upload: bool = False

    # Scaling — how many channels may capture at the same time
    max_concurrent: int = 6


def load_config(path: str = "config/channels.yml"):
    """Return (channels, options) from a channels.yml file (defaults if missing)."""
    channels: list = []
    opts = Options()
    p = os.fspath(path)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        channels = data.get("channels") or []
        for key, value in (data.get("options") or {}).items():
            if hasattr(opts, key):
                setattr(opts, key, value)
    # Environment overrides (useful on Actions)
    if os.environ.get("SCF_NO_UPLOAD"):
        opts.no_upload = True
    if os.environ.get("SCF_MODEL"):
        opts.whisper_model = os.environ["SCF_MODEL"]
    if os.environ.get("SCF_THRESHOLD"):
        try:
            opts.threshold = float(os.environ["SCF_THRESHOLD"])
        except ValueError:
            pass
    # Duration policy: clips are always strictly under 90 s
    opts.max_clip_seconds = min(float(opts.max_clip_seconds), 90.0)
    opts.min_clip_seconds = min(float(opts.min_clip_seconds),
                                opts.max_clip_seconds - 5.0)
    opts.clip_length = min(int(opts.clip_length),
                           int(opts.max_clip_seconds) - int(opts.tail))
    return channels, opts
