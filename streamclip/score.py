"""Sliding-window hype scoring (v2).

Signals are bucketed every 5s over a rolling baseline (default 15 min):

  chat      – messages/sec, weighted by unique-chatter share + reaction cues
  audio     – segment loudness: z-score plus dB rise above rolling median
  kw        – Whisper transcript hype keywords/sec
  clip_req  – "CLIP IT" chat spam and streamer voice commands (very strong)

Design rules taken from production clippers and research:
  - chat is measured relative to the channel's own rolling baseline,
    never raw counts (ClipMe: chat velocity)
  - one spammer is not a crowd: weight by unique-chatter share
    (VoXCluTcH: velocity 45% / chatter entropy 35% / reaction cues 20%)
  - loudness counts as dB rise above the ambient median, not absolute level
    (HotClip: median + 6 dB sustained; VoXCluTcH EchoMap: ΔdB vs 40 s ambient)
  - chat reacts ~5–10 s AFTER the moment, so clip start must reach back
    before the spike (spike_start / ClipMe "backward offset")
  - the streamer saying "clip it" is the highest-precision trigger
    (Eklipse voice command — the clip is built from the seconds before)

Missing signals (no chat, no transcripts) are renormalised away, so every
platform works even without chat.
"""

import math
import statistics
from collections import defaultdict, deque

BUCKET = 5.0          # seconds per event bucket
HIST_STEP = 30.0      # seconds per baseline sample
Z_CLAMP = 8.0

CLIP_COMMANDS = ("clip it", "clip this", "clip that", "clip now")
REACTION_CUES = (
    "pog", "poggers", "pogchamp", "kekw", "lmao", "lol", "rofl", "lets go",
    "let's go", "gg", "clutch", "insane", "hype", "no way", "wtf", "holy",
    "omg", "sheesh", "sheeesh", "ez clap", "clapped", "diff", "caught",
    "ayo", " Based", "W in the chat",
)


class Scorer:
    def __init__(self, window=60, baseline=900):
        self.window = window
        self.baseline = baseline
        self.msgs = defaultdict(float)       # bucket -> messages
        self.users = defaultdict(set)        # bucket -> {username}
        self.cues = defaultdict(float)       # bucket -> reaction-cue hits
        self.clip_req = defaultdict(float)   # bucket -> chat clip commands
        self.voice_cmd = defaultdict(float)  # bucket -> transcript commands
        self.kw = defaultdict(float)         # bucket -> transcript keyword weight
        self.audio = deque()                 # (t, mean_db, max_db)

    # ---------------------------------------------------------------- events

    def add_chat(self, t, user, text):
        b = int(t // BUCKET)
        self.msgs[b] += 1
        self.users[b].add(user or "anon")
        low = (text or "").lower()
        if any(c in low for c in CLIP_COMMANDS):
            self.clip_req[b] += 1
        hits = sum(1 for c in REACTION_CUES if c in low)
        if hits:
            self.cues[b] += hits

    def add_audio(self, t, mean_db, max_db=None):
        self.audio.append((t, mean_db, max_db if max_db is not None else mean_db))
        while self.audio and self.audio[0][0] < t - self.baseline:
            self.audio.popleft()

    def add_keywords(self, t, hits, command=False):
        if command:
            self.voice_cmd[int(t // BUCKET)] += 1
        if hits > 0:
            self.kw[int(t // BUCKET)] += hits

    # ---------------------------------------------------------------- score

    def evaluate(self, now):
        self._prune(now)
        w0 = now - self.window

        # chat: effective rate = velocity x unique-chatter share + reaction cues
        msgs_sum = self._sum(self.msgs, w0, now)
        uniq = set()
        for b in self._buckets(w0, now):
            uniq |= self.users.get(b, set())
        uniq_ratio = (len(uniq) / msgs_sum) if msgs_sum else 0.0
        cue_rate = self._sum(self.cues, w0, now) / self.window
        chat_x = (msgs_sum / self.window) * (0.4 + 0.6 * uniq_ratio) + 0.5 * cue_rate
        clip_x = self._sum(self.clip_req, w0, now) / self.window
        kw_x = self._sum(self.kw, w0, now) / self.window

        audio_vals = [db for t, db, _mx in self.audio if w0 <= t < now]
        audio_x = sum(audio_vals) / len(audio_vals) if audio_vals else -91.0

        # rolling history samples
        chat_hist, clip_hist, kw_hist, audio_hist = [], [], [], []
        t = now - self.baseline
        while t < w0:
            chat_hist.append(self._chat_signal(t, t + HIST_STEP))
            clip_hist.append(self._sum(self.clip_req, t, t + HIST_STEP) / HIST_STEP)
            kw_hist.append(self._sum(self.kw, t, t + HIST_STEP) / HIST_STEP)
            av = [db for tt, db, _mx in self.audio if t <= tt < t + HIST_STEP]
            audio_hist.append(sum(av) / len(av) if av else None)
            t += HIST_STEP
        audio_hist_v = [v for v in audio_hist if v is not None]

        chat_z = self._z(chat_hist, chat_x)
        clip_z = self._z(clip_hist, clip_x)
        kw_z = self._z(kw_hist, kw_x)
        audio_z = self._z(audio_hist_v, audio_x)
        # dB rise above the ambient median (HotClip/EchoMap style), 0..2 bonus
        if len(audio_hist_v) >= 5:
            med_db = statistics.median(audio_hist_v)
            db_rise = max(0.0, min(2.0, (audio_x - med_db) / 6.0))
        else:
            db_rise = 0.0
        audio_component = audio_z + db_rise

        parts = {"chat": chat_z, "clip_req": clip_z, "kw": kw_z,
                 "audio": audio_component}
        weights = {"chat": 0.45 if chat_hist else 0.0,
                   "clip_req": 0.10 if clip_hist else 0.0,
                   "kw": 0.15 if kw_hist else 0.0,
                   "audio": 0.30 if audio_hist_v else 0.0}
        total = sum(weights.values()) or 1.0
        score = sum(parts[k] * weights[k] for k in parts) / total

        detail = {"chat_z": round(chat_z, 2), "audio_z": round(audio_z, 2),
                  "kw_z": round(kw_z, 2), "clip_z": round(clip_z, 2),
                  "uniq_ratio": round(uniq_ratio, 2), "db_rise": round(db_rise, 2),
                  "chat_rate": round(msgs_sum / self.window, 2)}
        return score, detail

    # ------------------------------------------------------- spike geometry

    def spike_start(self, now, max_back=None):
        """Walk back from `now` through buckets while per-bucket activity is
        elevated above its median. Returns the elevated period's start time
        (wall clock) or None. This implements the 'backward offset': clip
        starts BEFORE the chat/audio spike, since chat reacts late."""
        max_back = max_back or self.baseline
        med_db = (statistics.median([db for _t, db, _mx in self.audio])
                  if len(self.audio) >= 5 else -91.0)

        db_by_bucket = defaultdict(list)
        for t, db, _mx in self.audio:
            db_by_bucket[int(t // BUCKET)].append(db)

        def activity(b):
            a = (self.msgs.get(b, 0.0) + self.cues.get(b, 0.0)
                 + self.kw.get(b, 0.0) + self.clip_req.get(b, 0.0) * 3.0)
            dbs = db_by_bucket.get(b)
            if dbs:
                a += max(0.0, (sum(dbs) / len(dbs) - med_db) / 1.5)
            return a

        all_buckets = set(db_by_bucket) | set(self.msgs) | set(self.cues) \
            | set(self.kw) | set(self.clip_req) | set(self.voice_cmd)
        activities = [activity(b) for b in all_buckets]
        med_act = statistics.median(activities) if len(activities) >= 6 else 0.0

        b = int(now // BUCKET) - 1
        floor_b = int((now - max_back) // BUCKET)
        quiet = 0
        start = None
        while b >= floor_b:
            act = activity(b)
            if act > max(0.5, med_act):
                start = b * BUCKET
                quiet = 0
            else:
                quiet += 1
                if quiet >= 2:
                    break
            b -= 1
        return start

    def recent_clip_commands(self, now, chat_secs=12, voice_secs=45):
        """Chat-spam commands (near-instant) in the last `chat_secs`, and
        transcript voice commands in the last `voice_secs` — voice commands
        are only *learned* when their segment gets transcribed 10–40 s after
        they were spoken, so they need the longer window."""
        chat_lo = int((now - chat_secs) // BUCKET)
        voice_lo = int((now - voice_secs) // BUCKET)
        hi = int(now // BUCKET)
        total = sum(v for k, v in self.clip_req.items() if chat_lo <= k <= hi)
        total += sum(v for k, v in self.voice_cmd.items() if voice_lo <= k <= hi)
        return total

    # ------------------------------------------------------------- internals

    def _chat_signal(self, t0, t1):
        msgs = self._sum(self.msgs, t0, t1)
        if msgs == 0:
            return 0.0
        uniq = set()
        for b in self._buckets(t0, t1):
            uniq |= self.users.get(b, set())
        cues = self._sum(self.cues, t0, t1)
        return (msgs / (t1 - t0)) * (0.4 + 0.6 * len(uniq) / msgs) \
            + 0.5 * cues / (t1 - t0)

    def _buckets(self, t0, t1):
        return range(int(t0 // BUCKET), int(t1 // BUCKET) + 1)

    def _prune(self, t):
        cut = int((t - self.baseline) // BUCKET)
        for d in (self.msgs, self.cues, self.clip_req, self.voice_cmd, self.kw):
            for k in [k for k in d if k < cut]:
                del d[k]
        for k in [k for k in self.users if k < cut]:
            del self.users[k]

    @staticmethod
    def _sum(d, t0, t1):
        return sum(v for k, v in d.items() if t0 <= k * BUCKET < t1)

    @staticmethod
    def _z(series, x):
        if len(series) < 12:
            return 0.0
        mu = sum(series) / len(series)
        var = sum((v - mu) ** 2 for v in series) / len(series)
        sd = math.sqrt(var) or 1e-6
        return max(-Z_CLAMP, min(Z_CLAMP, (x - mu) / max(sd, 1e-6)))
