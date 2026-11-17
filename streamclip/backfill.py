"""Backfill the dashboard from Drive manifests.

For a channel+date, reads gdrive:StreamClips/<channel>/<date>/manifest.jsonl
and publishes a dashboard-data/clips/<clip_key>.json row for every clip that
does not have one yet (uploads happen in upload.py; share links via
rclone link as in dashboard.py).

Usage (workflow or local with rclone configured):
    python -m streamclip.backfill --channel tarik --date 2026-11-17
"""

import argparse
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from . import upload as up
from .dashboard import publish_clip

log = logging.getLogger("backfill")


def fetch_manifest(channel: str, date: str, remote: str, folder: str) -> list[dict]:
    if not up.rclone_ready():
        raise RuntimeError("rclone/config missing")
    src = f"{remote}:{folder}/{channel}/{date}/manifest.jsonl"
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "manifest.jsonl"
        subprocess.run(["rclone", "copyto", src, str(dst)], check=True, timeout=300)
        entries = []
        for line in dst.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill dashboard rows from Drive")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--remote", default="gdrive")
    ap.add_argument("--folder", default="StreamClips")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    try:
        entries = fetch_manifest(args.channel, args.date, args.remote, args.folder)
    except Exception as exc:
        log.error("manifest fetch failed: %s", exc)
        return 1
    log.info("%s existing clips in manifest for %s/%s", len(entries), args.channel, args.date)
    for entry in entries:
        publish_clip(entry, f"{args.remote}:{args.folder}/{args.channel}/{args.date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
