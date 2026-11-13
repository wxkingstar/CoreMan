"""盯消息的读取：顺序对比找出有新动静的会话，读历史的参数，消息解析与游标。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from coreman.core.assistants import watch_feed as feed
from coreman.core.feishu_personal.service import PersonalError


class Reader:
    """`toolbase.Call` 的替身：按接口名依次给出准备好的应答，记下每次调用。"""

    def __init__(self, **answers: list[Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str] | None, dict[str, Any] | None]] = []

    async def __call__(
        self, key: str, *, path: Any = None, params: Any = None, json: Any = None
    ) -> dict[str, Any]:
        self.calls.append((key, path, params))
        answer = self.answers[key].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]


def raw(mid: str, ms: int, **extra: Any) -> dict[str, Any]:
    return {
        "message_id": mid,
        "chat_id": "oc_team",
        "msg_type": "text",
        "create_time": str(ms),
        "sender": {"id": "ou_li", "id_type": "open_id", "sender_type": "user"},
        "body": {"content": json.dumps({"text": "在吗"}, ensure_ascii=False)},
        **extra,
    }


@pytest.mark.parametrize(
    ("order", "prev", "moved", "rest"),
    [
        # 顺序没变：没有挪动过的，后缀从第一个开始探。
        (["a", "b", "c"], ["a", "b", "c"], [], ["a", "b", "c"]),
        # b 来了消息，排到了前面。
        (["b", "a", "c"], ["a", "b", "c"], ["b"], ["a", "c"]),
        # c 来了消息，随后队首 a 也来了：a 没挪位置，但排在挪动过的 c 前面，一起读。
        (["a", "c", "b"], ["a", "b", "c"], ["a", "c"], ["b"]),
        # 没见过的会话排在最前：新动静。
        (["x", "a", "b"], ["a", "b", "c"], ["x"], ["a", "b"]),
        # 上面有会话退出，末尾补进来一个没见过的旧会话：不算新动静。
        (["a", "c", "d"], ["a", "b", "c"], [], ["a", "c", "d"]),
        # 没见过的会话夹在中间：留在后缀里，由逐个探测决定读不读。
        (["a", "x", "b"], ["a", "b"], [], ["a", "x", "b"]),
        # 上一轮什么都没有：全算挪动过。
        (["a", "b"], [], ["a", "b"], []),
        ([], ["a"], [], []),
    ],
)
def test_split_finds_the_chats_that_moved_up(
    order: list[str], prev: list[str], moved: list[str], rest: list[str]
) -> None:
    assert feed.split(order, prev) == (moved, rest)


async def test_list_chats_asks_once_and_tolerates_odd_items() -> None:
    reader = Reader(
        **{
            "im.chats": [
                {
                    "items": [
                        {"chat_id": "oc_li", "chat_mode": "p2p"},
                        {"chat_id": "oc_team", "name": "项目群", "chat_mode": "group"},
                        {"chat_id": "oc_topic", "chat_mode": "topic"},
                        {"chat_id": "oc_plain", "name": "没带类型"},
                        {"chat_id": "oc_team", "name": "重复的"},
                        {"name": "没有编号"},
                        "不是对象",
                    ]
                }
            ]
        }
    )
    listed = await feed.list_chats(reader)
    assert listed.order == ["oc_li", "oc_team", "oc_topic", "oc_plain"]
    assert listed.names["oc_team"] == "项目群" and listed.names["oc_li"] == ""
    assert listed.kinds == {"oc_li": "p2p", "oc_team": "group", "oc_topic": "group"}
    assert reader.calls == [
        (
            "im.chats",
            None,
            {
                "user_id_type": "open_id",
                "sort_type": "ByActiveTimeDesc",
                "page_size": 50,
                "types": "p2p,group",
            },
        )
    ]
    assert (await feed.list_chats(Reader(**{"im.chats": [{}]}))).order == []


def test_to_item_reads_time_mentions_and_thread() -> None:
    item = feed.to_item(
        raw(
            "om_1",
            1_700_000_000_123,
            thread_id="omt_1",
            mentions=[{"key": "@_user_1", "id": "ou_owner", "id_type": "open_id"}, "坏的"],
        ),
        chat_id="oc_team",
        chat_kind="group",
        chat_name="项目群",
    )
    assert item is not None
    assert (item.message_id, item.at_ms, item.thread_id) == ("om_1", 1_700_000_000_123, "omt_1")
    assert (item.sender_id, item.sender_type, item.msg_type) == ("ou_li", "user", "text")
    assert item.text == "在吗" and item.mentions == frozenset({"ou_owner"})
    assert (item.chat_id, item.chat_kind, item.chat_name) == ("oc_team", "group", "项目群")
    in_thread = feed.to_item(
        raw("om_2", 5), chat_id="oc_team", chat_kind="group", chat_name="", thread_id="omt_9"
    )
    assert in_thread is not None and in_thread.thread_id == "omt_9"
    gone = feed.to_item(raw("om_3", 5, deleted=True), chat_id="c", chat_kind="p2p", chat_name="")
    assert gone is not None and gone.deleted
    # 没有编号或读不出时间的不要。
    for bad in (raw("", 5), raw("om_4", 0), {**raw("om_5", 5), "create_time": "昨天"}, {}):
        assert feed.to_item(bad, chat_id="c", chat_kind="p2p", chat_name="") is None


async def test_odd_shapes_from_feishu_are_skipped_not_raised() -> None:
    """字段缺了、类型不对：按没有处理，不让一条坏数据废掉整轮。"""
    for odd in ({"items": None}, {"items": "x"}, {"items": 3}, {"items": {"a": 1}}, {}):
        assert (await feed.list_chats(Reader(**{"im.chats": [odd]}))).order == []
        assert await feed.read_main(Reader(**{"im.history": [odd]}), "oc_x", 0) == ([], 1)
        assert await feed.read_thread(Reader(**{"im.history": [odd]}), "omt_x") == []
        assert await feed.recalled(Reader(**{"im.mget": [odd]}), ["om_1"]) == []
    for mentions in (7, "ou_owner", {"id": "ou_owner"}, [None, 3, {"id": 5}, {"key": "@_all"}]):
        item = feed.to_item(
            raw("om_1", 5, mentions=mentions), chat_id="c", chat_kind="group", chat_name=""
        )
        assert item is not None and item.mentions == frozenset()
    nested = feed.to_item(
        raw("om_1", 5, mentions=[{"id": {"open_id": "ou_owner", "user_id": "u1"}}]),
        chat_id="c",
        chat_kind="group",
        chat_name="",
    )
    assert nested is not None and nested.mentions == frozenset({"ou_owner"})
    # 发送人、正文不是预期的形状：照样成条，缺的按空。
    bare = feed.to_item(
        {"message_id": "om_2", "create_time": 9, "sender": "x", "body": None},
        chat_id="c",
        chat_kind="p2p",
        chat_name="",
    )
    assert bare is not None and (bare.sender_id, bare.sender_type, bare.at_ms) == ("", "", 9)
    assert await feed.chat_kind(Reader(**{"im.chat": [{"chat_mode": 3, "name": None}]}), "c") == (
        "group",
        "",
    )


def test_fresh_uses_milliseconds_and_the_ids_at_the_cursor() -> None:
    def at(mid: str, ms: int) -> Any:
        return feed.to_item(raw(mid, ms), chat_id="c", chat_kind="p2p", chat_name="")

    assert feed.fresh(100, ["om_1"], at("om_2", 101))
    assert feed.fresh(100, ["om_1"], at("om_2", 100))
    assert not feed.fresh(100, ["om_1"], at("om_1", 100))
    assert not feed.fresh(100, [], at("om_0", 99))


async def test_read_main_starts_at_the_cursor_second_and_follows_one_more_page() -> None:
    reader = Reader(
        **{
            "im.history": [
                {"items": [raw("om_1", 1000), "坏的"], "has_more": True, "page_token": "p2"},
                {"items": [raw("om_2", 2000)], "has_more": True, "page_token": "p3"},
            ]
        }
    )
    items, calls = await feed.read_main(reader, "oc_team", 1_700_000_000_999)
    assert [item["message_id"] for item in items] == ["om_1", "om_2"] and calls == 2
    first = {
        "container_id_type": "chat",
        "container_id": "oc_team",
        "start_time": "1700000000",
        "sort_type": "ByCreateTimeAsc",
        "page_size": 50,
    }
    assert reader.calls == [
        ("im.history", None, first),
        ("im.history", None, {**first, "page_token": "p2"}),
    ]
    one, calls = await feed.read_main(Reader(**{"im.history": [{"items": []}]}), "oc_x", 0)
    assert one == [] and calls == 1


async def test_read_thread_takes_the_latest_page_without_a_time_range() -> None:
    reader = Reader(**{"im.history": [{"items": [raw("om_3", 3000), raw("om_2", 2000)]}]})
    items = await feed.read_thread(reader, "omt_1")
    assert [item["message_id"] for item in items] == ["om_2", "om_3"]
    assert reader.calls == [
        (
            "im.history",
            None,
            {
                "container_id_type": "thread",
                "container_id": "omt_1",
                "sort_type": "ByCreateTimeDesc",
                "page_size": 20,
            },
        )
    ]


async def test_chat_kind_asks_the_chat_itself() -> None:
    reader = Reader(**{"im.chat": [{"chat_mode": "p2p"}, {"chat_mode": "group", "name": "项目群"}]})
    assert await feed.chat_kind(reader, "oc_li") == ("p2p", "")
    assert await feed.chat_kind(reader, "oc_team") == ("group", "项目群")
    assert reader.calls[0] == ("im.chat", {"chat_id": "oc_li"}, {"user_id_type": "open_id"})


async def test_recalled_checks_at_most_twenty_ids() -> None:
    ids = [f"om_{n}" for n in range(25)]
    answer = {"items": [raw("om_1", 5, deleted=True), raw("om_2", 5), {"deleted": True}]}
    reader = Reader(**{"im.mget": [answer]})
    assert await feed.recalled(reader, ids) == ["om_1"]
    assert reader.calls == [("im.mget", None, {"message_ids": ids[:20]})]
    assert await feed.recalled(Reader(), []) == []
    # 一条都读不到（不可见、已不存在）不算撤回；别的错误照抛。
    unreadable = Reader(**{"im.mget": [PersonalError("feishu_read_failed")]})
    assert await feed.recalled(unreadable, ["om_1"]) == []
    with pytest.raises(PersonalError):
        await feed.recalled(Reader(**{"im.mget": [PersonalError("authorization_required")]}), ["x"])


def test_cursor_round_trip_copies_and_prunes() -> None:
    stored = {
        "since": 5,
        "chats": {"oc_a": {"t": 9, "m": ["om_1"]}, "bad": "x"},
        "todo": ["oc_b", 3],
    }
    cursor = feed.load(stored)
    assert cursor == {"since": 5, "chats": {"oc_a": {"t": 9, "m": ["om_1"]}}, "todo": ["oc_b"]}
    cursor["chats"]["oc_a"]["t"] = 10
    assert stored["chats"]["oc_a"]["t"] == 9  # 改的是副本
    assert feed.load({}) == {"since": 0, "chats": {}, "todo": []}
    many = {"since": 1, "chats": {f"oc_{n}": {"t": n} for n in range(250)}, "todo": []}
    kept = feed.dump(many, ["oc_z"] * 80)
    assert len(kept["chats"]) == 200 and "oc_249" in kept["chats"] and "oc_49" not in kept["chats"]
    assert kept["since"] == 1 and len(kept["todo"]) == 50
