#!/usr/bin/env bash
# ▶ START the clip-finder system (from this Mac)
cd "$(dirname "$0")"
GH=$(command -v gh || echo "workdir/bin/gh")
"$GH" workflow run start.yml && echo "▶ START dispatched — agents spin up for live channels (Actions tab)"
