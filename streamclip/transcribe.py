"""Whisper transcription + hype keyword extraction (faster-whisper, CPU int8).

Keyword weights follow clip-detection research:
  - hype vernacular ("insane", "let's go", …) scores 1 each
  - laughter ("haha…") scores, since it marks reaction payoffs
  - streamer clip commands ("clip it/this/that") score 5 — the highest
    precision signal there is (Eklipse-style voice command)
"""

HYPE_KEYWORDS = [
    # English
    "let's go", "lets go", "no way", "what the", "holy", "insane", "crazy",
    "unreal", "oh my god", "omg", "we did it", "clutch", "unbelievable",
    "are you kidding", "no shot", "wtf", "sheesh", "unreal",
    # Hinglish (Roman) — Hindi streams are transcribed this way most often
    "pagal", "paagal", "gajab", "ghazab", "zabardast", "jabardast",
    "kya baat", "kya kar", "matlab", "sach mein", "sachmein", "bakwas",
    "fatafat", "khatam", "arre", "oye", "yaar", "bhai", "dekh", "dekhna",
    "ruko", "chalo", "dhoka", "majboor", "hahaha", "hahah",
    # Devanagari (Whisper writes Hindi in its own script when confident)
    "पागल", "गजब", "ज़बरदस्त", "जबरदस्त", "क्या बात", "क्या कर",
    "मतलब", "सच में", "बकवास", "फटाफट", "खत्म", "अरे", "ओए", "यार",
    "भाई", "देखो", "रुको", "चलो", "धोखा", "हाहा", "हाहाहा",
]

CLIP_COMMANDS = (
    "clip it", "clip this", "clip that", "clip now",
    # Hindi/Hinglish clip commands — the highest-precision signal there is
    "clip karo", "clip kar", "clip kar do", "clip bana", "clip banao",
    "क्लिप करो", "क्लिप कर", "क्लिप बनाओ", "इसे क्लिप",
)


def analyze(text: str) -> dict:
    """Return {"score": float, "command": bool} for a transcript snippet."""
    low = (text or "").lower()
    if not low:
        return {"score": 0.0, "command": False}
    score = sum(1.0 for k in HYPE_KEYWORDS if k in low)
    score += min(low.count("!"), 3) * 0.2
    score += min(low.count("haha"), 6) * 0.75   # English laughter
    score += min(low.count("हाहा") + low.count("हा हा"), 4) * 0.75
    command = any(c in low for c in CLIP_COMMANDS)
    if command:
        score += 5.0
    return {"score": score, "command": command}


def hype_score(text: str) -> float:
    """Backwards-compatible float wrapper around analyze()."""
    return analyze(text)["score"]


def confident(seg: dict) -> bool:
    """Filter Whisper hallucinations without dropping quiet Hindi speech.

    `no_speech_prob` is high when the model thinks the chunk is silence/music;
    `avg_logprob` is low when the decode is uncertain. A genuine quiet line
    can be uncertain, so only a strong no-speech signal (or both signals at
    once) rejects it.
    """
    nsp = seg.get("no_speech_prob")
    alp = seg.get("avg_logprob")
    if nsp is not None and nsp >= 0.85:
        return False
    if alp is not None and alp <= -1.2 and (nsp is None or nsp >= 0.5):
        return False
    return True


class Transcriber:
    """Lazy-loaded faster-whisper model; safe to instantiate in a worker thread."""

    def __init__(self, model_name="base", device="cpu", compute_type="int8"):
        from faster_whisper import WhisperModel  # imported lazily on purpose
        self._model = WhisperModel(model_name, device=device,
                                   compute_type=compute_type)

    def transcribe(self, path, progress=None):
        """Return [{"start", "end", "text"}] for one segment file.

        `progress(end_seconds, total_seconds)` is called while the generator
        yields segments. The live path ignores it; the non-realtime VOD path
        uses it to keep its dashboard heartbeat moving during long videos.
        """
        segments, info = self._model.transcribe(
            str(path),
            # language=None → auto-detect per segment, so Hindi, Hinglish,
            # Spanish, Arabic… all get transcribed and can be clipped.
            language=None,
            task="transcribe",
            vad_filter=True,
            condition_on_previous_text=False,
            beam_size=1,
        )
        total = float(getattr(info, "duration", 0) or 0)
        out = []
        for s in segments:
            if s.text and s.text.strip():
                out.append({"start": s.start, "end": s.end,
                            "text": s.text.strip(),
                            "avg_logprob": getattr(s, "avg_logprob", None),
                            "no_speech_prob": getattr(s, "no_speech_prob",
                                                      None)})
            if progress:
                try:
                    progress(float(s.end), total)
                except Exception:
                    pass
        return out
