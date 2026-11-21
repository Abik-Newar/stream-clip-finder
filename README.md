# StreamClipFinder 🎬

Real-time "best moment" clipper for live streams. Up to **6 parallel capture
agents** watch the Twitch / Kick / YouTube channels **you paste on the
dashboard**, detect hype moments from **chat spikes + audio loudness + Whisper
transcript keywords + "clip it" commands**, cut a **25–90 second** clip
anchored around the moment, and upload it straight to your **Google Drive** —
a few minutes after it happened.

```
 paste a link + Watch
        │
        ▼
  ┌─ agent 1: twitch/x ─┐
  ├─ agent 2: kick/y ───┤──► hype scorer ──► trigger
  ├─ … up to 6 agents ──┤                       │
  └─ agent 6: … ────────┘                       ▼
   signals: chat velocity × unique-chatter, dB rise over median,
   transcript hype/laughter/clip-commands  ──►  adaptive 25–90 s span
                                                     │
                              scene-cut snapping ────┤
                                                     ▼
                              ffmpeg cut ──► rclone ──► Google Drive
```

**Manual mode:** nothing is picked automatically. Each Watch click dispatches
one GitHub Actions job for that exact slot + channel (own 4-core VM). When the
live ends the agent stops.

---

## ClipDesk — the browser dashboard (PWA)

A static admin dashboard ships in `dashboard/` and deploys to GitHub Pages
automatically (`pages.yml`). Live at:

**https://abik-newar.github.io/stream-clip-finder/**

Installable as a homescreen app (Android: ⋮ → Install app; iOS: Share → Add
to Home Screen).

```
agent cuts a clip ──► Google Drive (video, permanent)
                  └─► dashboard-data/clips/<clip>.json  (tiny task row, in-repo)
                              │
   browser (ClipDesk) ────────┘  GitHub API = the backend, no server needed
```

| Feature | How it works |
|---|---|
| **Review tasks** | every pulled clip becomes a task: `not started → in progress / reviewing → done` |
| **Star** | star a clip to bookmark it as your best; `★ Starred` filter |
| **Archive lifecycle** | `done` leaves the board and is kept for **3 days** (restorable as *not started*), then the monitor purges the row permanently — the video stays in Drive either way |
| **Watch** | plays from Google Drive in a modal (share link created automatically per clip) |
| **Start/Stop** | one click — dispatches `start.yml` / `stop.yml` via the GitHub API; live agent count shown in the header |
| **Auto-refresh** | polls the repo every 20 s; new clips appear as tasks automatically |

### Admin access (3 admins, no one else)

The dashboard is a static page; **GitHub itself is the gate**:

1. Add your 3 admins as **collaborators** on this repo (Settings → Collaborators).
2. Each admin opens the dashboard and pastes a **fine-grained GitHub token**
   (repo access limited to this repo; permissions: Actions read+write,
   Contents read+write). The login screen has step-by-step instructions.
3. The token is validated against push access and stored **only in that
   admin's browser**. No token, no data — the clip rows are not browsable
   without it.

Notes:
- Clip **videos** live in Drive behind auto-created share links ("anyone with
  the link"). Keep that in mind for private streams; deleting the Drive file
  or unsharing removes playback.
- `backfill.yml` (dispatch with channel + date) re-publishes dashboard rows
  for clips captured before the dashboard existed.
- Everything in `dashboard-data/` is metadata (names, scores, timestamps) —
  no video bytes are stored in the repo.

---

## How clip detection works (research summary)

Built on how production clippers work (Eklipse, VoXCluTcH, HotClip, ClipMe)
and broadcast highlight-detection research:

| # | Parameter | Why it matters | How we implement it |
|---|---|---|---|
| 1 | **Chat velocity vs. own baseline** | Raw counts mean nothing across channels; a spike relative to the channel's normal pace is the signal | per-bucket robust z-score (median + MAD) over a rolling 15-min baseline |
| 2 | **Unique-chatter share** | One spammer ≠ a crowd; broad bursts are the real signal (VoXCluTcH: velocity 45% / chatter entropy 35% / cues 20%) | chat signal = velocity × (0.4 + 0.6·unique-chatter share) |
| 3 | **Reaction vernacular density** | "pog", "KEKW", "gg", "W" spam is a denser proxy than plain text | curated cue list adds to the chat signal |
| 4 | **Audio as dB rise over ambient** | Absolute loudness is meaningless (game audio is constant); *sudden rises* mark shouting/laughter/crowd (HotClip: median + 6 dB; EchoMap: ΔdB vs 40 s ambient) | z-score of segment loudness **+ bonus for dB rise above the rolling median** |
| 5 | **Backward anchoring** | Chat reacts 5–10 s *after* the moment (ClipMe: "the step people miss"); clip start must reach back before the spike | the clip span starts at the elevated-period start, minus 3 s of context |
| 6 | **Clip commands** | The streamer saying "clip it/this/that" is the highest-precision signal (Eklipse voice command); chat spamming it too | transcript commands + chat commands bypass every other rule and win outright |
| 7 | **Transcript hype & laughter** | Punchlines, "let's go", "hahaha…" correlate with highlights; needed when chat is unavailable | Whisper keywords, weighted; commands score 5× |
| 8 | **Multi-signal corroboration** | Chat spikes happen for raids/sub-trains too; loud ≠ good (doorbell, sponsor read) | a candidate needs ≥2 agreeing signals, or one *broad* crowd reaction (many distinct users + cue spam) |
| 9 | **Scene cuts as edit anchors** | Clean cuts start/end clips instead of mid-action slices (HotClip cut-density) | cheap scene detection (4 fps) snaps boundaries ±3 s |
| 10 | **Duration policy** | Shorts platforms: 15–90 s; catch the full payoff but stay postable | adaptive span 25–90 s around the peak, hard-capped at 90 s (measured on the rendered file, not estimated) |

### How a clip is chosen (v3, peak-based)

The previous scorer compared a *smoothed 60 s window* against *sharper 30 s
history samples* — a mismatch that biased every score and made picks feel
arbitrary. v3 instead:

1. Builds a **per-5s-bucket** series for chat, unique-chatter share, cues,
   loudness, keywords and clip commands.
2. Scores each bucket with **modified z-scores (median + MAD)** — robust to
   the heavy skew of chat bursts (standard practice for anomaly detection).
3. **Fuses** the signals: chat 0.45 / audio 0.30 / keywords 0.15 / commands 0.10
   (renormalised when a signal is missing, e.g. no chat on YouTube).
4. Finds **peaks** with key-saddle prominence and a minimum separation, so a
   raid can't trigger a clip and the same moment can't be clipped twice.
5. **Rejects false positives**: one spammer (unique share < 0.25 without cue
   spam) is ignored; a peak needs corroboration.
6. **Grows the span** while the signal stays elevated, then pads 3 s of
   context before and 8 s of reaction after, clamped to 25–90 s.

Each clip's manifest line records *why* it was chosen, e.g.
`"trigger": "chat z12.4/u0.91, audio z6.8, keywords z9.1"`.

### How a clip is chosen now (v5, gta6 picker)

The z-score scorer still computes the signals, but it no longer decides what
gets clipped. The default engine is the **gta6 picker**, vendored from
[nocodehack/gta6-clipper](https://gitlab.com/nocodehack.io/gta6-clipper) (MIT,
v1.0.13) and ported to the live transcript ring in
`streamclip/gta6_pick.py`:

1. Every 10 s it annotates recent transcript lines with the live signals:
   `[LOUD +NdB]` (dB rise over ambient), `[CHAT xN]` (rate vs the previous
   five minutes), `[HEAT 0-100]` (all signals fused into one number).
2. It scores each line like upstream (`loud`, `chat`, `heat`, exclamations,
   laughter, callbacks) and builds one window per peak: **15–32 s**, ~24 s
   target, at least 14 s of speech, with the peak in the **last 60 %**.
3. It looks for **setup→payoff callbacks** up to two hours apart, so a claim
   and its payoff can be tied into one story.
4. A window must contain a **verbatim spoken quote**. No quote = a bluff,
   dropped before the AI judge. The quote is copied exactly — Hindi,
   Hinglish, and every other language are kept in their own script.
5. Windows are deduped by peak (30 s), then `SelectionGate` still applies the
   cooldown, rolling quota, and overlap rules.

The picker's `hook` and `quote` become the card's title/caption; the AI judge
gets the quote as a hint. `GTA6` branding is still applied by `brand.py`,
never by the model. `picker: legacy` in `config/channels.yml` restores the v3
scorer; `SCF_PICKER=legacy` does the same for one run. The port is checked
against the vendored upstream for window, hook, and heat parity in
`tests/test_gta6_pick.py`.

What we deliberately skip (and why): in-game UI/kill-feed detection (Eklipse's
game-specific models — per-title training is out of scope), face-cam tracking
(framing concern, not detection), YAMNet laughter classifiers (too heavy for a
free Actions runner — Whisper "haha" + audio rises cover most of it).

## The agent model (manual watching)

Agents are tied to a *slot*, and each slot watches exactly one link:

```
dashboard: paste link + Watch
   └─ dispatch capture.yml(slot, platform, channel)
        │
        └─ AGENT
             ├─ claims that exact creator (platform-blind)
             ├─ captures it for the WHOLE live
             ├─ live ends → exits (no automatic next channel)
             └─ or press ■ Stop to cancel now
```

- **Claims:** each agent publishes its channel in
  `dashboard-data/agent-state/<slot>.json`; identical creator handles are one
  claim, so `twitch/xqc` and `kick/xqc` can never be captured at the same
  time. Same-millisecond ties are broken deterministically.
- **No auto-selection:** an agent with no link waits for one (or exits); it
  never scans a pool or substitutes another channel.
- **No auto-dispatch:** the scheduled workflow only purges old dashboard
  tasks. Capture jobs exist only because you pressed Watch.
- **Fresh baseline per channel:** switching channels resets the scorer, so
  each stream is judged against its own normal chat pace.

## Start / Stop — one link at a time

Nothing runs until **you** give an agent a link:

| Action | How (no terminal needed) | Effect |
|---|---|---|
| **Watch one agent** | ClipDesk → paste a Twitch / Kick / YouTube live link in the tile → **Watch** | Dispatches one capture job for that exact slot + channel |
| **■ Stop one agent** | ClipDesk → **Stop** on that tile | Cancels that agent's run within seconds |
| **■ Stop everything** | ClipDesk → **Stop all** | Cancels every capture run |
| Freeze the cleaner | Settings → Actions → Variables → `SCF_RUNNING` = `false` | Skips the old-task purge; running agents keep going |

From your Mac it's the same thing: `./start.sh` and `./stop.sh`.

When nothing is watched, nothing runs and nothing costs anything. An agent
stops by itself when its live ends.

## Add your own agents

ClipDesk has 9 built-in agents (3 per platform). Press **+ Add agent**, pick a
platform, and its tile appears — then paste a link and press **Watch**.

`config/channels.yml` is legacy tuning for the old auto mode; manual mode never
reads a channel pool.

**Platform notes (verified from GitHub runner IPs):**
- **Twitch** — works out of the box (streamlink + anonymous chat) ✅
- **Kick** — capture works; chat may be blocked by Kick's API on datacenter IPs
- **YouTube** — needs cookies (run `./export_yt_cookies.sh` once) ⚠️
  The capture workflow installs Deno + the yt-dlp solver scripts automatically.

## Quick start

### 1. Create the repo
Create a GitHub repo and push this folder. **Public is recommended** — standard
runner minutes are free for public repos, and your secrets stay encrypted.

### 2. Prepare the Google Drive remote (once, on your Mac)

```bash
brew install rclone
rclone config          # name it exactly: gdrive  → Google Drive → default answers
rclone mkdir gdrive:StreamClips    # sanity check
```

Then copy the config as base64 into a repo secret:

```bash
base64 -i ~/.config/rclone/rclone.conf | tr -d '\n' | pbcopy
```

### 3. Add repo secrets (Settings → Secrets and variables → Actions)

| Secret | Required | Purpose |
|---|---|---|
| `GDRIVE_RCLONE_CONF` | yes | base64 of your rclone.conf (step 2) |
| `OPENAI_API_KEY` | no | enables `ai_ranking` (clip confirmation + titles) |
| `YT_COOKIES` | no | base64 of a `cookies.txt` for restricted YouTube lives |

### 4. Choose channels (`config/channels.yml`)

Add up to 6+ channels — they run as parallel agents. `max_concurrent` caps the
pool.

### 5. Push, then press ▶ START
After pushing, go to the **Actions** tab and run **"stream-clip ▶ START"** —
the system stays off until you do. Agents then dispatch within ~10 min of a
channel going live; clips land in Drive while the stream is still running.

## Run locally (Mac)

```bash
python3 -m venv .venv && . ./.venv/bin/activate
pip install -r requirements-capture.txt
brew install ffmpeg rclone streamlink yt-dlp

python -m streamclip.capture --platform twitch --channel <name> --no-upload
# clips land in workdir/…
```

## Tuning

| Option | Default | Meaning |
|---|---|---|
| `threshold` | 3.0 | z-score trigger; lower = more clips |
| `clip_length` / `tail` | 25 / 10 | lookback before the peak / post-peak tail |
| `max_clip_seconds` | 60 | hard cap (system clamps under 90 s) |
| `min_clip_seconds` | 18 | skips micro-triggers |
| `cooldown` | 120 | min seconds between clips |
| `grace_period` | 45 | best-of-lull wait before a marginal clip is cut |
| `clip_command_trigger` | true | instant trigger on "clip it" (any language) |
| `scene_snap` | true | snap boundaries to scene cuts |
| `whisper_model` | small | `tiny` (fastest) → `small` (best for Hindi/Hinglish) |
| `brand_tag` | GTA6 | forced into every title + caption + `#hashtag` (empty = off) |
| `transcription` | true | set false for lowest CPU usage |
| `max_concurrent` | 6 | parallel capture agents |

The scorer needs ~3–5 minutes of warm-up (rolling baseline) before the first
trigger, and the local AI judge finishes pulling its model around the same
time. Very short streams may not produce a clip, and candidates from the first
few minutes are the most likely to be dropped.

## Honest limitations

- Clips are **near-real-time**: target **3–5 min** from the moment to the board
  (~1–2.5 min typical). Each clip logs its real `latency: peak→board` breakdown
  (wait / cut / whisper / AI / upload / publish) in the Actions log.
- GitHub Actions jobs cap at **6h**; streams longer than 5.5h restart cleanly.
- Monitor cron can drift a few minutes (GitHub limitation).
- **Concurrency**: free plans allow 20 concurrent standard-runner jobs, so 6
  agents fit comfortably. On a **private** repo those minutes count against
  2000 free min/month (a 4h/day, 6-channel setup needs ~45 000 min/month →
  effectively requires a public repo or paid minutes).
- **YouTube**: live chat isn't read in v1 (audio + transcript still work), and
  YouTube live extraction from datacenter IPs can be flaky — `YT_COOKIES` helps.
- **Kick**: its API sometimes blocks datacenter IPs, so chat may be unavailable
  there; audio + transcript still cover detection.
- Whisper on the runner may fall behind on 60fps streams — use `tiny`, or set
  `transcription: false` (detection still works via chat + audio).
- Clip rights: only post someone else's stream with their permission.

## Roadmap ideas

- Vertical 9:16 export for Shorts/TikTok
- Post-cut quality pass with Higgsfield **Virality Predictor** to rank clips
- Auto-post to YouTube Shorts / TikTok instead of (or in addition to) Drive
- Chat-emote-similarity (40 identical messages = stronger than 40 different)
