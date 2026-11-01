"""群聊旁听的判断模型：只回答一个「它现在该不该主动说话」，返回概率，不生成文字。

平台进程直接调用，不经运行时节点。实测（中文群聊样本）OpenAI Decisions 在不该说话的情况上
几乎给 0，误插话最少；判断层做成接口，换别家只需再实现一个 `Decider`。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from coreman.core.config import Settings

DECISIONS_URL = "https://api.openai.com/v1/decisions"
TIMEOUT_SECONDS = 3.0

INSTRUCTION = (
    "群聊里有一位 AI 同事 `bot.name`，职责见 `bot.role`。判断：它现在是否应该主动发一条消息，"
    "回应 `recent_messages` 最后一条消息所在的话题？"
    "应该（是）：最后一条消息叫了它的名字在找它（例如「`bot.name` 在吗」「`bot.name` 帮我看下」），"
    "即使只是问在不在；或者最近的消息里有人（包括其他机器人）提出了属于它职责范围的问题、请求或数据疑问，"
    "或在追问它自己（`sender_kind` 为 `self`）之前的回答；或者它掌握与当前讨论直接相关、"
    "大家还不知道且需要知道的信息（例如有人正在用已被纠正的数据）；并且还没有同事回答、认领或"
    "同步过，也不是点名问其他具体的人，也没有人要求它别说话。"
    "不应该（否）：闲聊、表情、确认致谢、通知公告、领域外的事、已被回答或有人认领、"
    "只是在谈论它而不是在找它、或是对它自己消息的致谢。"
    "如果 `recent_messages` 里它最近已经就这个话题说过，之后没有人回应或提出新的情况，"
    "也算否：不要重复自己。"
)


@dataclass(frozen=True)
class Decision:
    probability: float | None
    model: str
    latency_ms: int
    input_tokens: int | None = None
    refused: bool = False
    error: str | None = None


class Decider(Protocol):
    async def should_interject(self, state: dict[str, Any]) -> Decision: ...


class OpenAIDecider:
    def __init__(self, api_key: str, model: str, *, http: httpx.AsyncClient | None = None):
        self._key = api_key
        self.model = model
        self._http = http

    async def should_interject(self, state: dict[str, Any]) -> Decision:
        body = {
            "model": self.model,
            "input": json.dumps(state, ensure_ascii=False),
            "questions": [{"type": "predicate", "name": "interject", "instructions": INSTRUCTION}],
        }
        started = time.monotonic()

        def done(**kwargs: Any) -> Decision:
            return Decision(
                model=self.model, latency_ms=int((time.monotonic() - started) * 1000), **kwargs
            )

        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            response = await client.post(
                DECISIONS_URL,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                return done(probability=None, error=f"http_{response.status_code}")
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return done(probability=None, error=type(exc).__name__)
        finally:
            if self._http is None:
                await client.aclose()
        usage = data.get("usage") if isinstance(data, dict) else None
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        tokens = tokens if isinstance(tokens, int) else None
        for answer in data.get("answers") or [] if isinstance(data, dict) else []:
            if not isinstance(answer, dict) or answer.get("name") != "interject":
                continue
            if answer.get("type") == "refusal":
                return done(probability=None, refused=True, input_tokens=tokens)
            value = answer.get("probability")
            if isinstance(value, int | float) and 0 <= value <= 1:
                return done(probability=float(value), input_tokens=tokens)
        return done(probability=None, input_tokens=tokens, error="no_answer")


def from_settings(cfg: Settings) -> Decider | None:
    """部署没配判断服务的密钥时返回 None：旁听开不了，已开的也不判断。"""
    if not cfg.decision_ready or cfg.openai_api_key is None:
        return None
    return OpenAIDecider(cfg.openai_api_key.get_secret_value(), cfg.decision_model)
