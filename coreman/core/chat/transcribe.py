"""语音转文字：飞书的语音消息只带音频文件，不带转写，平台先转成文字再交给 AI 员工。

三种运行时的 CLI 都收不了音频，所以不把音频交给 Agent。用 OpenAI 的转写接口（与群聊旁听共用
`OPENAI_API_KEY`）；没配密钥时语音照旧回「暂不支持」。音频不落盘、不进日志。
"""

from __future__ import annotations

from typing import Protocol

import httpx

from coreman.core.config import Settings

TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
TIMEOUT_SECONDS = 60.0
# OpenAI 转写接口单个文件上限 25MB。
MAX_BYTES = 25 * 1024 * 1024


class TranscribeError(Exception):
    """转写失败（网络、接口报错、文件过大）；调用方给用户一句「没听清」。"""


class Transcriber(Protocol):
    async def transcribe(self, data: bytes, *, filename: str, mime: str) -> str: ...


class OpenAITranscriber:
    def __init__(self, api_key: str, model: str, *, http: httpx.AsyncClient | None = None):
        self._key = api_key
        self.model = model
        self._http = http

    async def transcribe(self, data: bytes, *, filename: str, mime: str) -> str:
        if not data or len(data) > MAX_BYTES:
            raise TranscribeError("size")
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            response = await client.post(
                TRANSCRIPTIONS_URL,
                data={"model": self.model, "response_format": "json"},
                files={"file": (filename, data, mime)},
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                raise TranscribeError(f"http_{response.status_code}")
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TranscribeError(type(exc).__name__) from exc
        finally:
            if self._http is None:
                await client.aclose()
        text = body.get("text") if isinstance(body, dict) else None
        return text.strip() if isinstance(text, str) else ""


def from_settings(cfg: Settings) -> Transcriber | None:
    """没配 OPENAI_API_KEY 时返回 None：飞书语音照旧回「暂不支持」。"""
    if cfg.openai_api_key is None or not cfg.openai_api_key.get_secret_value():
        return None
    return OpenAITranscriber(cfg.openai_api_key.get_secret_value(), cfg.transcribe_model)
