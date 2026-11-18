"""不托管的会话：个人令牌的入口拒绝点名它的请求、先查清只给了消息编号的请求、从读应答里滤掉它。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from coreman.core.assistants import chat_guard, write_fuse
from coreman.core.feishu_personal import endpoints, service

OWN = "oc_own"
# 测试里的会话类型：oc_li 是单聊，其余是群。
KINDS = {"oc_li": "p2p"}


async def kind_of(chat_id: str) -> str | None:
    return KINDS.get(chat_id, "group")


def _rules(
    *,
    listed: bool = False,
    ignored: tuple[str, ...] = (),
    managed: tuple[str, ...] = (),
    message_range: str = "all",
):
    row = SimpleNamespace(
        chat_scope="listed" if listed else "all",
        ignored_chats_enc=list(ignored),
        managed_chats_enc=list(managed),
        chat_id=OWN,
    )
    return chat_guard.rules_of(row, message_range)  # type: ignore[arg-type]


def test_rules_by_scope() -> None:
    hidden = _rules(ignored=("oc_secret",))
    assert hidden.denies("oc_secret") and not hidden.denies("oc_team")
    assert not hidden.denies(None) and not hidden.denies("")
    only = _rules(listed=True, managed=("oc_team",), ignored=("oc_team",))
    # 只托管名单时不看不托管名单；所有者与助理的私聊总在托管里。
    assert not only.denies("oc_team") and not only.denies(OWN)
    assert only.denies("oc_other")
    assert not _rules().active and _rules(listed=True).active
    group_only = _rules(message_range="group")
    assert group_only.active and group_only.denies_kind("p2p") and group_only.denies_kind(None)
    assert not group_only.denies_kind("group") and not _rules().denies_kind(None)


async def test_screen_drops_unmanaged_items_and_refuses_a_single_message() -> None:
    rules = _rules(ignored=("oc_secret",))
    data = {
        "items": [
            {"message_id": "om_1", "chat_id": "oc_team"},
            {"message_id": "om_2", "chat_id": "oc_secret"},
            {"meta_data": {"chat_id": "oc_secret", "message_id": "om_3"}},
            "not-a-dict",
        ],
        "has_more": True,
    }
    shown = await chat_guard.screen("im.mget", rules, data, kind_of)
    assert shown["items"] == [{"message_id": "om_1", "chat_id": "oc_team"}, "not-a-dict"]
    assert shown["has_more"] is True
    assert await chat_guard.screen("calendar.events", rules, data, kind_of) is data
    # 合并转发：看最外层那条在哪个会话；里面转进来的子消息不管出处。
    forward = {"items": [{"chat_id": "oc_team"}, {"chat_id": "oc_secret"}]}
    assert await chat_guard.screen("im.get", rules, forward, kind_of) is forward
    with pytest.raises(service.PersonalError) as caught:
        await chat_guard.screen("im.get", rules, {"items": [{"chat_id": "oc_secret"}]}, kind_of)
    assert caught.value.code == "chat_not_managed"


async def test_group_only_drops_direct_chats_by_kind() -> None:
    rules = _rules(message_range="group")
    chats = {"items": [{"chat_id": "oc_li", "chat_mode": "p2p"}, {"chat_id": "oc_team"}]}
    shown = await chat_guard.screen("im.chats", rules, chats, kind_of)
    assert shown["items"] == [{"chat_id": "oc_team"}]
    messages = {"items": [{"chat_id": "oc_li"}, {"chat_id": "oc_team"}]}
    shown = await chat_guard.screen("im.mget", rules, messages, kind_of)
    assert shown["items"] == [{"chat_id": "oc_team"}]
    p2p_only = _rules(message_range="p2p")
    shown = await chat_guard.screen("im.mget", p2p_only, messages, kind_of)
    assert shown["items"] == [{"chat_id": "oc_li"}]


async def _check(
    key: str,
    path_values: dict[str, str] | None = None,
    *,
    rules: Any,
    params: Any = None,
    json: Any = None,
    chats: dict[str, Any] | None = None,
) -> list[str]:
    asked: list[str] = []

    async def resolve(message_id: str) -> Any:
        asked.append(message_id)
        return (chats or {}).get(message_id)

    endpoint = endpoints.ENDPOINTS[key]
    path = endpoints.build(endpoint, **(path_values or {}))
    await chat_guard.check(endpoint, path, rules, params, json, resolve, kind_of)
    return asked


@pytest.mark.parametrize(
    ("key", "path_values", "params", "json"),
    [
        ("im.history", None, {"container_id_type": "chat", "container_id": "oc_secret"}, None),
        ("im.chat", {"chat_id": "oc_secret"}, None, None),
        ("im.chat_members", {"chat_id": "oc_secret"}, None, None),
        ("im.announcement", {"chat_id": "oc_secret"}, None, None),
        ("im.chat_add_members", {"chat_id": "oc_secret"}, None, {"id_list": ["ou_1"]}),
        ("im.send", None, {"receive_id_type": "chat_id"}, {"receive_id": "oc_secret"}),
        (
            "im.forward",
            {"message_id": "om_ok"},
            {"receive_id_type": "chat_id"},
            {"receive_id": "oc_secret"},
        ),
        ("im.search", None, None, {"query": "x", "filter": {"chat_ids": ["oc_secret"]}}),
    ],
)
async def test_named_unmanaged_chat_is_refused(
    key: str, path_values: dict[str, str] | None, params: Any, json: Any
) -> None:
    rules = _rules(ignored=("oc_secret",))
    with pytest.raises(service.PersonalError) as caught:
        await _check(
            key, path_values, rules=rules, params=params, json=json, chats={"om_ok": "oc_team"}
        )
    assert caught.value.code == "chat_not_managed"


@pytest.mark.parametrize(
    ("key", "path_values", "json"),
    [
        ("im.reply", {"message_id": "om_1"}, None),
        ("im.recall", {"message_id": "om_1"}, None),
        ("im.reaction", {"message_id": "om_1"}, None),
        ("im.reaction_delete", {"message_id": "om_1", "reaction_id": "r1"}, None),
        ("im.read_users", {"message_id": "om_1"}, None),
        ("im.edit", {"message_id": "om_1"}, None),
        ("im.resource", {"message_id": "om_1", "file_key": "file_1"}, None),
        ("im.pin", None, {"message_id": "om_1"}),
        ("im.reactions", None, {"queries": [{"message_id": "om_ok"}, {"message_id": "om_1"}]}),
    ],
)
async def test_message_only_requests_are_resolved_first(
    key: str, path_values: dict[str, str] | None, json: Any
) -> None:
    rules = _rules(ignored=("oc_secret",))
    chats = {"om_1": "oc_secret", "om_ok": "oc_team"}
    with pytest.raises(service.PersonalError):
        await _check(key, path_values, rules=rules, json=json, chats=chats)
    chats["om_1"] = "oc_team"
    asked = await _check(key, path_values, rules=rules, json=json, chats=chats)
    assert "om_1" in asked


async def test_unknown_chat_is_refused_only_in_listed_mode() -> None:
    values = {"message_id": "om_1"}
    await _check("im.reply", values, rules=_rules(ignored=("oc_secret",)), chats={})
    with pytest.raises(service.PersonalError):
        await _check("im.reply", values, rules=_rules(listed=True, managed=("oc_team",)))


async def test_open_id_targets_and_other_domains_pass_untouched() -> None:
    rules = _rules(listed=True, managed=("oc_team",))
    # 以 open_id 给人发新消息：发之前说不出会落在哪个单聊，放行（所有者已在审批单上批过）。
    await _check(
        "im.send", rules=rules, params={"receive_id_type": "open_id"}, json={"receive_id": "ou_1"}
    )
    assert await _check("im.history", rules=rules, params={"container_id_type": "thread"}) == []
    assert await _check("im.chats", rules=rules) == []
    assert await _check("calendar.primary", rules=rules) == []


async def test_group_only_refuses_direct_chats_and_sending_to_a_person() -> None:
    rules = _rules(message_range="group")
    with pytest.raises(service.PersonalError):
        await _check(
            "im.history", rules=rules, params={"container_id_type": "chat", "container_id": "oc_li"}
        )
    with pytest.raises(service.PersonalError):
        await _check("im.reply", {"message_id": "om_1"}, rules=rules, chats={"om_1": "oc_li"})
    with pytest.raises(service.PersonalError):
        await _check(
            "im.send",
            rules=rules,
            params={"receive_id_type": "open_id"},
            json={"receive_id": "ou_1"},
        )
    with pytest.raises(service.PersonalError):
        # 查不到消息在哪个会话：范围有限制时不做。
        await _check("im.reply", {"message_id": "om_x"}, rules=rules, chats={})
    await _check("im.reply", {"message_id": "om_1"}, rules=rules, chats={"om_1": "oc_team"})
    await _check(
        "im.history", rules=rules, params={"container_id_type": "chat", "container_id": "oc_team"}
    )


# ---- 接在个人令牌的入口上 ----

GRANT = SimpleNamespace(app_id="cli_x", message_range="all")


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`api_request` 跳过授权与限频，记下发往飞书的请求，按路径给出应答。"""
    state: dict[str, Any] = {"rules": None, "sent": [], "answers": {}}

    async def authorize(_s: Any, _c: Any, _scope: Any, method: str, path: str) -> Any:
        endpoint = endpoints.match(method, path)
        assert endpoint is not None
        if write_fuse.blocked(_scope.bot, endpoint.kind):
            raise service.PersonalError("approval_required")
        return endpoint, GRANT, "app-secret-x9", {"access_token": "token-x9"}

    async def rules(_session: Any, bot: Any, _range: str = "all") -> Any:
        return state["rules"] if bot.kind == "assistant" else None

    async def http(method: str, url: str, **_: Any) -> dict[str, Any]:
        path = url.removeprefix(service.BASE)
        state["sent"].append((method, path))
        return {"code": 0, "data": state["answers"].get((method, path), {})}

    class Budget:
        def __init__(self, *_: Any) -> None: ...

        async def record(self, *_: Any) -> None: ...

    monkeypatch.setattr(service, "_authorize", authorize)
    monkeypatch.setattr(chat_guard, "rules", rules)
    monkeypatch.setattr(service, "_http", http)
    monkeypatch.setattr(service.ratelimit, "Budget", Budget)
    return state


def _scope(kind: str = "assistant") -> Any:
    return SimpleNamespace(bot=SimpleNamespace(kind=kind, id=uuid.uuid4()))


async def _request(scope: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
    return await service.api_request(None, None, scope, method, path, **kw)  # type: ignore[arg-type]


async def test_entry_filters_reads_and_refuses_before_writing(entry: dict[str, Any]) -> None:
    entry["rules"] = _rules(ignored=("oc_secret",))
    entry["answers"][("GET", "/im/v1/chats")] = {
        "items": [{"chat_id": "oc_team"}, {"chat_id": "oc_secret"}]
    }
    entry["answers"][("GET", "/im/v1/messages/om_1")] = {"items": [{"chat_id": "oc_secret"}]}
    listed = await _request(_scope(), "GET", "/im/v1/chats")
    assert listed["items"] == [{"chat_id": "oc_team"}]
    scope = _scope()
    with write_fuse.allow(scope.bot.id):
        with pytest.raises(service.PersonalError) as caught:
            await _request(scope, "POST", "/im/v1/messages/om_1/reply", json={"msg_type": "text"})
    assert caught.value.code == "chat_not_managed"
    # 只查了那条消息在哪个会话，回复没有发出去。
    assert ("POST", "/im/v1/messages/om_1/reply") not in entry["sent"]
    assert ("GET", "/im/v1/messages/om_1") in entry["sent"]


async def test_entry_leaves_everyone_else_alone(entry: dict[str, Any]) -> None:
    entry["answers"][("GET", "/im/v1/chats")] = {"items": [{"chat_id": "oc_secret"}]}
    entry["rules"] = _rules(ignored=("oc_secret",))
    shown = await _request(_scope("standard"), "GET", "/im/v1/chats")
    assert shown["items"] == [{"chat_id": "oc_secret"}]
    entry["rules"] = None
    shown = await _request(_scope(), "GET", "/im/v1/chats")
    assert shown["items"] == [{"chat_id": "oc_secret"}]


async def test_bypass_lets_the_owner_pick_from_every_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SimpleNamespace(
        chat_scope="all", ignored_chats_enc=["oc_secret"], managed_chats_enc=[], chat_id=OWN
    )

    class Session:
        async def get(self, *_: Any) -> Any:
            return row

    bot = SimpleNamespace(kind="assistant", id=uuid.uuid4())
    assert await chat_guard.rules(Session(), bot) is not None  # type: ignore[arg-type]
    with chat_guard.bypass():
        assert await chat_guard.rules(Session(), bot) is None  # type: ignore[arg-type]
    assert await chat_guard.rules(Session(), bot) is not None  # type: ignore[arg-type]
