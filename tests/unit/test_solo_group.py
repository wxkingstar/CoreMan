"""群里真人只有发话人自己：每次都实时问飞书，查不到、外部群、多一个人都按普通群。"""

from typing import Any

import pytest

from coreman.core.chat import solo_group
from coreman.core.platforms.feishu import FeishuError


class FakeClient:
    """按路径回固定结果；记下每次调用，验证没有缓存。"""

    def __init__(self, chat: Any, members: Any) -> None:
        self.chat = chat
        self.members = members
        self.calls: list[str] = []

    async def call(self, method: str, path: str, **_: Any) -> dict[str, Any]:
        self.calls.append(path)
        value = self.members if path.endswith("/members") else self.chat
        if isinstance(value, Exception):
            raise value
        return value


def chat(user_count: Any = "1", external: Any = False) -> dict[str, Any]:
    data: dict[str, Any] = {"user_count": user_count, "bot_count": "2"}
    if external is not None:
        data["external"] = external
    return {"data": data}


def members(*ids: str, has_more: bool = False, total: Any = None) -> dict[str, Any]:
    return {
        "data": {
            "items": [{"member_id": i, "member_id_type": "user_id"} for i in ids],
            "has_more": has_more,
            "member_total": len(ids) if total is None else total,
        }
    }


async def test_only_the_speaker_and_bots_is_solo():
    client = FakeClient(chat(), members("u_speaker"))
    assert await solo_group.solo_with(client, "oc_1", "u_speaker") is True


@pytest.mark.parametrize(
    "fake",
    [
        # 真人有两个：另一个人也在群里。
        FakeClient(chat(user_count="2"), members("u_speaker", "u_other")),
        # 群信息说一个人，但那个人不是发话人（发完话退群、换了人）。
        FakeClient(chat(), members("u_other")),
        # 成员列表还有下一页。
        FakeClient(chat(), members("u_speaker", has_more=True)),
        FakeClient(chat(), members("u_speaker", total=2)),
        # 外部群，或者群信息里看不出是不是外部群。
        FakeClient(chat(external=True), members("u_speaker")),
        FakeClient(chat(external=None), members("u_speaker")),
        # 查不到。
        FakeClient(FeishuError(99991672, "no permission"), members("u_speaker")),
        FakeClient(chat(), FeishuError(99991672, "no permission")),
        FakeClient(chat(), RuntimeError("network down")),
        FakeClient({"data": None}, members("u_speaker")),
        FakeClient(chat(user_count=None), members("u_speaker")),
        FakeClient(chat(user_count="0"), members()),
    ],
)
async def test_anything_else_counts_as_an_ordinary_group(fake):
    assert await solo_group.solo_with(fake, "oc_1", "u_speaker") is False


async def test_no_speaker_id_is_never_solo():
    client = FakeClient(chat(), members(""))
    assert await solo_group.solo_with(client, "oc_1", "") is False
    assert client.calls == []


async def test_every_check_asks_feishu_again():
    client = FakeClient(chat(), members("u_speaker"))
    assert await solo_group.solo_with(client, "oc_1", "u_speaker") is True
    # 有人进群：下一次判断马上变成普通群，不吃缓存。
    client.chat, client.members = chat(user_count="2"), members("u_speaker", "u_new")
    assert await solo_group.solo_with(client, "oc_1", "u_speaker") is False
    # 那人又退了：再下一次回到单人群。
    client.chat, client.members = chat(), members("u_speaker")
    assert await solo_group.solo_with(client, "oc_1", "u_speaker") is True
    # 群信息就说有两个人时不再查成员列表。
    assert len(client.calls) == 5


async def test_slow_feishu_counts_as_an_ordinary_group(monkeypatch):
    import asyncio

    class Slow(FakeClient):
        async def call(self, method: str, path: str, **_: Any) -> dict[str, Any]:
            await asyncio.sleep(1)
            return chat()

    monkeypatch.setattr(solo_group, "TIMEOUT_SECONDS", 0.01)
    assert await solo_group.solo_with(Slow(None, None), "oc_1", "u_speaker") is False


def test_marked_reads_only_a_strict_true():
    class T:
        payload: dict[str, Any] = {}

    task = T()
    assert solo_group.marked(task) is False  # type: ignore[arg-type]
    task.payload = {solo_group.PAYLOAD_KEY: "true"}
    assert solo_group.marked(task) is False  # type: ignore[arg-type]
    task.payload = {solo_group.PAYLOAD_KEY: True}
    assert solo_group.marked(task) is True  # type: ignore[arg-type]
