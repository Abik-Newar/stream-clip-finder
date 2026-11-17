"""Publish pulled clips to the GitHub-Pages dashboard (dashboard/).

After a clip lands in Google Drive we create its public share link
(`rclone link`) and commit one tiny JSON file per clip to
`dashboard-data/clips/` in this repo. The static dashboard reads those
files through the GitHub API; the Drive file itself is the video source.

Everything here is best-effort: a dashboard failure must never interrupt
capture. All functions swallow their errors and just log.
"""

import base64
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("dashboard")

API = "https://api.github.com"
DATA_DIR = "dashboard-data/clips"
DRIVE_LINK_RE = re.compile(r"/d/([A-Za-z0-9_-]{20,})")


def _token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "Abik-Newar/stream-clip-finder")


def _request(method: str, url: str, token: str, payload=None,
             accept: str = "application/vnd.github+json") -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "clipdesk-agent")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as res:
        body = res.read()
    try:
        return res.status, json.loads(body) if body else None
    except ValueError:
        return res.status, body


def drive_file_id(drive_path: str) -> str | None:
    """Create a public share link for the clip and return its Drive file id."""
    try:
        r = subprocess.run(["rclone", "link", drive_path], capture_output=True,
                           text=True, timeout=120, check=True)
        m = DRIVE_LINK_RE.search(r.stdout or "")
        if m:
            return m.group(1)
        log.warning("dashboard: no file id in rclone link output: %r",
                    (r.stdout or "")[:120])
    except Exception as exc:
        log.warning("dashboard: rclone link failed for %s: %s", drive_path, exc)
    return None


def publish_clip(entry: dict, drive_dest: str) -> None:
    """Commit dashboard-data/clips/<clip_key>.json for one uploaded clip."""
    try:
        token = _token()
        if not token:
            log.info("dashboard: no token available — clip not published")
            return
        name = entry.get("file") or ""
        if not name:
            return
        clip_key = Path(name).stem
        drive_path = f"{drive_dest}/{name}"
        doc = {
            "clip_key": clip_key,
            "platform": entry.get("platform"),
            "channel": entry.get("channel"),
            "file": name,
            "drive_id": drive_file_id(drive_path),
            "drive_path": drive_path,
            "score": entry.get("score", 0),
            "trigger": entry.get("trigger", ""),
            "stream_t": entry.get("stream_t"),
            "duration": entry.get("duration", 0),
            "created_at": entry.get("created"),
            "status": "not_started",
            "starred": False,
            "done_at": None,
        }
        repo = _repo()
        url = f"{API}/repos/{repo}/contents/{DATA_DIR}/{clip_key}.json"
        body = base64.b64encode(
            json.dumps(doc, indent=2, sort_keys=True).encode()).decode()
        status, _ = _request("PUT", url, token, payload={
            "message": f"clip: {clip_key}",
            "content": body,
        })
        if status in (200, 201):
            log.info("dashboard: published %s", clip_key)
        else:
            log.warning("dashboard: publish %s → HTTP %s", clip_key, status)
    except Exception as exc:
        log.warning("dashboard: publish failed: %s", exc)
