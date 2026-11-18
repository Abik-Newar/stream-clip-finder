"""Peak-based highlight detection (v3).

Why v3: v2 scored an *aggregate* window (60 s) against history buckets of a
different length (30 s) — a smoothed current value compared to sharper history
samples. That mismatch biased every score and made clip picks feel arbitrary.

v3 works on a per-bucket time series and finds real *peaks*:

  1. Per-5s-bucket features: chat volume, unique-chatter share, reaction cues,
     loudness, transcript keywords, clip commands.
  2. Robust z-scores via median + MAD (modified z-score, 0.6745·(x−med)/MAD).
     Chat spikes are heavily skewed, so median/MAD is far more reliable than
     mean/std (standard anomaly-detection practice).
  3. Weighted fusion: chat 0.45 / audio 0.30 / keywords 0.15 / commands 0.10,
     renormalised when a signal is unavailable (e.g. no chat on YouTube).
  4. Peak detection with prominence + minimum distance, so a single raid or a
     single screaming viewer can't trigger a clip, and the same moment can't
     be clipped twice.
  5. Corroboration: a candidate needs ≥2 independent signals to agree
     (chat + audio + keywords + commands), unless the streamer explicitly
     asked for the clip. This is what separates a real moment from a raid,
     a sub train or a sponsor read.
  6. Spans are grown around the peak while the signal stays elevated, then
     padded with context before and the post-play reaction after — clips are
     cut around the moment, not at it.

Reference behaviour from production clippers: chat reacts 5–10 s AFTER the
moment (so clip start reaches back), the payoff/reaction comes after the peak
(so clip end extends forward), and loudness counts as a dB *rise* over the
ambient median rather than absolute level.
"""

import statistics
from collections import defaultdict, deque

BUCKET = 5.0          # seconds per feature bucket
Z_CLAMP = 25.0        # high enough that real bursts stay distinguishable
MAD_SCALE = 0.6745    # modified z-score constant (1/Φ⁻¹(0.75))

CLIP_COMMANDS = ("clip it", "clip this", "clip that", "clip now")
REACTION_CUES = (
    "pog", "poggers", "pogchamp", "kekw", "lmao", "lol", "rofl", "lets go",
    "let's go", "gg", "clutch", "insane", "hype", "no way", "wtf", "holy",
    "omg", "sheesh", "sheeesh", "ez clap", "clapped", "diff", "caught",
    "ayo", "W in the chat",
    # Hinglish chat — Indian Kick/Twitch chats react in Roman Hindi
    "pagal", "paagal", "gajab", "ghazab", "zabardast", "jabardast",
    "kya baat", "khatam", "arre", "oye", "bhai", "haha",
    # Devanagari chat
    "पागल", "गजब", "ज़बरदस्त", "जबरदस्त", "क्या बात", "खत्म", "अरे",
    "ओए", "भाई", "हाहा",
    # emoji bursts only count when repeated — one emoji is just normal chat
    "🔥🔥", "😂😂", "🤣🤣",
)

# Signal weights for the fused hype score.
# Research note (Autohighlight/LoL, Park et al. 2022): audio is the single
# strongest reaction modality, chat is the densest crowd signal.
W_CHAT, W_AUDIO, W_KW, W_CMD = 0.42, 0.33, 0.15, 0.10


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
        self.start_t = None                  # first observed event
        self.cut_peaks = deque(maxlen=64)    # peak times already clipped

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
        self._touch(t)

    def add_audio(self, t, mean_db, max_db=None):
        self.audio.append((t, mean_db, max_db if max_db is not None else mean_db))
        while self.audio and self.audio[0][0] < t - self.baseline:
            self.audio.popleft()
        self._touch(t)

    def add_keywords(self, t, hits, command=False):
        if command:
            self.voice_cmd[int(t // BUCKET)] += 1
        if hits > 0:
            self.kw[int(t // BUCKET)] += hits
        self._touch(t)

    def _touch(self, t):
        if self.start_t is None or t < self.start_t:
            self.start_t = t

    def note_cut(self, peak_time):
        """Remember a clipped peak so the same moment isn't clipped twice."""
        self.cut_peaks.append(peak_time)

    # ------------------------------------------------------------- features

    def _bucket_audio(self):
        out = defaultdict(list)
        for t, db, _mx in self.audio:
            out[int(t // BUCKET)].append(db)
        return {b: sum(v) / len(v) for b, v in out.items()}

    def _chat_effective(self, b):
        """Chat volume weighted by unique-chatter share, plus reaction cues."""
        msgs = self.msgs.get(b, 0.0)
        if not msgs:
            return 0.0
        uniq = len(self.users.get(b, ()))
        ratio = uniq / msgs
        cues = self.cues.get(b, 0.0)
        return msgs * (0.4 + 0.6 * ratio) + 0.5 * cues

    def _uniq_ratio(self, b):
        msgs = self.msgs.get(b, 0.0)
        return (len(self.users.get(b, ())) / msgs) if msgs else 0.0

    def _cmd_weight(self, b):
        return self.clip_req.get(b, 0.0) + 3.0 * self.voice_cmd.get(b, 0.0)

    @staticmethod
    def _robust_z(history, x):
        """Modified z-score using median + MAD (robust to skewed chat spikes)."""
        if len(history) < 12:
            return 0.0
        med = statistics.median(history)
        mad = statistics.median([abs(v - med) for v in history])
        if mad < 1e-9:
            # Flat signal: treat any deviation as tiny unless it is large
            spread = max(1e-3, (max(history) - min(history)) * 0.1)
            z = (x - med) / spread
        else:
            z = MAD_SCALE * (x - med) / mad
        return max(-Z_CLAMP, min(Z_CLAMP, z))

    def _history(self, series_by_b, b0, b1):
        """Baseline samples strictly before b1 (the current bucket is never
        part of its own reference)."""
        return [series_by_b.get(b, 0.0) for b in range(b0, max(b0, b1))]

    # ------------------------------------------------------------ candidates

    def candidates(self, now, threshold=2.5, min_signals=2, prominence=1.0,
                   min_distance=30.0, pre_pad=3.0, reaction_tail=8.0,
                   min_clip=25.0, max_clip=90.0):
        """Return ranked clip candidates around peaks, newest first.

        Each candidate dict:
          {"peak": t, "span": (start, end), "clip": (start, end),
           "score": float, "signals": {...}, "reason": str}
        """
        if self.start_t is None:
            return []
        b_now = int(now // BUCKET)
        b_start = int(max(self.start_t, now - self.baseline) // BUCKET)
        if b_now - b_start < 12:
            return []                      # not enough history yet

        audio_by_b = self._bucket_audio()
        chat_series = {b: self._chat_effective(b) for b in range(b_start, b_now + 1)}
        cue_series = {b: self.cues.get(b, 0.0) for b in range(b_start, b_now + 1)}
        audio_series = {b: audio_by_b.get(b, 0.0) for b in range(b_start, b_now + 1)}
        kw_series = {b: self.kw.get(b, 0.0) for b in range(b_start, b_now + 1)}
        cmd_series = {b: self._cmd_weight(b) for b in range(b_start, b_now + 1)}

        buckets = list(range(b_start, b_now + 1))
        hist_lo = b_start
        z_chat = {b: self._robust_z(self._history(chat_series, hist_lo, b), chat_series[b])
                  for b in buckets}
        z_audio = {b: self._robust_z(self._history(audio_series, hist_lo, b), audio_series[b])
                   for b in buckets}
        z_kw = {b: self._robust_z(self._history(kw_series, hist_lo, b), kw_series[b])
                for b in buckets}
        z_cmd = {b: self._robust_z(self._history(cmd_series, hist_lo, b), cmd_series[b])
                 for b in buckets}

        # loudness also counts as a dB rise over the ambient median
        audio_hist = [audio_series[b] for b in buckets if audio_series[b] > -80]
        med_db = statistics.median(audio_hist) if len(audio_hist) >= 5 else None
        db_rise = {}
        for b in buckets:
            db_rise[b] = 0.0
            if med_db is not None and audio_series[b] > -80:
                db_rise[b] = max(0.0, min(2.0, (audio_series[b] - med_db) / 6.0))

        # fused hype score per bucket
        scores, weights = {}, {}
        for b in buckets:
            parts = {"chat": z_chat[b], "audio": z_audio[b] + db_rise[b],
                     "kw": z_kw[b], "cmd": z_cmd[b]}
            w = {"chat": W_CHAT if self.msgs else 0.0,
                 "audio": W_AUDIO if audio_hist else 0.0,
                 "kw": W_KW if self.kw else 0.0,
                 "cmd": W_CMD if (self.clip_req or self.voice_cmd) else 0.0}
            total_w = sum(w.values()) or 1.0
            scores[b] = sum(parts[k] * w[k] for k in parts) / total_w
            weights[b] = w

        peaks = self._find_peaks(scores, threshold, prominence, min_distance)

        out = []
        # Explicit clip commands always win: the streamer asked for this moment,
        # so it is marked even if a louder chat burst outscores it.
        cmd_candidates = []
        for b in buckets:
            voice = self.voice_cmd.get(b, 0.0)
            chat_cmd = self.clip_req.get(b, 0.0)
            if voice <= 0 and chat_cmd <= 0:
                continue
            peak_t = b * BUCKET
            if any(abs(peak_t - p["peak"]) < 30.0 for p in cmd_candidates):
                continue
            if any(abs(peak_t - p["peak"]) < 30.0 for p in out):
                continue
            cmd_end = (b + 1) * BUCKET + 3.0        # include the callout itself
            cmd_start = peak_t - max(30.0, min_clip * 0.6)
            dur = cmd_end - cmd_start
            if dur > max_clip:
                cmd_start = cmd_end - max_clip
            elif dur < min_clip:
                cmd_end = cmd_start + min_clip
            cmd_candidates.append({
                "peak": peak_t,
                "span": (cmd_start, cmd_end),
                "clip": (cmd_start, min(cmd_end, now)),
                "score": 99.0,                        # highest precision signal
                "signals": {"chat": round(z_chat[b], 2), "audio": round(z_audio[b], 2),
                            "kw": round(z_kw[b], 2), "cmd": round(z_cmd[b], 2)},
                "uniq_ratio": round(self._uniq_ratio(b), 2),
                "reason": "clip command" + (" (voice)" if voice > 0 else " (chat)"),
            })

        for peak in peaks:
            b = peak["bucket"]          # where the signal actually spiked
            value = peak["value"]
            strong = [k for k, v in (("chat", z_chat[b]), ("audio", z_audio[b]),
                                    ("kw", z_kw[b]), ("cmd", z_cmd[b])) if v >= 1.5]
            cmd_hit = z_cmd[b] >= 1.5 or self.voice_cmd.get(b, 0) > 0
            uniq = self._uniq_ratio(b)
            cue_rate = cue_series.get(b, 0.0)
            crowd = z_chat[b] >= 3.0 and uniq >= 0.5 and cue_rate >= 3.0

            # Corroboration scales down to the number of signals that actually
            # have data (chat can be Cloudflare-blocked on Kick; VAD can strip
            # music-heavy audio). Requiring 2 of 0 available would freeze
            # detection forever — the quota/threshold gates still protect us.
            available = (1 if self.msgs else 0) + (1 if audio_hist else 0) \
                + (1 if self.kw else 0) + (1 if (self.clip_req or self.voice_cmd) else 0)
            eff_min = min(min_signals, available) if available else 999

            reason = []
            if not cmd_hit and len(strong) < eff_min and not crowd:
                continue                      # no corroboration → not a moment
            if cmd_hit:
                reason.append("clip command")
            if z_chat[b] >= 1.5:
                # one spammer is not a crowd
                if uniq < 0.25 and cue_rate < 2:
                    continue
                reason.append(f"chat z{z_chat[b]:.1f}/u{uniq:.2f}")
                if crowd and len(strong) < min_signals:
                    reason.append("crowd reaction")
            if z_audio[b] >= 1.5:
                reason.append(f"audio z{z_audio[b]:.1f}")
            if z_kw[b] >= 1.5:
                reason.append(f"keywords z{z_kw[b]:.1f}")

            span_start_b, span_end_b = self._grow_span(scores, peak["centre"], threshold)
            peak_t = b * BUCKET
            span = (span_start_b * BUCKET, (span_end_b + 1) * BUCKET)
            clip_start = span[0] - pre_pad
            clip_end = span[1] + reaction_tail
            dur = clip_end - clip_start
            if dur > max_clip:
                clip_start = clip_end - max_clip
            elif dur < min_clip:
                clip_end = clip_start + min_clip
            clip_end = min(clip_end, now)     # never cut into the future

            if any(abs(peak_t - p) < 60.0 for p in self.cut_peaks):
                continue                      # already clipped this moment

            out.append({
                "peak": peak_t,
                "span": span,
                "clip": (clip_start, clip_end),
                "score": round(value, 2),
                "signals": {"chat": round(z_chat[b], 2), "audio": round(z_audio[b], 2),
                            "kw": round(z_kw[b], 2), "cmd": round(z_cmd[b], 2)},
                "uniq_ratio": round(uniq, 2),
                "reason": ", ".join(reason) or "hype peak",
            })

        out.extend(cmd_candidates)
        out.sort(key=lambda c: -c["score"])
        return out

    # ------------------------------------------------------------- internals

    @staticmethod
    def _find_peaks(scores, threshold, prominence, min_distance):
        """Local maxima with key-saddle prominence and minimum separation.

        Prominence is measured against the *key saddle* on each side (the
        lowest point reached before the series rises again), not against the
        minimum inside a flat burst — otherwise every plateau scores zero
        prominence and no peak is ever found.

        Returns [{"bucket", "centre", "value"}]: `bucket` is where the peak
        actually is (signals are read there), `centre` is the middle of the
        plateau (used to grow the clip span).
        """
        buckets = sorted(scores)
        if len(buckets) < 5:
            return []
        values = [scores[b] for b in buckets]
        min_dist_b = max(1, int(min_distance // BUCKET))
        peaks = []
        for i in range(1, len(values) - 1):
            v = values[i]
            if v < threshold:
                continue
            if not (v >= values[i - 1] and v > values[i + 1]):
                continue
            left = Scorer._key_saddle(values, i, -1)
            right = Scorer._key_saddle(values, i, +1)
            if v - max(left, right) < prominence:
                continue
            # centre on the plateau of equal values (the hype burst is wide)
            lo = i
            while lo > 0 and values[lo - 1] >= v - 1e-9:
                lo -= 1
            hi = i
            while hi < len(values) - 1 and values[hi + 1] >= v - 1e-9:
                hi += 1
            centre = (lo + hi) // 2
            if peaks and (centre - peaks[-1]["centre_i"]) < min_dist_b:
                if v > peaks[-1]["value"]:
                    peaks[-1] = {"bucket": buckets[i], "centre": buckets[centre],
                                 "centre_i": centre, "value": v}
                continue
            peaks.append({"bucket": buckets[i], "centre": buckets[centre],
                          "centre_i": centre, "value": v})
        return peaks

    @staticmethod
    def _key_saddle(values, i, direction):
        """Lowest value reached walking away from a peak until the series
        rises above it again (or a local minimum is found)."""
        v = values[i]
        lowest = v
        j = i + direction
        while 0 <= j < len(values):
            if values[j] > v:
                break
            lowest = min(lowest, values[j])
            if direction > 0 and j + 1 < len(values) and values[j + 1] > values[j]:
                break                      # local minimum = key saddle
            if direction < 0 and j - 1 >= 0 and values[j - 1] > values[j]:
                break
            j += direction
        return lowest

    @staticmethod
    def _grow_span(scores, peak_b, threshold):
        """Grow left/right while the fused score stays elevated."""
        buckets = sorted(scores)
        idx = buckets.index(peak_b)
        half = max(0.4, threshold * 0.5)
        start = idx
        while start > 0 and (idx - start) < 12 and scores[buckets[start - 1]] >= half:
            start -= 1
        end = idx
        while end < len(buckets) - 1 and (end - idx) < 6 and scores[buckets[end + 1]] >= half:
            end += 1
        return buckets[start], buckets[end]

    # ------------------------------------------------- legacy/compat helpers

    def recent_clip_commands(self, now, chat_secs=12, voice_secs=45):
        """Chat commands (instant) and transcript voice commands (learned late)."""
        chat_lo = int((now - chat_secs) // BUCKET)
        voice_lo = int((now - voice_secs) // BUCKET)
        hi = int(now // BUCKET)
        total = sum(v for k, v in self.clip_req.items() if chat_lo <= k <= hi)
        total += sum(v for k, v in self.voice_cmd.items() if voice_lo <= k <= hi)
        return total

    def evaluate(self, now):
        """Compatibility wrapper: the best candidate's score + detail."""
        cands = self.candidates(now, threshold=0.0, min_signals=0,
                                prominence=0.0, min_distance=0.0)
        if cands:
            best = cands[0]
            return best["score"], dict(best["signals"], uniq_ratio=best["uniq_ratio"],
                                       reason=best["reason"])
        return 0.0, {"reason": "no peak yet"}
