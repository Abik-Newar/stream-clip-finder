# Vendored: nocodehack / gta6-clipper

- Source: https://gitlab.com/nocodehack.io/gta6-clipper
- Commit: `9a96d6f3d71010a94b482eb1cbde1caf9d5d72ce`
- Version: `1.0.13` (2026-11-20)
- License: MIT (see `LICENSE`)
- Vendored files: `nopick.py`, `callbacks.py`, `chat.py`, plus the upstream
  `README.md`, `SKILL.md`, `INSTALL.md`, `NOTICE.md`, and `version.json` for
  provenance and attribution.

We vendor only the pure-Python selection code. The renderer (`clip.py`,
`batch.py`, `formats.py`), the downloaders, and the vision/audio extras are
not used by StreamClipFinder: it already owns capture, rendering, upload, and
the dashboard. `batch.py`, `formats.py`, `parts.py`, and `lite.py` are
included **only** because `nopick.py` imports them at module load; they let
the parity tests import the upstream module unmodified. Nothing in
`streamclip/` imports those four files.

`streamclip/gta6_pick.py` is an adapted port of `nopick.py` and
`callbacks.py` with the same scoring/window/callback rules, changed only where
the upstream assumes files on disk and a finished VOD:

- it consumes in-memory transcript lines from the live ring buffer,
- it derives `[LOUD]` / `[CHAT]` tags from StreamClipFinder's live scorer,
- it requires a verbatim quote (so bluff windows are rejected),
- it supports a configurable maximum window (default 32 s, hard-capped at 90 s).

Upstream remains the source of truth for the algorithm. Keep this directory
in sync when updating the vendored version.
