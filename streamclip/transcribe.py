"""Whisper transcription + hype keyword extraction (faster-whisper, CPU int8).

Keyword weights follow clip-detection research:
  - hype vernacular ("insane", "let's go", …) scores 1 each
  - laughter ("haha…") scores, since it marks reaction payoffs
  - streamer clip commands ("clip it/this/that") score 5 — the highest
    precision signal there is (Eklipse-style voice command)
"""

HYPE_KEYWORDS = [
    "let's go", "lets go", "no way", "what the", "holy", "insane", "crazy",
    "unreal", "oh my god", "omg", "we did it", "clutch", "unbelievable",
    "are you kidding", "no shot", "wtf", "sheesh", "unreal",
]

CLIP_COMMANDS = ("clip it", "clip this", "clip that", "clip now")


def analyze(text: str) -> dict:
    """Return {"score": float, "command": bool} for a transcript snippet."""
    low = (text or "").lower()
    if not low:
        return {"score": 0.0, "command": False}
    score = sum(1.0 for k in HYPE_KEYWORDS if k in low)
    score += min(low.count("!"), 3) * 0.2
    score += min(low.count("haha"), 6) * 0.75   # laughter
    command = any(c in low for c in CLIP_COMMANDS)
    if command:
        score += 5.0
    return {"score": score, "command": command}


def hype_score(text: str) -> float:
    """Backwards-compatible float wrapper around analyze()."""
    return analyze(text)["score"]


class Transcriber:
    """Lazy-loaded faster-whisper model; safe to instantiate in a worker thread."""

    def __init__(self, model_name="base", device="cpu", compute_type="int8"):
        from faster_whisper import WhisperModel  # imported lazily on purpose
        self._model = WhisperModel(model_name, device=device,
                                   compute_type=compute_type)

    def transcribe(self, path):
        """Return [{"start", "end", "text"}] for one segment file."""
        segments, _info = self._model.transcribe(
            str(path),
            vad_filter=True,
            condition_on_previous_text=False,
            beam_size=1,
        )
        return [{"start": s.start, "end": s.end, "text": s.text.strip()}
                for s in segments if s.text and s.text.strip()]
