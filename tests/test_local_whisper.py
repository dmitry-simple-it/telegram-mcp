import inspect
import json
from types import SimpleNamespace

import pytest

from telegram_mcp import local_whisper
from telegram_mcp.tools import media

_transcribe_voice_message = inspect.unwrap(media.transcribe_voice_message)

# Exact text the long-lived daemon returned for a 138s voice message on 14.10.2026.
DAEMON_GARBAGE = (
    "Продолжение следует... Продолжение следует... Продолжение следует... "
    "Продолжение следует... Продолжение следует..."
)
REAL_TEXT = (
    "Привет, слушаю. Да, почитал документ. Ну, тут по факту вот из идеи, не знаю, "
    "там первое и третье, это как бы еще что-то реально меняющее в проекте."
)


def test_filler_phrases_are_not_a_transcript():
    assert local_whisper.hallucination_reason(DAEMON_GARBAGE, 138.0)
    assert local_whisper.hallucination_reason("Субтитры сделал DimaTorzok", 30.0)


def test_empty_text_is_not_a_transcript():
    assert local_whisper.hallucination_reason("  ", 12.0) == "empty transcript"


def test_real_speech_passes():
    assert local_whisper.hallucination_reason(REAL_TEXT, 12.0) is None


def test_one_word_on_short_clip_passes():
    assert local_whisper.hallucination_reason("Да.", 1.5) is None


def test_few_words_on_long_audio_is_rejected():
    assert local_whisper.hallucination_reason("Привет, слушаю.", 138.0)


@pytest.mark.asyncio
async def test_retries_once_and_returns_good_result(monkeypatch):
    results = iter(
        [
            {"text": DAEMON_GARBAGE, "language": "ru", "duration": 138.0},
            {"text": REAL_TEXT, "language": "ru", "duration": 12.0},
        ]
    )
    calls = []

    async def _worker(path, model, language):
        calls.append(path)
        return next(results)

    monkeypatch.setattr(local_whisper, "_run_worker", _worker)
    result = await local_whisper.transcribe_file("a.ogg", "m", "ru")
    assert result["text"] == REAL_TEXT
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_raises_instead_of_returning_garbage(monkeypatch):
    async def _worker(path, model, language):
        return {"text": DAEMON_GARBAGE, "language": "ru", "duration": 138.0}

    monkeypatch.setattr(local_whisper, "_run_worker", _worker)
    with pytest.raises(local_whisper.TranscriptionFailed, match="filler"):
        await local_whisper.transcribe_file("a.ogg", "m", "ru")


# --- the MCP tool is wired to the worker, never to in-process mlx_whisper ---


class _Client:
    def __init__(self, message, payload):
        self.message = message
        self.payload = payload

    async def get_messages(self, entity, ids):
        return self.message

    async def download_media(self, message, file, **kwargs):
        with open(file, "wb") as fh:
            fh.write(self.payload)


def _voice(size):
    voice_attr = SimpleNamespace(voice=True)
    document = SimpleNamespace(attributes=[voice_attr], size=size)
    return SimpleNamespace(
        id=5, media=SimpleNamespace(document=document), file=SimpleNamespace(size=size)
    )


async def _call(monkeypatch, client):
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        return "entity"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)
    return json.loads(await _transcribe_voice_message(1, 5, language="ru"))


@pytest.mark.asyncio
async def test_tool_uses_worker_and_reports_failure_as_error(monkeypatch):
    async def _worker(path, model, language):
        return {"text": DAEMON_GARBAGE, "language": "ru", "duration": 138.0}

    monkeypatch.setattr(local_whisper, "_run_worker", _worker)
    out = await _call(monkeypatch, _Client(_voice(4), b"oggs"))
    assert "text" not in out
    assert "Transcription failed" in out["error"]


@pytest.mark.asyncio
async def test_tool_returns_worker_text(monkeypatch):
    async def _worker(path, model, language):
        return {"text": REAL_TEXT, "language": "ru", "duration": 12.0}

    monkeypatch.setattr(local_whisper, "_run_worker", _worker)
    out = await _call(monkeypatch, _Client(_voice(4), b"oggs"))
    assert out["text"] == REAL_TEXT
    assert out["duration"] == 12.0


@pytest.mark.asyncio
async def test_tool_refuses_incomplete_download(monkeypatch):
    async def _worker(path, model, language):  # pragma: no cover - must not run
        raise AssertionError("worker must not run on a partial file")

    monkeypatch.setattr(local_whisper, "_run_worker", _worker)
    out = await _call(monkeypatch, _Client(_voice(10), b"oggs"))
    assert "Incomplete download" in out["error"]
