from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from coreman.core.chat.transcribe import OpenAITranscriber, TranscribeError, from_settings


async def test_openai_transcriber_posts_the_audio_and_reads_the_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": " 你好 "})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transcriber = OpenAITranscriber("sk-synthetic", "gpt-4o-transcribe", http=http)
    assert await transcriber.transcribe(b"OggS", filename="voice.ogg", mime="audio/ogg") == "你好"
    [request] = seen
    assert request.url.path == "/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer sk-synthetic"
    assert b'name="model"' in request.content and b"gpt-4o-transcribe" in request.content
    await http.aclose()


async def test_openai_transcriber_errors() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    transcriber = OpenAITranscriber("sk-synthetic", "m", http=http)
    with pytest.raises(TranscribeError):
        await transcriber.transcribe(b"x", filename="voice.ogg", mime="audio/ogg")
    with pytest.raises(TranscribeError):
        await transcriber.transcribe(b"", filename="voice.ogg", mime="audio/ogg")
    await http.aclose()


def test_from_settings_needs_the_key() -> None:
    def cfg(key: str | None) -> Any:
        return SimpleNamespace(
            openai_api_key=SecretStr(key) if key is not None else None,
            transcribe_model="gpt-transcribe",
        )

    assert from_settings(cfg(None)) is None and from_settings(cfg("")) is None
    made = from_settings(cfg("sk-synthetic"))
    assert isinstance(made, OpenAITranscriber) and made.model == "gpt-transcribe"
