#!/usr/bin/env bash
# Export YouTube cookies for the YT_COOKIES secret.
#
# Why: YouTube serves GitHub runner IPs a "Sign in to confirm you're not a
# bot" wall, so YouTube channels need cookies. Twitch and Kick need none.
#
# Requires: Chrome (or Brave/Edge) logged into YouTube on this Mac, plus yt-dlp.
#   brew install yt-dlp
#   pip install yt-dlp
set -e
cd "$(dirname "$0")"

REPO="${SCF_REPO:-Abik-Newar/stream-clip-finder}"
BROWSER="${1:-chrome}"
OUT="$(mktemp -t yt_cookies).txt"

echo "Exporting cookies from $BROWSER …"
yt-dlp --cookies-from-browser "$BROWSER" --cookies "$OUT" "https://www.youtube.com/" \
  >/dev/null 2>&1 || true

if [ -s "$OUT" ]; then
  echo "✓ cookies exported ($(wc -c < "$OUT") bytes)"
  if command -v gh >/dev/null 2>&1; then
    base64 -i "$OUT" | tr -d '\n' | gh secret set YT_COOKIES --repo "$REPO" \
      && echo "✓ secret YT_COOKIES set on $REPO"
  else
    echo "gh CLI not found — paste this into Settings > Secrets > YT_COOKIES:"
    base64 -i "$OUT" | tr -d '\n'
    echo
  fi
else
  echo "✗ no cookies found. Log into YouTube in $BROWSER first, then retry."
fi
rm -f "$OUT"
