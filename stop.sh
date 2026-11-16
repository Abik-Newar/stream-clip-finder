#!/usr/bin/env bash
# ■ STOP the clip-finder system (from this Mac)
cd "$(dirname "$0")"
GH=$(command -v gh || echo "workdir/bin/gh")
"$GH" workflow run stop.yml && echo "■ STOP dispatched — scheduler off, running agents killed"
