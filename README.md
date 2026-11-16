# StreamClipFinder 🎬

Real-time "best moment" clipper for live streams. Up to **6 parallel capture
agents** watch Twitch / Kick / YouTube channels while they are live, detect
hype moments from **chat spikes + audio loudness + Whisper transcript
keywords + "clip it" commands**, cut a **25–90 second** clip anchored around
the moment, and upload it straight to your **Google Drive** — a few minutes
after it happened.

```
                    ┌─ agent 1: twitch/x ─┐
live streams ───────┼─ agent 2: kick/y ───┤
(monitor polls      ├─ … up to 6 agents ──┤──► hype scorer ──► trigger
 every 10 min)      └─ agent 6: … ────────┘         │
                                                     ▼
   signals: chat velocity × unique-chatter, dB rise over median,
   transcript hype/laughter/clip-commands  ──►  adaptive 25–90 s span
                                                     │
                              scene-cut snapping ────┤
                                                     ▼
                              ffmpeg cut ──► rclone ──► Google Drive
```

Each live channel = one GitHub Actions job (own 4-core VM), so agents run in
parallel without fighting over CPU. The monitor dispatches captures only while
the pool (`max_concurrent`, default 6) has capacity.

---

## How clip detection works (research summary)

Built on how production clippers work (Eklipse, VoXCluTcH, HotClip, ClipMe)
and broadcast highlight-detection research:

| # | Parameter | Why it matters | How we implement it |
|---|---|---|---|
| 1 | **Chat velocity vs. own baseline** | Raw counts mean nothing across channels; a spike relative to the channel's normal pace is the signal | z-score of msgs/sec against a rolling 15-min baseline (5s buckets) |
| 2 | **Unique-chatter share** | One spammer ≠ a crowd; broad bursts are the real signal (VoXCluTcH: velocity 45% / chatter entropy 35% / cues 20%) | chat signal = velocity × (0.4 + 0.6·unique-chatter share) |
| 3 | **Reaction vernacular density** | "pog", "KEKW", "gg", "W" spam is a denser proxy than plain text | curated cue list adds to the chat signal |
| 4 | **Audio as dB rise over ambient** | Absolute loudness is meaningless (game audio is constant); *sudden rises* mark shouting/laughter/crowd (HotClip: median + 6 dB; EchoMap: ΔdB vs 40 s ambient) | z-score of segment loudness **+ bonus for dB rise above the rolling median** |
| 5 | **Backward anchoring** | Chat reacts 5–10 s *after* the moment (ClipMe: "the step people miss"); clip start must reach back before the spike | `spike_start()` walks buckets back until activity fades; clip start = spike start − padding |
| 6 | **Clip commands** | The streamer saying "clip it/this/that" is the highest-precision signal (Eklipse voice command); chat spamming it too | transcript commands + chat commands both force a trigger (bypass threshold), anchored right before the callout |
| 7 | **Transcript hype & laughter** | Punchlines, "let's go", "hahaha…" correlate with highlights; needed when chat is unavailable | Whisper keywords, weighted; commands score 5× |
| 8 | **Multi-signal corroboration** | Chat spikes happen for raids/sub-trains too; loud ≠ good (doorbell, sponsor read) | weighted fusion: chat 0.45 / audio 0.30 / kw 0.15 / clip-cmd 0.10; signals must align |
| 9 | **Scene cuts as edit anchors** | Clean cuts start/end clips instead of mid-action slices (HotClip cut-density) | cheap scene detection (4 fps) snaps boundaries ±3 s |
| 10 | **Duration policy** | Shorts platforms: 15–90 s; catch the full payoff but stay postable | adaptive span 25–90 s: default 45 s lookback, extends only while the spike is still elevated, hard-capped at 90 s |

What we deliberately skip (and why): in-game UI/kill-feed detection (Eklipse's
game-specific models — per-title training is out of scope), face-cam tracking
(framing concern, not detection), YAMNet laughter classifiers (too heavy for a
free Actions runner — Whisper "haha" + audio rises cover most of it).

## Start / Stop — full control, one click

The system only runs when **you** turn it on:

| Action | How (no terminal needed) | Effect |
|---|---|---|
| **▶ START** | GitHub repo → **Actions** tab → **"stream-clip ▶ START"** → Run workflow | Enables the scheduler, immediately spins up agents for any live channel |
| **■ STOP** | Actions tab → **"stream-clip ■ STOP"** → Run workflow | Disables the scheduler **and kills every running agent** within seconds |
| Per-channel off | Set `enabled: false` on that channel in `config/channels.yml` | Channel skipped, rest keep running |

From your Mac it's the same thing: `./start.sh` and `./stop.sh`.

When stopped, nothing runs and nothing costs anything. When started, agents
run until the stream ends (or you stop them).

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
| `threshold` | 2.5 | z-score trigger; lower = more clips |
| `clip_length` / `tail` | 45 / 15 | default lookback / post-peak tail |
| `max_clip_seconds` | 90 | hard cap (clamped; never posts a >90 s clip) |
| `min_clip_seconds` | 25 | skips micro-triggers |
| `cooldown` | 90 | min seconds between clips |
| `clip_command_trigger` | true | instant trigger on "clip it" |
| `scene_snap` | true | snap boundaries to scene cuts |
| `whisper_model` | base | `tiny` (fastest) → `small` (best, needs CPU) |
| `transcription` | true | set false for lowest CPU usage |
| `max_concurrent` | 6 | parallel capture agents |

The scorer needs ~5 minutes of warm-up (rolling baseline) before the first
trigger, so very short streams won't produce clips.

## Honest limitations

- Clips are **near-real-time**: expect the clip in Drive ~1–2 min after the moment.
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
