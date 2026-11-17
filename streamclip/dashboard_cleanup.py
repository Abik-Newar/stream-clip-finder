"""Purge dashboard task rows older than the 3-day archive window.

Runs on the monitor schedule. Rows (dashboard-data/clips/*.json) whose
`done_at` is older than 3 days are deleted from the repo permanently.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from .dashboard import API, DATA_DIR, _request, _token  # noqa: F401

log = logging.getLogger("dashboard_cleanup")
DAYS = 3


def _repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "Abik-Newar/stream-clip-finder")


def main() -> int:
    token = _token()
    if not token:
        log.info("cleanup: no token — skipped")
        return 0
    repo = _repo()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    try:
        status, entries = _request(
            "GET", f"{API}/repos/{repo}/contents/{DATA_DIR}", token)
        if status != 200 or not isinstance(entries, list):
            log.info("cleanup: no data dir yet — skipped")
            return 0
    except Exception as exc:
        log.warning("cleanup: list failed: %s", exc)
        return 0

    purged = 0
    for item in entries:
        name = item.get("name", "")
        if not name.endswith(".json"):
            continue
        try:
            _, raw = _request(
                "GET", f"{API}/repos/{repo}/contents/{DATA_DIR}/{name}", token,
                accept="application/vnd.github.raw+json")
            doc = json.loads(raw) if isinstance(raw, str) else raw
            done_at = (doc or {}).get("done_at")
            if not done_at or done_at >= cutoff:
                continue
            _, _ = _request("DELETE",
                            f"{API}/repos/{repo}/contents/{DATA_DIR}/{name}",
                            token, payload={
                                "message": f"purge: {name[:-5]}",
                                "sha": item["sha"],
                            })
            purged += 1
            log.info("cleanup: purged %s", name)
        except Exception as exc:
            log.warning("cleanup: %s failed: %s", name, exc)
    log.info("cleanup: %s purged of %s candidates", purged, len(entries))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
