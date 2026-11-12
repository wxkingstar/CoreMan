"""Tower 发送端客户端：提交、取令牌、核销、上报、撤回，以及请求体与哈希。"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from coreman.core.assistants.approvals import tower
from coreman.core.cloud_proxy import tower_gate
from coreman.core.config import Settings
from coreman.core.db.models.bots import BOT_KEY_RE

CFG = tower.Config(
    "https://tower.example.com", "sender-key", "target-key", "https://coreman.example.com/base"
)
DUE = datetime(2026, 11, 15, 8, 30, tzinfo=UTC)
# Tower 对 `request.caller` 的要求：账号名里常见的那几类字符，最长 128。
TOWER_CALLER = r"^[A-Za-z0-9._@:-]{1,128}$"


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(tower, "transport", httpx.MockTransport(handle))
    return seen


def test_config_needs_every_part() -> None:
    base = {
        "tower_url": "https://tower.example.com/",
        "tower_sender_key": SecretStr("s"),
        "tower_callback_secret": SecretStr("c"),
        "tower_target_keys": {tower.TARGET: SecretStr("t")},
        "public_base_url": "https://coreman.example.com/",
    }
    settings = Settings.model_construct(**base)
    found = tower.config(settings)
    assert found == tower.Config(
        "https://tower.example.com", "s", "t", "https://coreman.example.com"
    )
    for missing in ("tower_url", "tower_sender_key", "tower_callback_secret"):
        assert tower.config(Settings.model_construct(**{**base, missing: None})) is None
    # 只配了云签名代理的目标系统：审批闸门不可用。
    other = {**base, "tower_target_keys": {"alidns": SecretStr("t")}}
    assert tower.config(Settings.model_construct(**other)) is None


def test_body_is_canonical_and_hash_follows_the_gate_rule() -> None:
    calls = [{"arguments": {"text": "好", "b": 1, "a": 2}, "chat_id": "oc_1"}]
    body = tower.request_body("feishu_send_message", calls)
    assert body == '{"calls":[{"a":2,"b":1,"text":"好"}],"tool":"feishu_send_message"}'.encode()
    # 会话与锚点是平台检查用的，不属于被批准的请求体。
    assert b"oc_1" not in body
    expected = tower_gate.request_hash_parts(
        "POST", "feishu-personal", "/base/api/assistant/approvals/cm-pa-1/execute", "", body, "bot"
    )
    assert tower.request_hash(CFG, "cm-pa-1", body, "bot") == expected
    assert tower.request_hash(CFG, "cm-pa-1", body + b" ", "bot") != expected
    assert tower.request_hash(CFG, "cm-pa-2", body, "bot") != expected


def test_files_are_hashed_by_content_not_by_object_name() -> None:
    ref = {"key": "tmp/abc", "filename": "a.pdf", "mime": "text/plain", "size": 3, "sha256": "ff"}
    body = tower.request_body("feishu_save_to_drive", [{"arguments": {"upload_id": ref}}])
    assert json.loads(body) == {
        "tool": "feishu_save_to_drive",
        "calls": [{"upload_id": {"filename": "a.pdf", "size": 3, "sha256": "ff"}}],
    }
    moved = {**ref, "key": "tmp/another"}
    assert body == tower.request_body("feishu_save_to_drive", [{"arguments": {"upload_id": moved}}])
    changed = {**ref, "sha256": "00"}
    assert body != tower.request_body(
        "feishu_save_to_drive", [{"arguments": {"upload_id": changed}}]
    )


def test_every_bot_key_is_an_acceptable_caller() -> None:
    # `bot_key` 的字符集（小写字母、数字、下划线、连字符，最长 50）是 Tower 接受的调用方的子集。
    assert BOT_KEY_RE == r"^[a-z0-9][a-z0-9_-]{1,49}$"
    for key in ("a1", "demo-bot_x", "a" + "z" * 49, "0-_"):
        assert re.fullmatch(BOT_KEY_RE, key) and re.fullmatch(TOWER_CALLER, key)


async def test_submit_sends_the_owner_as_requester_and_no_approvers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serve(monkeypatch, lambda _: httpx.Response(201, json={"approval_id": "apv-1"}))
    sent = await tower.submit(
        CFG,
        external_id="cm-pa-1",
        requester="zhangsan",
        caller="assistant-abc",
        abstract="私聊 → 李四",
        content="渠道：私聊",
        body=b'{"x":1}',
        expires_at=DUE,
    )
    assert sent == tower.Submitted("apv-1", False)
    request = seen[0]
    assert str(request.url) == "https://tower.example.com/v1/approvals"
    assert request.headers["x-api-key"] == "sender-key"
    payload = json.loads(request.content)
    assert payload["approvers"] == [] and payload["abstract"] == "私聊 → 李四"
    assert payload["expires_at"] == "2026-11-15T08:30:00Z"
    assert payload["request"] == {
        "target": "feishu-personal",
        "caller": "assistant-abc",
        "requester": "zhangsan",
        "method": "POST",
        "url": "https://coreman.example.com/base/api/assistant/approvals/cm-pa-1/execute",
        "body_b64": base64.b64encode(b'{"x":1}').decode(),
        "summary": "私聊 → 李四",
    }


async def test_resubmitting_the_same_external_id_reports_an_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        lambda _: httpx.Response(200, json={"approval_id": "apv-1", "updated": True}),
    )
    sent = await tower.submit(
        CFG,
        external_id="e",
        requester="r",
        caller="c",
        abstract="a",
        content="x",
        body=b"{}",
        expires_at=DUE,
    )
    assert sent.updated is True


async def test_an_odd_approval_id_is_refused_before_it_reaches_a_url_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in ("../x", "a/b", "a b", "x" * 129):
        _serve(monkeypatch, lambda _, bad=bad: httpx.Response(201, json={"approval_id": bad}))
        with pytest.raises(tower.TowerError) as refused:
            await tower.submit(
                CFG,
                external_id="e",
                requester="r",
                caller="c",
                abstract="a",
                content="x",
                body=b"{}",
                expires_at=DUE,
            )
        assert (refused.value.code, refused.value.status) == ("tower_unavailable", 503), bad


async def test_refusals_carry_the_code_and_outages_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        lambda _: httpx.Response(400, json={"error": "unknown_approvers", "message": "…"}),
    )
    with pytest.raises(tower.TowerError) as refused:
        await tower.token(CFG, "apv-1")
    assert refused.value.code == "unknown_approvers" and not refused.value.unavailable

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    for handler in (down, lambda _: httpx.Response(502, text="bad gateway")):
        _serve(monkeypatch, handler)
        with pytest.raises(tower.TowerError) as outage:
            await tower.withdraw(CFG, "cm-pa-1")
        assert outage.value.code == "tower_unavailable" and outage.value.unavailable


async def test_token_report_and_withdraw_hit_their_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"token": "twa_1"})
        return httpx.Response(200, json={"ok": True})

    seen = _serve(monkeypatch, handle)
    assert await tower.token(CFG, "apv-1") == "twa_1"
    await tower.report(CFG, "apv-1", "failed", "stale")
    await tower.withdraw(CFG, "cm-pa-1")
    assert [r.url.path for r in seen] == [
        "/v1/approvals/apv-1/token",
        "/v1/approvals/apv-1/result",
        "/v1/approvals/withdraw",
    ]
    assert json.loads(seen[1].content) == {"status": "failed", "error": "stale"}
    assert json.loads(seen[2].content) == {"external_id": "cm-pa-1"}


async def test_redeem_uses_the_target_key_and_maps_gate_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, ...]] = []

    def redeem(*args: Any) -> str:
        seen.append(args)
        return "apv-1"

    monkeypatch.setattr(tower_gate, "redeem", redeem)
    assert await tower.redeem(CFG, "twa_1", "hash", "bot") == "apv-1"
    assert seen == [("https://tower.example.com", "target-key", "twa_1", "hash", "bot")]

    def mismatch(*_: Any) -> str:
        raise tower_gate.TowerGateError("request_mismatch", 403)

    monkeypatch.setattr(tower_gate, "redeem", mismatch)
    with pytest.raises(tower.TowerError) as caught:
        await tower.redeem(CFG, "twa_1", "hash", "bot")
    assert caught.value.code == "request_mismatch" and not caught.value.unavailable
