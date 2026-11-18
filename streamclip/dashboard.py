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
DRIVE_LINK_RE = re.compile(r"(?:/d/|[?&]id=)([A-Za-z0-9_-]{20,})")


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
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            body = res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:200]
    try:
        return res.status, json.loads(body) if body else None
    except ValueError:
        return res.status, body


def put_file(repo: str, path: str, text: str, message: str, token: str) -> int:
    """Create or update a file via the contents API (sha-aware)."""
    url = f"{API}/repos/{repo}/contents/{path}"
    content = base64.b64encode(text.encode()).decode()
    status, _ = _request("PUT", url, token, payload={
        "message": message, "content": content})
    if status in (200, 201):
        return status
    # 422 = already exists → fetch its sha and update instead
    status_get, data = _request("GET", url, token)
    if status_get == 200 and isinstance(data, dict) and data.get("sha"):
        status, _ = _request("PUT", url, token, payload={
            "message": message, "content": content, "sha": data["sha"]})
    return status


AGENT_STATE_DIR = "dashboard-data/agent-state"
OVERRIDE_DIR = "dashboard-data/override"


def take_override(slot: str):
    """Return the dashboard's pending redirect for this slot, then consume it.

    The dashboard writes {"platform", "channel", "ts"} when the user pastes a
    link + Watch on a running agent. Polling it lets a live agent switch
    channels in ~20 s instead of a full workflow restart (~1 min).
    Best-effort: returns None for no file / any error."""
    try:
        token = _token()
        if not token or not slot:
            return None
        url = f"{API}/repos/{_repo()}/contents/{OVERRIDE_DIR}/{slot}.json"
        status, meta = _request("GET", url, token)
        if status != 200 or not isinstance(meta, dict):
            return None
        doc = json.loads(base64.b64decode(meta.get("content", "")).decode())
        if not isinstance(doc, dict):
            return None
        # consume it so the redirect fires exactly once
        _request("DELETE", url, token, payload={
            "message": f"override: {slot} consumed",
            "sha": meta.get("sha"),
        })
        return doc
    except Exception as exc:
        log.info("dashboard: override check failed: %s", exc)
        return None


def publish_agent_state(slot: str, platform: str, channel: str,
                        run_id: str = "") -> str:
    """Commit dashboard-data/agent-state/<slot>.json — the agent's live
    channel. Doubles as the claim system (run-title renames are not
    supported by the API) and as the dashboard's agent visibility.
    Returns the claim's UTC timestamp (or "" when unavailable)."""
    try:
        token = _token()
        if not token:
            return ""
        from datetime import datetime, timezone
        since = datetime.now(timezone.utc).isoformat()
        doc = {
            "slot": slot,
            "platform": platform,
            "channel": channel,
            "run_id": run_id,
            "since": since,
        }
        status = put_file(_repo(), f"{AGENT_STATE_DIR}/{slot}.json",
                          json.dumps(doc, indent=2, sort_keys=True) + "\n",
                          f"agent: {slot} → {platform}/{channel}", token)
        if status not in (200, 201):
            log.warning("dashboard: agent state %s → HTTP %s", slot, status)
            return ""
        return since
    except Exception as exc:
        log.warning("dashboard: agent state failed: %s", exc)
        return ""


def release_agent_state(slot: str, run_id: str = "") -> None:
    """Clear a claim on shutdown — but only if THIS run still owns the file.

    Guards against a cancelled agent wiping the fresh claim of its
    replacement (the concurrency group can overlap for a few seconds)."""
    try:
        token = _token()
        if not token or not slot:
            return
        url = f"{API}/repos/{_repo()}/contents/{AGENT_STATE_DIR}/{slot}.json"
        status, meta = _request("GET", url, token)
        if status != 200 or not isinstance(meta, dict):
            return
        doc = json.loads(base64.b64decode(meta.get("content", "")).decode())
        if run_id and str(doc.get("run_id") or "") != str(run_id):
            return                          # a newer run owns the slot now
        from datetime import datetime, timezone
        new = {
            "slot": slot,
            "platform": doc.get("platform"),
            "channel": "",
            "run_id": str(run_id),
            "since": datetime.now(timezone.utc).isoformat(),
        }
        _request("PUT", url, token, payload={
            "message": f"agent: {slot} released its channel",
            "content": base64.b64encode(
                (json.dumps(new, indent=2, sort_keys=True) + "\n").encode()
            ).decode(),
            "sha": meta.get("sha"),
        })
    except Exception as exc:
        log.info("dashboard: claim release failed: %s", exc)


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
            "title": entry.get("title"),
            "caption": entry.get("caption"),
            "hashtags": entry.get("hashtags"),
            "quote": entry.get("quote"),
            "stream_t": entry.get("stream_t"),
            "duration": entry.get("duration", 0),
            "created_at": entry.get("created"),
            "status": "not_started",
            "starred": False,
            "done_at": None,
        }
        repo = _repo()
        status = put_file(repo, f"{DATA_DIR}/{clip_key}.json",
                          json.dumps(doc, indent=2, sort_keys=True) + "\n",
                          f"clip: {clip_key}", token)
        if status in (200, 201):
            log.info("dashboard: published %s", clip_key)
        else:
            log.warning("dashboard: publish %s → HTTP %s", clip_key, status)
    except Exception as exc:
        log.warning("dashboard: publish failed: %s", exc)
