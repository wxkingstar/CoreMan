"""写操作保险丝：助理记录的非读接口到不了飞书，除非平台正在执行已批准的单。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from coreman.core.assistants import write_fuse
from coreman.core.feishu_personal import service


class _Reached(Exception):
    """保险丝放行之后的下一步（取授权记录）。"""


@pytest.fixture(autouse=True)
def past_the_fuse(monkeypatch: pytest.MonkeyPatch) -> None:
    async def row(*_: Any) -> Any:
        raise _Reached

    monkeypatch.setattr(service, "_row", row)


def _scope(kind: str) -> Any:
    return SimpleNamespace(bot=SimpleNamespace(kind=kind, id=uuid.uuid4()))


async def _call(scope: Any, method: str, path: str) -> None:
    await service.api_request(None, None, scope, method, path)  # type: ignore[arg-type]


async def test_assistant_writes_stop_before_any_token_is_read() -> None:
    scope = _scope("assistant")
    for method, path in (
        ("POST", "/im/v1/messages"),
        ("DELETE", "/im/v1/messages/om_1"),
        ("POST", "/task/v2/tasks"),
    ):
        with pytest.raises(service.PersonalError) as caught:
            await _call(scope, method, path)
        assert caught.value.code == "approval_required"


async def test_assistant_reads_pass() -> None:
    with pytest.raises(_Reached):
        await _call(_scope("assistant"), "GET", "/im/v1/messages")
    with pytest.raises(_Reached):
        await _call(_scope("assistant"), "POST", "/im/v1/messages/search")


async def test_allow_opens_it_only_for_that_assistant_and_only_inside() -> None:
    scope, other = _scope("assistant"), _scope("assistant")
    with write_fuse.allow(scope.bot.id):
        with pytest.raises(_Reached):
            await _call(scope, "POST", "/im/v1/messages")
        with pytest.raises(service.PersonalError):
            await _call(other, "POST", "/im/v1/messages")
    with pytest.raises(service.PersonalError):
        await _call(scope, "POST", "/im/v1/messages")


async def test_standard_bots_are_untouched() -> None:
    with pytest.raises(_Reached):
        await _call(_scope("standard"), "POST", "/im/v1/messages")
    with pytest.raises(_Reached):
        await _call(_scope("standard"), "DELETE", "/im/v1/messages/om_1")
