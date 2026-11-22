"""Config loading for StreamClipFinder."""

import os
from dataclasses import dataclass

import yaml


@dataclass
class Options:
    # Clip geometry — hook-first (viral research: hook in the first 2-3 s,
    # tight 15-60 s cuts; a 45 s setup buries the moment)
    clip_length: int = 25          # default lookback before the peak (floor)
    tail: int = 10                 # seconds recorded after the peak
    max_clip_seconds: int = 60     # platform sweet spot (hard cap stays < 90 s)
    min_clip_seconds: int = 18     # clips shorter than this are not worth it
    pad_before: float = 3.0        # extra seconds before the detected spike
    segment_time: int = 10         # ffmpeg segment length (s)

    # Detection
    threshold: float = 2.2         # fused hype z-score that a peak must reach
    cooldown: int = 30             # min seconds between clips (diversity gap)
    window: int = 60               # scoring window (s)
    baseline: int = 900            # rolling baseline window (s)
    min_uptime: int = 60           # ignore the first N seconds of a capture
    clip_command_trigger: bool = True  # "clip it" chat spam / voice commands
    scene_snap: bool = True        # snap cut points to visual scene cuts

    # Clip finding/choosing — the gta6 picker is the primary engine.
    # "gta6" = tag windows + callbacks + verbatim quote (15–32 s, peak near
    # the end); "legacy" = the v3/v4 z-score scorer and best-of-lull arena.
    picker: str = "gta6"
    picker_min_score: float = 2.5  # gta6 score floor (loud/chat/heat/exc)
    picker_interval: float = 10.0  # seconds between transcript scans
    picker_delay: float = 8.0      # ignore the newest N s (Whisper lag)
    picker_top: int = 2            # candidates per scan
    picker_max_seconds: float = 32.0  # story window (upstream default)
    picker_min_talk: float = 14.0  # seconds of speech a window must hold
    picker_fallback: bool = True   # switch to legacy if the picker errors 3×
    picker_warmup: float = 180.0   # let the AI judge load before emitting picks
    picker_override_min: float = 3.0  # AI score floor to override a rejection

    # Non-realtime (finished YouTube video) clipping
    vod_max_clips: int = 4         # clips published per video
    vod_max_minutes: int = 90      # refuse videos longer than this
    vod_min_gap: int = 45          # seconds between VOD clip peaks
    vod_override_min: float = 7.0  # AI score floor for a VOD override
    vod_model: str = "auto"        # auto: base for Latin scripts, small otherwise
    vod_batch_size: int = 32       # batched inference batch (0 = one-at-a-time)

    # Peak selection (v3)
    min_signals: int = 1           # independent signals that must agree
    peak_prominence: float = 1.0   # how far a peak must stand out
    peak_min_distance: float = 30  # seconds between distinct peaks
    pre_pad: float = 3.0           # context seconds before the spike
    reaction_tail: float = 6.0     # post-play reaction seconds after the peak

    # Quality gates (v4) — only the BEST of a stream reaches the board
    strong_factor: float = 1.2     # score ≥ this × threshold cuts immediately
    grace_period: int = 15         # unbeaten "lull-best" waits this long, then cuts
    max_per_hour: int = 8          # rolling-hour clip quota per channel
    threshold_floor: float = 2.0   # auto-calibration lower bound (quality floor)
    threshold_ceiling: float = 6.0 # auto-calibration upper bound
    overlap_iou: float = 0.3       # max overlap allowed with a recent cut
    quiet_decay: float = 0.97      # threshold decay step during quiet stretches
    duplicate_gap: int = 120       # seconds before the same channel may post again

    # Storage
    keep_buffer: int = 900         # seconds of segments kept on disk

    # Intelligence
    transcription: bool = True
    whisper_model: str = "base"
    ai_ranking: bool = True        # open-source LLM (Ollama) judges + titles clips
    ai_min_score: float = 4.5      # 1-10 virality bar — below this the clip is dropped
    title_template: str = ""       # e.g. "🔥 {channel} — {title}" (empty = raw AI title)
    brand_tag: str = "GTA6"        # forced into every title/caption/hashtag ("" = off)

    # Speed / resilience
    probe_timeout: int = 45        # seconds per channel live-check
    idle_retry_seconds: int = 600  # how long an agent waits for a live channel

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
    if os.environ.get("SCF_PICKER"):
        opts.picker = os.environ["SCF_PICKER"].strip().lower()
    if os.environ.get("SCF_PICKER_MIN_SCORE"):
        try:
            opts.picker_min_score = float(os.environ["SCF_PICKER_MIN_SCORE"])
        except ValueError:
            pass
    for env, attr, cast in (
            ("SCF_VOD_MAX_CLIPS", "vod_max_clips", int),
            ("SCF_VOD_MAX_MINUTES", "vod_max_minutes", int),
            ("SCF_VOD_MIN_GAP", "vod_min_gap", int),
            ("SCF_VOD_BATCH_SIZE", "vod_batch_size", int)):
        if os.environ.get(env):
            try:
                setattr(opts, attr, cast(os.environ[env]))
            except ValueError:
                pass
    if os.environ.get("SCF_VOD_MODEL"):
        opts.vod_model = os.environ["SCF_VOD_MODEL"].strip()
    if os.environ.get("SCF_THRESHOLD"):
        try:
            opts.threshold = float(os.environ["SCF_THRESHOLD"])
        except ValueError:
            pass
    # Duration policy: clips are always strictly under 90 s
    opts.max_clip_seconds = min(float(opts.max_clip_seconds), 90.0)
    opts.picker_max_seconds = min(float(opts.picker_max_seconds),
                                  opts.max_clip_seconds)
    opts.min_clip_seconds = min(float(opts.min_clip_seconds),
                                opts.max_clip_seconds - 5.0)
    opts.clip_length = min(int(opts.clip_length),
                           int(opts.max_clip_seconds) - int(opts.tail))
    return channels, opts
