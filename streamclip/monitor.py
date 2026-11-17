"""Scheduled checker: sees which channels are live and dispatches capture
workflows. Runs inside GitHub Actions (auth via GITHUB_TOKEN) or locally
(auth via `gh auth login`).

Supports running up to `max_concurrent` capture agents at once (default 6):
each live channel gets its own capture job, capped by the pool size.
"""

import os
import subprocess

from . import livecheck
from .config import load_config


def _run_list_json():
    r = subprocess.run(
        ["gh", "run", "list", "--workflow", "capture.yml", "--limit", "60",
         "--json", "displayTitle,status"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return []
    import json
    try:
        return json.loads(r.stdout) or []
    except json.JSONDecodeError:
        return []


def _channel_busy(runs, platform, channel) -> bool:
    """A live capture run whose title claims this channel."""
    marker = f"clip-capture|{platform}|{channel}"
    for run in runs:
        title = (run.get("displayTitle") or "")
        status = (run.get("status") or "")
        if title == marker and status in ("in_progress", "queued"):
            return True
    return False


def _dispatch(platform, channel):
    r = subprocess.run(
        ["gh", "workflow", "run", "capture.yml",
         "-f", f"platform={platform}", "-f", f"channel={channel}"],
        capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  → dispatched capture ({platform}/{channel})")
    else:
        print(f"  ! dispatch failed: {r.stderr.strip()}")


def main():
    channels, opts = load_config(os.environ.get("SCF_CONFIG",
                                                "config/channels.yml"))
    can_dispatch = bool(os.environ.get("GITHUB_REPOSITORY")
                        and os.environ.get("GH_TOKEN"))
    runs = _run_list_json() if can_dispatch else []
    active = sum(1 for r in runs if r.get("status") in ("in_progress", "queued"))
    print(f"capture pool: {active}/{opts.max_concurrent} agents busy")

    for ch in channels:
        platform, name = ch.get("platform"), ch.get("name")
        if not platform or not name:
            continue
        enabled = ch.get("enabled", True)
        try:
            live = livecheck.is_live(platform, name)
        except Exception as exc:
            print(f"{platform}/{name}: check failed ({exc})")
            continue
        print(f"{platform}/{name}: {'LIVE' if live else 'offline'}"
              + ("" if enabled else " (disabled in config)"))
        if not live or not enabled:
            continue
        if not can_dispatch:
            print("  (GH_TOKEN/GITHUB_REPOSITORY missing — cannot auto-dispatch)")
            continue
        if _channel_busy(runs, platform, name):
            print("  capture already running")
            continue
        if active >= opts.max_concurrent:
            print(f"  pool full ({active}/{opts.max_concurrent}) — will retry next tick")
            continue
        _dispatch(platform, name)
        active += 1


if __name__ == "__main__":
    main()
