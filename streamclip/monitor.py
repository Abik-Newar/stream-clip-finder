"""Slot nurse: keeps the nine capture-agent slots alive.

Reads dashboard-data/agents.json (edited by the dashboard), then for every
ENABLED slot that has no in-progress capture run, dispatches capture.yml for
that slot. The agent itself picks a live channel of its platform, captures
until the live ends, then switches — or exits fast when nothing is live.
This monitor re-launches the slot on its next 10-minute tick.
"""

import json
import os
import subprocess
import time

SLOT_PLATFORM = {f"{p}-{i}": p for p in ("twitch", "kick", "youtube")
                 for i in (1, 2, 3)}
AGENTS_PATH = "dashboard-data/agents.json"


def _gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def load_slots(repo) -> dict:
    """slot → {"enabled": bool, "platform": str} from agents.json.

    Extra agents added from the dashboard carry their own platform; the
    nine built-in slots fall back to the SLOT_PLATFORM map."""
    r = _gh("api", f"repos/{repo}/contents/{AGENTS_PATH}",
            "-H", "Accept: application/vnd.github.raw")
    if r.returncode != 0:
        print("agents.json unreadable — nothing to do")
        return {}
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        print("agents.json invalid — nothing to do")
        return {}
    return {
        name: {
            "enabled": bool((cfg or {}).get("enabled", False)),
            "platform": (cfg or {}).get("platform") or "",
        }
        for name, cfg in (data.get("slots") or {}).items()
    }


def active_runs():
    """slot → (platform, channel) for running capture agents, or None.

    None means the API call failed. That MUST abort the tick: treating a
    failed listing as "nothing is running" dispatched all nine slots into
    an exhausted quota and produced a storm of 403s."""
    r = _gh("run", "list", "--workflow", "capture.yml", "--limit", "40",
            "--json", "displayTitle,status")
    if r.returncode != 0:
        return None
    out = {}
    try:
        runs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for run in runs:
        if (run.get("status") or "") not in ("in_progress", "queued"):
            continue
        parts = (run.get("displayTitle") or "").split("|")
        if len(parts) >= 4 and parts[0] == "clip-capture":
            out[parts[3]] = (parts[1], parts[2])
    return out


def dispatch(platform, slot):
    r = _gh("workflow", "run", "capture.yml", "-f", f"platform={platform}",
            "-f", f"slot={slot}", "-f", "channel=")
    if r.returncode == 0:
        print(f"  → dispatched {slot} ({platform})")
    else:
        print(f"  ! dispatch {slot} failed: {r.stderr.strip()}")


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        print("(needs GITHUB_REPOSITORY + token — nothing to do)")
        return
    slots = load_slots(repo)
    if not slots:
        return
    running = active_runs()
    if running is None:
        print("run list failed (API budget?) — not dispatching this tick")
        return
    enabled = [s for s, cfg in slots.items() if cfg["enabled"]]
    print(f"slots: {len(enabled)} enabled, {len(running)} agents running")
    # Respect options.max_concurrent (6): extra idle agents only burn the
    # shared API quota polling claims for creators other agents already hold.
    try:
        from .config import load_config
        _, opts = load_config()
        cap = max(1, int(getattr(opts, "max_concurrent", 9)))
    except Exception:
        cap = 9
    free_slots = max(0, cap - len(running))
    print(f"max concurrent: {cap} — room for {free_slots} more")
    dispatched = 0
    for slot in sorted(slots):
        cfg = slots[slot]
        platform = cfg["platform"] or SLOT_PLATFORM.get(slot)
        if not platform:
            print(f"{slot}: unknown slot id — skipped")
            continue
        if not cfg["enabled"]:
            print(f"{slot}: stopped (disabled in dashboard)")
            continue
        if slot in running:
            p, ch = running[slot]
            print(f"{slot}: running on {p}/{ch or 'auto'}")
            continue
        if dispatched >= free_slots:
            print(f"{slot}: waiting (max concurrent {cap} reached)")
            continue
        if dispatched:
            # stagger starts: every agent commits a claim on startup, and a
            # simultaneous burst makes the contents API lose SHA races
            time.sleep(8)
        dispatch(platform, slot)
        dispatched += 1


if __name__ == "__main__":
    main()
