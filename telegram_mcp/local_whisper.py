"""[fork] Local mlx-whisper transcription, isolated in a fresh process per call.

Why a subprocess: the MCP server is a launchd singleton that lives for days, and
mlx-whisper running inside it degrades with uptime — the same file, model and code
that transcribe correctly in a fresh process come back from the daemon as
hallucinated filler ("Продолжение следует..." x5) or hang (18.09.2026, 14.10.2026;
a restart always "fixed" it). Clearing the Metal cache and unloading the model did
not stop it. A short-lived worker process owns all MLX/Metal state and takes it
with it on exit, so daemon uptime can no longer affect transcription quality.

Run as a script, this file is the worker: it prints one JSON line to stdout.
"""

import asyncio
import json
import os
import re
import sys
from typing import Optional

WORKER_PATH = os.path.abspath(__file__)
WHISPER_TIMEOUT_SECONDS = float(os.getenv("WHISPER_TIMEOUT_SECONDS", "900"))

# One transcription at a time: parallel workers would each load ~1.5GB of weights
# onto the same GPU and only slow each other down.
_lock = asyncio.Lock()

# Phrases Whisper emits on silence/garbage input (subtitle credits from its
# training data), not speech. Lowercase, punctuation-insensitive.
_HALLUCINATION_PHRASES = (
    "продолжение следует",
    "субтитры сделал",
    "субтитры создавал",
    "субтитры подогнал",
    "редактор субтитров",
    "корректор",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "ставьте лайки",
    "thank you for watching",
    "thanks for watching",
    "subtitles by",
)
_MIN_WORDS_PER_SECOND = 0.25  # normal speech is ~2 words/s
_MIN_CHECKED_DURATION = 8.0  # shorter clips can legitimately hold one word


class TranscriptionFailed(Exception):
    """Raised with a curated, caller-safe ``reason`` (no paths, no worker stderr)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def hallucination_reason(text: str, duration: Optional[float]) -> Optional[str]:
    """Return why ``text`` does not look like a real transcript, or None if it does."""
    normalized = re.sub(r"[^\w\s]", " ", text.lower())
    remainder = normalized
    for phrase in _HALLUCINATION_PHRASES:
        remainder = remainder.replace(phrase, " ")
    words = remainder.split()
    if not words:
        if normalized.split():
            return f"only Whisper filler phrases, no speech: {text.strip()[:120]!r}"
        return "empty transcript"
    if duration and duration >= _MIN_CHECKED_DURATION:
        rate = len(normalized.split()) / duration
        if rate < _MIN_WORDS_PER_SECOND:
            return (
                f"{len(normalized.split())} words for {duration:.0f}s of audio "
                f"({rate:.2f} words/s) — transcript is mostly missing"
            )
    return None


async def _run_worker(path: str, model: str, language: Optional[str]) -> dict:
    args = [sys.executable, "-I", WORKER_PATH, path, model, language or ""]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), WHISPER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TranscriptionFailed(f"Whisper worker timed out after {WHISPER_TIMEOUT_SECONDS:.0f}s")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    lines = stdout.decode("utf-8", "replace").strip().splitlines()
    if proc.returncode != 0 or not lines:
        # stderr can hold paths and tracebacks: it stays in the daemon's own stderr log.
        sys.stderr.write(stderr.decode("utf-8", "replace")[-4000:])
        raise TranscriptionFailed(
            f"Whisper worker failed (exit {proc.returncode}); details in the server stderr log"
        )
    return json.loads(lines[-1])


async def transcribe_file(path: str, model: str, language: Optional[str] = None) -> dict:
    """Transcribe ``path`` in a fresh worker; retry once if the result is not speech.

    Returns {"text", "language", "duration"}. Raises TranscriptionFailed instead of
    returning filler text, so a caller never mistakes a hallucination for a transcript.
    """
    async with _lock:
        reason = None
        for _attempt in range(2):
            result = await _run_worker(path, model, language)
            reason = hallucination_reason(result.get("text", ""), result.get("duration"))
            if reason is None:
                return result
        raise TranscriptionFailed(f"Whisper returned no usable transcript twice: {reason}")


def _worker_main(argv: list) -> None:
    path, model, language = argv[1], argv[2], argv[3] or None
    import mlx_whisper
    from mlx_whisper.audio import SAMPLE_RATE, load_audio

    audio = load_audio(path)
    kwargs = {"path_or_hf_repo": model, "verbose": None}
    if language:
        kwargs["language"] = language
    result = mlx_whisper.transcribe(audio, **kwargs)
    print(
        json.dumps(
            {
                "text": result.get("text", "").strip(),
                "language": result.get("language"),
                "duration": round(len(audio) / SAMPLE_RATE, 2),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    _worker_main(sys.argv)
