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
import random
import re
import subprocess
import time
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


def _backoff(attempt: int) -> None:
    """Jittered exponential backoff — 9 agents commit claims at once, so a
    flat retry loses the same branch-head race over and over."""
    time.sleep(min(5.0, 0.4 * (2 ** attempt)) + random.random())


def put_file(repo: str, path: str, text: str, message: str, token: str) -> int:
    """Create or update a file via the contents API (sha-aware).

    The first PUT has no SHA (create). If the file already exists it
    answers 422, then we read the blob SHA and update. Concurrent agents
    commit to the same branch, so update PUTs can answer 409 ("is at X but
    expected Y"); re-read the SHA and retry with jitter."""
    url = f"{API}/repos/{repo}/contents/{path}"
    content = base64.b64encode(text.encode()).decode()
    status, _ = _request("PUT", url, token, payload={
        "message": message, "content": content})
    if status in (200, 201):
        return status
    if status != 422 and status != 409:
        return status
    for attempt in range(6):
        status_get, data = _request("GET", url, token)
        if status_get == 200 and isinstance(data, dict) and data.get("sha"):
            status, body = _request("PUT", url, token, payload={
                "message": message, "content": content,
                "sha": data["sha"]})
            if status in (200, 201):
                return status
            if status != 409:
                log.info("dashboard: %s write → HTTP %s %r", path, status, body)
                return status
        _backoff(attempt)
    return status


AGENT_STATE_DIR = "dashboard-data/agent-state"
PROGRESS_DIR = "dashboard-data/live"
_LAST_PROGRESS: dict[str, float] = {}
OVERRIDE_DIR = "dashboard-data/override"

# ETag cache: conditional contents-API GETs that answer 304 are FREE
# (GitHub does not charge them against the primary rate limit). The nine
# agents polled ~10 endpoints every 30 s, which exhausted the installation
# token and made monitor dispatches fail with HTTP 403.
_ETAGS: dict[str, str] = {}
_FILE_DOCS: dict[str, dict] = {}
_MISS_UNTIL: dict[str, float] = {}


def get_file(path: str, token: str) -> tuple[int, dict | None]:
    """Contents-API GET with ETag caching. Returns (status, meta).

    304 → cached meta is returned with status 304 (free). 404s are cached
    briefly so idle override polls don't burn quota either."""
    if time.time() < _MISS_UNTIL.get(path, 0):
        return 404, None
    url = f"{API}/repos/{_repo()}/contents/{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "clipdesk-agent")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    etag = _ETAGS.get(path)
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read()
            new_etag = res.headers.get("ETag")
            if new_etag:
                _ETAGS[path] = new_etag
            _MISS_UNTIL.pop(path, None)
            meta = json.loads(body) if body else None
            if isinstance(meta, dict):
                _FILE_DOCS[path] = meta
            return res.status, meta
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, _FILE_DOCS.get(path)
        if exc.code == 404:
            _MISS_UNTIL[path] = time.time() + 20
        return exc.code, None
    except Exception as exc:
        log.info("dashboard: GET %s failed: %s", path, exc)
        return 0, None


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
        path = f"{OVERRIDE_DIR}/{slot}.json"
        status, meta = get_file(path, token)
        # 304 = unchanged since the last poll → nothing new to consume
        if status != 200 or not isinstance(meta, dict):
            return None
        doc = json.loads(base64.b64decode(meta.get("content", "")).decode())
        if not isinstance(doc, dict):
            return None
        # consume it so the redirect fires exactly once
        url = f"{API}/repos/{_repo()}/contents/{path}"
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


def write_progress(slot: str, doc: dict, min_gap: float = 45.0) -> None:
    """Best-effort live status for an in-flight agent.

    Actions only exposes a run's log after it finishes, so a stuck capture
    used to be invisible. This commits ~one tiny file per minute with the
    counters that matter (segments, candidates, accepted, AI verdict, chat)
    so the board owner can see exactly where a clip is. Never raises."""
    try:
        token = _token()
        if not token or not slot:
            return
        now = time.time()
        if min_gap and now - _LAST_PROGRESS.get(slot, 0) < min_gap:
            return
        _LAST_PROGRESS[slot] = now
        status = put_file(
            _repo(), f"{PROGRESS_DIR}/{slot}.json",
            json.dumps(doc, indent=2, sort_keys=True) + "\n",
            f"live: {slot} status", token)
        if status not in (200, 201):
            log.info("dashboard: progress %s → HTTP %s", slot, status)
    except Exception as exc:
        log.info("dashboard: progress failed: %s", exc)


def _update_claim(slot: str, message: str, make_doc, allow=None) -> bool:
    """Read-modify-write a slot's claim file, retrying lost SHA races.

    The contents API needs the current blob SHA; when another writer wins
    between our GET and PUT it answers 409 and NOTHING is written. That
    used to leave stale claims forever, so each attempt re-reads the SHA."""
    token = _token()
    if not token or not slot:
        return False
    url = f"{API}/repos/{_repo()}/contents/{AGENT_STATE_DIR}/{slot}.json"
    for attempt in range(6):
        status, meta = _request("GET", url, token)
        if status != 200 or not isinstance(meta, dict):
            return False
        cur = json.loads(base64.b64decode(meta.get("content", "")).decode())
        if allow is not None and not allow(cur):
            return False
        doc = make_doc(cur)
        code, body = _request("PUT", url, token, payload={
            "message": message,
            "content": base64.b64encode(
                (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
            ).decode(),
            "sha": meta.get("sha"),
        })
        if code in (200, 201):
            return True
        log.info("dashboard: claim write %s → HTTP %s %r — retrying",
                 slot, code, body)
        _backoff(attempt)
    return False


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def release_agent_state(slot: str, run_id: str = "") -> None:
    """Clear a claim on shutdown — but only if THIS run still owns the file.

    Guards against a cancelled agent wiping the fresh claim of its
    replacement (the concurrency group can overlap for a few seconds)."""
    try:
        def allow(cur):
            return not (run_id and str(cur.get("run_id") or "") != str(run_id))

        def make(cur):
            return {"slot": slot, "platform": cur.get("platform"),
                    "channel": "", "run_id": str(run_id), "since": _now_iso()}

        if _update_claim(slot, f"agent: {slot} released its channel",
                         make, allow):
            log.info("dashboard: %s released its channel", slot)
    except Exception as exc:
        log.info("dashboard: claim release failed: %s", exc)


def reset_agent_state(slot: str, run_id: str = "") -> None:
    """Clear a claim left behind by a PREVIOUS run of this same slot.

    GitHub can force-kill a cancelled job before its shutdown handler runs,
    so the old channel would keep showing (and keep blocking other agents
    for the 5-minute freshness window). A new run of the slot owns that
    file and clears it before picking anything."""
    try:
        def allow(cur):
            old = str(cur.get("run_id") or "")
            return bool(old) and old != str(run_id)

        def make(cur):
            return {"slot": slot, "platform": cur.get("platform"),
                    "channel": "", "run_id": str(run_id), "since": _now_iso()}

        status, meta = _request(
            "GET", f"{API}/repos/{_repo()}/contents/"
            f"{AGENT_STATE_DIR}/{slot}.json", _token() or "")
        old_run = ""
        if status == 200 and isinstance(meta, dict):
            cur = json.loads(base64.b64decode(meta.get("content", "")).decode())
            old_run = str(cur.get("run_id") or "")
        if _update_claim(slot, f"agent: {slot} cleared a stale claim",
                         make, allow):
            log.info("dashboard: cleared stale claim for %s (run %s)",
                     slot, old_run)
    except Exception as exc:
        log.info("dashboard: stale claim reset failed: %s", exc)


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
