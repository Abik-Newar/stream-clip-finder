"""Backfill the dashboard from Drive manifests.

Reads gdrive:StreamClips/<channel>/manifest.jsonl (the manifest lives at the
channel root) and publishes a dashboard-data/clips/<clip_key>.json row for
every clip that does not have one yet. Clips themselves live in a date
subfolder derived from the entry's `created` timestamp.

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


def fetch_manifest(channel: str, remote: str, folder: str) -> list[dict]:
    if not up.rclone_ready():
        raise RuntimeError("rclone/config missing")
    src = f"{remote}:{folder}/{channel}/manifest.jsonl"
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
    ap.add_argument("--date", default="all", help="YYYY-MM-DD filter, or 'all'")
    ap.add_argument("--remote", default="gdrive")
    ap.add_argument("--folder", default="StreamClips")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    try:
        entries = fetch_manifest(args.channel, args.remote, args.folder)
    except Exception as exc:
        log.error("manifest fetch failed: %s", exc)
        return 1

    def entry_date(entry):
        return (entry.get("created") or "")[:10]

    picked = [e for e in entries
              if args.date == "all" or entry_date(e) == args.date]
    log.info("%s of %s manifest entries match date filter", len(picked), len(entries))
    for entry in picked:
        date = entry_date(entry) or "unknown-date"
        publish_clip(entry, f"{args.remote}:{args.folder}/{args.channel}/{date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
