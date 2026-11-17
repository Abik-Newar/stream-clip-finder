"""Google Drive upload via rclone (rclone.conf decoded from a repo secret)."""

import json
import shutil
import subprocess
import tempfile
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
    """Merge-upload the manifest: concurrent runs and repeat uploads must not
    clobber each other's rows (merge by clip key on the remote copy)."""
    if not rclone_ready():
        return
    dest = f"{remote}:{folder}/{channel}/manifest.jsonl"
    merged = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        remote_copy = tmpdir / "remote.jsonl"
        r = subprocess.run(["rclone", "copyto", dest, str(remote_copy)],
                           capture_output=True, timeout=300)
        if r.returncode == 0 and remote_copy.exists():
            for line in remote_copy.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    key = Path(row.get("file", line[:16])).stem
                    merged[key] = row
                except (ValueError, IndexError):
                    continue
        try:
            for line in Path(manifest_path).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    merged[Path(row.get("file", line[:16])).stem] = row
        except OSError:
            pass
        out = tmpdir / "merged.jsonl"
        out.write_text(
            "\n".join(json.dumps(merged[k], ensure_ascii=False)
                      for k in sorted(merged)) + "\n", encoding="utf-8")
        subprocess.run(["rclone", "copyto", str(out), dest],
                       check=False, timeout=300)
