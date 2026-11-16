"""Google Drive upload via rclone (rclone.conf decoded from a repo secret)."""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def rclone_ready() -> bool:
    conf = Path.home() / ".config/rclone/rclone.conf"
    return bool(shutil.which("rclone")) and conf.exists()


def upload_clip(clip_path, channel, remote="gdrive", folder="StreamClips"):
    if not rclone_ready():
        raise RuntimeError("rclone or ~/.config/rclone/rclone.conf missing")
    dest = f"{remote}:{folder}/{channel}/{datetime.now(timezone.utc):%Y-%m-%d}"
    subprocess.run(["rclone", "copy", str(clip_path), dest, "--transfers", "1"],
                   check=True, timeout=1800)
    return dest


def append_manifest(manifest_path, entry: dict):
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def upload_manifest(manifest_path, channel, remote="gdrive", folder="StreamClips"):
    if not rclone_ready():
        return
    dest = f"{remote}:{folder}/{channel}/manifest.jsonl"
    subprocess.run(["rclone", "copyto", str(manifest_path), dest],
                   check=False, timeout=300)
