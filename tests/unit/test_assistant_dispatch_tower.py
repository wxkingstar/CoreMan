"""调度单走自己的目标系统：配置、哈希与提交体都按它来，个人工具的那一个不受影响。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from coreman.core.assistants.approvals import tower
from coreman.core.config import Settings

BASE = {
    "tower_url": "https://tower.example.com/",
    "tower_sender_key": SecretStr("s"),
    "tower_callback_secret": SecretStr("c"),
    "public_base_url": "https://coreman.example.com/",
}


def _settings(**keys: str) -> Settings:
    targets = {name: SecretStr(value) for name, value in keys.items()}
    return Settings.model_construct(**{**BASE, "tower_target_keys": targets})


def test_each_target_system_has_its_own_key() -> None:
    assert tower.DISPATCH_TARGET == "assistant-dispatch"
    both = _settings(**{tower.TARGET: "personal-key", tower.DISPATCH_TARGET: "dispatch-key"})
    personal, dispatching = tower.config(both), tower.config(both, tower.DISPATCH_TARGET)
    assert personal is not None and dispatching is not None
    assert (personal.target, personal.target_key) == ("feishu-personal", "personal-key")
    assert (dispatching.target, dispatching.target_key) == ("assistant-dispatch", "dispatch-key")
    # 只登记了其中一个：另一个不可用，互不顶替。
    only = _settings(**{tower.TARGET: "personal-key"})
    assert tower.config(only) is not None and tower.config(only, tower.DISPATCH_TARGET) is None


def test_the_hash_is_bound_to_the_target_system() -> None:
    both = _settings(**{tower.TARGET: "k1", tower.DISPATCH_TARGET: "k2"})
    personal, dispatching = tower.config(both), tower.config(both, tower.DISPATCH_TARGET)
    assert personal is not None and dispatching is not None
    body = tower.request_body("assistant_ask_bot", [{"arguments": {"question": "查库存"}}])
    assert tower.request_hash(personal, "cm-pa-1", body, "bot") != tower.request_hash(
        dispatching, "cm-pa-1", body, "bot"
    )


async def test_submit_names_the_target_system(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"approval_id": "apv-1"})

    monkeypatch.setattr(tower, "transport", httpx.MockTransport(handle))
    cfg = tower.config(_settings(**{tower.DISPATCH_TARGET: "k"}), tower.DISPATCH_TARGET)
    assert cfg is not None
    await tower.submit(
        cfg,
        external_id="cm-pa-1",
        requester="zhangsan",
        caller="demo-assistant",
        abstract="调度员工 → demo-bot",
        content="正文",
        body=b"{}",
        expires_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    assert seen[0]["request"]["target"] == "assistant-dispatch"
