"""Backfill the dashboard directly from the clip files on Drive.

Enumerates gdrive:StreamClips/<channel>/<date>/*.mp4 with `rclone lsf`
(manifests are unreliable: each capture run overwrites the channel
manifest) and:

1. publishes a dashboard row for every clip file that has no row yet,
2. fills in the missing drive_id (share link) for rows that have none.

Usage (workflow or local with rclone configured):
    python -m streamclip.backfill --channel tarik --date all
"""

import argparse
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from . import upload as up
from .dashboard import (API, DATA_DIR, _request, _token, drive_file_id,
                        publish_clip, put_file)

log = logging.getLogger("backfill")

# channel → platform is not stored on Drive; the workflow passes it or we
# default to twitch. Platform is cosmetic (a badge), so a wrong default is
# harmless — override with --platform when needed.


def list_clip_files(channel: str, date: str, remote: str, folder: str) -> dict:
    """Return {file_name: (relative_path, modtime)} for a channel's clips."""
    if not up.rclone_ready():
        raise RuntimeError("rclone/config missing")
    base = f"{remote}:{folder}/{channel}"
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "ls.txt"
        cmd = ["rclone", "lsf", base, "--files-only", "-R",
               "--format", "tp", "--separator", "|", "--include", "*.mp4"]
        with open(listing, "w") as fh:
            subprocess.run(cmd, stdout=fh, check=True, timeout=300)
        files = {}
        for line in listing.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split("|", 1)
            if len(parts) != 2:
                continue
            modtime, relpath = parts
            name = Path(relpath).name
            if date != "all" and not relpath.startswith(f"{date}/"):
                continue
            files[name] = (relpath, modtime)
        return files


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill dashboard rows from Drive")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--date", default="all", help="YYYY-MM-DD filter, or 'all'")
    ap.add_argument("--platform", default="twitch",
                    help="platform badge for new rows (twitch|kick|youtube)")
    ap.add_argument("--remote", default="gdrive")
    ap.add_argument("--folder", default="StreamClips")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    token = _token()
    if not token:
        log.error("no token available")
        return 1
    repo = _repo_env()

    try:
        files = list_clip_files(args.channel, args.date, args.remote, args.folder)
    except Exception as exc:
        log.error("drive listing failed: %s", exc)
        return 1
    log.info("%s clip files on Drive for %s", len(files), args.channel)

    # existing dashboard rows for this channel
    status, entries = _request("GET", f"{API}/repos/{repo}/contents/{DATA_DIR}", token)
    existing = {}
    if status == 200 and isinstance(entries, list):
        for item in entries:
            name = item.get("name", "")
            if not name.endswith(".json"):
                continue
            _, raw = _request(
                "GET", f"{API}/repos/{repo}/contents/{DATA_DIR}/{name}", token,
                accept="application/vnd.github.raw+json")
            try:
                doc = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if doc.get("channel") == args.channel:
                existing[doc.get("file", name[:-5] + ".mp4")] = {
                    "name": name, "sha": item["sha"], "doc": doc}

    published = refreshed = 0
    for file_name, (relpath, modtime) in sorted(files.items()):
        key = Path(file_name).stem
        drive_path = f"{args.remote}:{args.folder}/{args.channel}/{relpath}"
        drive_path = drive_path[: -len(file_name)]  # folder containing the file
        full_file_path = f"{args.remote}:{args.folder}/{args.channel}/{relpath}"
        if key in existing:
            doc = existing[key]["doc"]
            if not doc.get("drive_id"):
                fid = drive_file_id(full_file_path)
                if fid:
                    doc["drive_id"] = fid
                    put_file(repo, f"{DATA_DIR}/{existing[key]['name']}",
                             json.dumps(doc, indent=2, sort_keys=True) + "\n",
                             f"clip: {key} link", token)
                    refreshed += 1
            continue
        entry = {
            "platform": args.platform,
            "channel": args.channel,
            "file": file_name,
            "score": 0,
            "trigger": "backfilled (no live scoring data)",
            "stream_t": None,
            "duration": 0,
            "created": modtime or None,
        }
        publish_clip(entry, drive_path)
        published += 1

    log.info("backfill done: %s new rows, %s rows given a Drive link",
             published, refreshed)
    return 0


def _repo_env() -> str:
    import os
    return os.environ.get("GITHUB_REPOSITORY", "Abik-Newar/stream-clip-finder")


if __name__ == "__main__":
    raise SystemExit(main())
