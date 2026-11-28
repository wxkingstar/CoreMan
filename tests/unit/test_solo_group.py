"""群里真人只有发话人自己：查不到、外部群、多一个人都按普通群；结果按群缓存一小时。"""

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


async def test_the_uncached_check_asks_feishu_every_time():
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


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def cached(monkeypatch):
    """`solo` 走缓存的那一层：换掉飞书客户端、凭据解密与时钟。"""
    solo_group.forget()
    clock = Clock()
    fake = FakeClient(chat(), members("u_speaker"))

    class Client:
        def __init__(self, app_id: str, secret: str) -> None:
            pass

        async def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            return await fake.call(method, path, **kwargs)

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(solo_group, "FeishuClient", Client)
    monkeypatch.setattr(solo_group, "decrypt_json", lambda *a: {"app_id": "a", "app_secret": "s"})
    monkeypatch.setattr(solo_group.time, "monotonic", clock)
    yield fake, clock
    solo_group.forget()


class Bot:
    id = "bot-1"
    credentials_enc = "x"


async def ask(user: str = "u_speaker", chat_id: str = "oc_1") -> bool:
    return await solo_group.solo(None, Bot(), chat_id, user)  # type: ignore[arg-type]


async def test_the_answer_is_cached_for_an_hour(cached):
    fake, clock = cached
    assert await ask() is True
    # 拉了人进来：缓存期内仍按单人群（产品上接受这段延迟），不再问飞书。
    fake.chat, fake.members = chat(user_count="2"), members("u_speaker", "u_new")
    clock.now += solo_group.CACHE_SECONDS - 1
    assert await ask() is True
    assert len(fake.calls) == 2
    # 过期后再查一次，回到群规则。
    clock.now += 2
    assert await ask() is False
    assert len(fake.calls) == 3


async def test_someone_else_speaking_ends_it_at_once(cached):
    fake, clock = cached
    assert await ask() is True
    # 新来的人说话：不等缓存过期，这一条和之后发话人自己的消息都按普通群，并按普通群记 7 天。
    assert await ask("u_new") is False
    clock.now += solo_group.GROUP_CACHE_SECONDS - 1
    assert await ask() is False
    assert len(fake.calls) == 2


async def test_an_ordinary_group_is_not_asked_again_for_seven_days(cached):
    fake, clock = cached
    fake.chat = chat(user_count="3")
    assert await ask() is False
    # 有人退群变成单人群：7 天内不再调接口，仍按普通群（产品上接受）。
    fake.chat = chat()
    clock.now += solo_group.GROUP_CACHE_SECONDS - 1
    assert await ask() is False
    assert len(fake.calls) == 1
    # 7 天后再查一次，认出单人群。
    clock.now += 2
    assert await ask() is True
    assert len(fake.calls) == 3


def test_ordinary_groups_are_kept_longer_than_solo_groups():
    assert solo_group.GROUP_CACHE_SECONDS == 7 * 24 * 3600
    assert solo_group.CACHE_SECONDS == 3600


async def test_a_failed_lookup_is_retried_after_a_few_minutes(cached):
    fake, clock = cached
    fake.chat = FeishuError(99991672, "no permission")
    assert await ask() is False
    assert await ask() is False
    assert len(fake.calls) == 1
    fake.chat = chat()
    clock.now += solo_group.FAILURE_CACHE_SECONDS + 1
    assert await ask() is True


async def test_each_group_is_cached_on_its_own(cached):
    fake, _ = cached
    assert await ask(chat_id="oc_1") is True
    fake.chat = chat(user_count="2")
    assert await ask(chat_id="oc_2") is False
    assert await ask(chat_id="oc_1") is True
