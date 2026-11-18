#!/usr/bin/env bash
# Export YouTube cookies for the YT_COOKIES secret.
#
# Why: YouTube serves GitHub runner IPs a "Sign in to confirm you're not a
# bot" wall. This is IP-based — no player client bypasses it. Cookies from a
# logged-in browser are the only reliable fix. Twitch and Kick need none.
#
# Usage:
#   ./export_yt_cookies.sh            # read cookies from Chrome
#   ./export_yt_cookies.sh brave      # or brave / edge / firefox / safari
#
# Requires: a browser logged into YouTube + yt-dlp.
#   pip install yt-dlp        (or: brew install yt-dlp)
set -e
cd "$(dirname "$0")"

REPO="${SCF_REPO:-Abik-Newar/stream-clip-finder}"
BROWSER="${1:-chrome}"
OUT="$(mktemp -t yt_cookies).txt"

echo "Exporting YouTube cookies from $BROWSER …"
# Visit a few pages so the auth cookies for youtube.com are all captured.
for url in "https://www.youtube.com/" "https://www.youtube.com/feed/subscriptions"; do
  yt-dlp --cookies-from-browser "$BROWSER" --cookies "$OUT" "$url" \
    >/dev/null 2>&1 || true
done

if [ ! -s "$OUT" ] || ! grep -q "youtube.com" "$OUT"; then
  echo "✗ no YouTube cookies found."
  echo "  1. Open $BROWSER and sign in to https://www.youtube.com"
  echo "  2. Close the browser, then run this script again."
  rm -f "$OUT"
  exit 1
fi

echo "✓ cookies exported ($(wc -c < "$OUT") bytes)"
B64="$(base64 -i "$OUT" | tr -d '\n')"
rm -f "$OUT"

# Prefer the repo's gh wrapper (it forces the right account/token).
GH="./workdir/bin/gha"
[ -x "$GH" ] || GH="$(command -v gh || true)"

if [ -n "$GH" ]; then
  printf '%s' "$B64" | "$GH" secret set YT_COOKIES --repo "$REPO" \
    && echo "✓ secret YT_COOKIES set on $REPO"
  "$GH" secret list --repo "$REPO" 2>/dev/null | grep -q YT_COOKIES \
    && echo "✓ verified: YT_COOKIES is present"
  echo
  echo "Next: restart the YouTube agents (Auto or paste a link) — they can"
  echo "now pass YouTube's bot check."
else
  echo "gh CLI not found — paste this into Settings > Secrets > YT_COOKIES:"
  echo
  printf '%s\n' "$B64"
fi

echo
echo "Note: YouTube cookies expire after a few weeks. If YouTube agents go"
echo "idle again, rerun this script."
