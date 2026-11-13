"""提交前检查：平台重新读会话，按锚点之后有没有新消息决定能不能提交。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from coreman.core.assistants.approvals import checks
from coreman.core.feishu_personal.service import PersonalError

OWNER = "ou_zhang"
PEER = "ou_li"


def msg(mid: str, sender: str = PEER, text: str = "在吗", **kw: Any) -> dict[str, Any]:
    return {
        "message_id": mid,
        "chat_id": kw.pop("chat_id", "oc_1"),
        "msg_type": "text",
        "sender": {"id": sender, "id_type": "open_id", "sender_type": kw.pop("kind", "user")},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        **kw,
    }


class Reader:
    """`toolbase.Call` 的替身：按接口名返回准备好的数据，并记下调用。"""

    def __init__(self, history: list[dict[str, Any]], *, mode: str = "p2p") -> None:
        self.history, self.mode = history, mode
        self.messages = {item["message_id"]: item for item in history}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, key: str, *, path: Any = None, params: Any = None, json: Any = None
    ) -> dict[str, Any]:
        self.calls.append((key, {"path": path, "params": params}))
        if key == "im.chat":
            return {"chat_mode": self.mode, "name": "" if self.mode == "p2p" else "项目群"}
        if key == "im.history":
            return {"items": self.history}
        if key == "im.mget":
            found = self.messages.get(params["message_ids"][0])
            if found is None:
                raise PersonalError("feishu_read_failed")
            return {"items": [found]}
        if key == "contact.user":
            return {"user": {"name": {PEER: "李四", OWNER: "张三"}.get(path["user_id"], "")}}
        raise AssertionError(key)


def reply(mid: str = "om_2", **meta: Any) -> dict[str, Any]:
    arguments = {"message_id": mid, "text": "好的", "format": "text", "uuid": "u1"}
    return {"arguments": arguments, "chat_id": "oc_1", "anchor_message_id": None, **meta}


def send(chat_id: str = "oc_1", **meta: Any) -> dict[str, Any]:
    arguments = {"receive_id": chat_id, "receive_id_type": "chat_id", "text": "好", "uuid": "u"}
    return {"arguments": arguments, "chat_id": "oc_1", "anchor_message_id": None, **meta}


async def check(reader: Reader, tool: str, one: dict[str, Any]) -> Any:
    return await checks.check_call(reader, tool, one, owner_open_id=OWNER)


async def test_reply_to_the_latest_message_passes_with_the_facts_for_the_form() -> None:
    reader = Reader([msg("om_2", text="周五能给报价吗？"), msg("om_1", OWNER, "我看看")])
    facts = await check(reader, "feishu_reply_message", reply())
    assert (facts.channel, facts.target) == ("p2p", "李四")
    assert facts.quoted == "李四：周五能给报价吗？"
    # 没给锚点时，被回复的那条就是锚点。
    assert facts.anchor_message_id == "om_2"
    history = next(call for key, call in reader.calls if key == "im.history")
    assert history["params"]["container_id"] == "oc_1"
    assert history["params"]["sort_type"] == "ByCreateTimeDesc"


@pytest.mark.parametrize(
    ("history", "code"),
    [
        # 锚点之后本人已经回过。
        ([msg("om_3", OWNER, "我来回"), msg("om_2")], "owner_replied_after_anchor"),
        # 锚点之后对方又发了一条。
        ([msg("om_3", text="算了不用了"), msg("om_2")], "newer_messages_after_anchor"),
        # 两种都有时，以本人已回复为准。
        ([msg("om_4"), msg("om_3", OWNER), msg("om_2")], "owner_replied_after_anchor"),
        # 锚点不在最近的消息里。
        ([msg("om_9"), msg("om_8")], "message_gone"),
    ],
)
async def test_reply_is_rejected_when_the_chat_moved_on(
    history: list[dict[str, Any]], code: str
) -> None:
    with pytest.raises(checks.Rejected) as caught:
        await check(Reader(history), "feishu_reply_message", reply())
    assert caught.value.code == code and caught.value.hint


async def test_bot_notices_and_recalled_messages_do_not_count_as_new() -> None:
    history = [
        msg("om_4", "cli_example", kind="app"),
        msg("om_3", deleted=True),
        msg("om_2"),
    ]
    facts = await check(Reader(history), "feishu_reply_message", reply())
    assert facts.anchor_message_id == "om_2"


async def test_explicit_anchor_is_the_last_message_the_assistant_read() -> None:
    history = [msg("om_3", text="补充一句"), msg("om_2", text="周五能给报价吗？")]
    facts = await check(Reader(history), "feishu_reply_message", reply(anchor_message_id="om_3"))
    assert facts.anchor_message_id == "om_3" and facts.quoted == "李四：周五能给报价吗？"
    with pytest.raises(checks.Rejected) as caught:
        await check(Reader(history), "feishu_reply_message", reply(anchor_message_id="om_0"))
    assert caught.value.code == "anchor_not_found"


async def test_message_tools_need_a_chat_and_send_must_address_it() -> None:
    reader = Reader([msg("om_2")])
    with pytest.raises(checks.Rejected) as no_chat:
        await check(reader, "feishu_send_message", {**send(), "chat_id": None})
    assert no_chat.value.code == "chat_id_required"
    by_person = send()
    by_person["arguments"] = {**by_person["arguments"], "receive_id_type": "open_id"}
    with pytest.raises(checks.Rejected) as wrong:
        await check(reader, "feishu_send_message", by_person)
    assert wrong.value.code == "receive_id_must_be_chat"
    with pytest.raises(checks.Rejected) as elsewhere:
        await check(reader, "feishu_send_message", send("oc_other"))
    assert elsewhere.value.code == "receive_id_must_be_chat"
    # 都没读到会话之前就拒绝。
    assert reader.calls == []


async def test_send_needs_an_anchor_unless_the_chat_is_empty() -> None:
    with pytest.raises(checks.Rejected) as caught:
        await check(Reader([msg("om_2")]), "feishu_send_message", send())
    assert caught.value.code == "anchor_required"
    facts = await check(Reader([]), "feishu_send_message", send())
    assert facts.anchor_message_id is None and facts.target == "对方未发言的私聊"
    group = await check(
        Reader([msg("om_2")], mode="group"), "feishu_send_message", send(anchor_message_id="om_2")
    )
    assert (group.channel, group.target, group.quoted) == ("group", "项目群", "李四：在吗")


async def test_reading_a_thread_reads_that_thread() -> None:
    reader = Reader([msg("om_2")])
    await check(reader, "feishu_reply_message", reply(thread_id="omt_1"))
    history = next(call for key, call in reader.calls if key == "im.history")
    assert history["params"]["container_id_type"] == "thread"
    assert history["params"]["container_id"] == "omt_1"


async def test_forward_reads_the_source_wherever_it_is() -> None:
    reader = Reader([msg("om_2")])
    reader.messages["om_src"] = msg("om_src", text="会议纪要", chat_id="oc_other")
    one = send(anchor_message_id="om_2")
    one["arguments"] = {
        "message_id": "om_src", "receive_id": "oc_1", "receive_id_type": "chat_id", "uuid": "u"
    }  # fmt: skip
    facts = await check(reader, "feishu_forward_message", one)
    assert facts.quoted == "李四：会议纪要"
    one["arguments"]["message_id"] = "om_missing"
    with pytest.raises(checks.Rejected) as caught:
        await check(reader, "feishu_forward_message", one)
    assert caught.value.code == "message_gone"


async def test_editing_and_recalling_only_touch_own_live_messages() -> None:
    reader = Reader([msg("om_2"), msg("om_1", OWNER, "我发的")])
    one = {"arguments": {"message_id": "om_1"}, "chat_id": "oc_1"}
    facts = await check(reader, "feishu_recall_message", one)
    assert facts.quoted == "张三：我发的" and facts.anchor_message_id == "om_1"
    with pytest.raises(checks.Rejected) as not_mine:
        await check(reader, "feishu_recall_message", {**one, "arguments": {"message_id": "om_2"}})
    assert not_mine.value.code == "not_own_message"
    # 表情与置顶可以落在别人的消息上，但消息要在这个会话里。
    await check(reader, "feishu_pin_message", {**one, "arguments": {"message_id": "om_2"}})
    with pytest.raises(checks.Rejected) as elsewhere:
        await check(reader, "feishu_pin_message", {**one, "chat_id": "oc_other"})
    assert elsewhere.value.code == "message_gone"


async def test_other_tools_need_no_chat_and_read_nothing() -> None:
    reader = Reader([])
    facts = await check(reader, "feishu_task_create", {"arguments": {"summary": "对账"}})
    assert (facts.channel, facts.target, facts.quoted) == ("task", "", "")
    assert reader.calls == []


async def test_authorization_errors_are_not_swallowed() -> None:
    class Expired(Reader):
        async def __call__(self, key: str, **kw: Any) -> dict[str, Any]:
            raise PersonalError("authorization_required")

    with pytest.raises(PersonalError):
        await check(Expired([]), "feishu_reply_message", reply())


def test_rich_text_and_other_message_types_are_flattened() -> None:
    post = {
        "title": "周报",
        "content": [[{"tag": "text", "text": "本周"}, {"tag": "a", "text": "链接"}]],
    }
    item = msg("om_1")
    item["msg_type"], item["body"] = "post", {"content": json.dumps(post, ensure_ascii=False)}
    assert checks.parse(item).text == "周报 本周 链接"
    item["msg_type"], item["body"] = "image", {"content": "{}"}
    assert checks.parse(item).text == "[image消息]"


async def test_sending_a_file_follows_the_send_and_reply_rules() -> None:
    ref = {"key": "k", "filename": "报价.pdf", "mime": "application/pdf", "size": 3, "sha256": "f"}
    arguments = {"receive_id": "oc_1", "receive_id_type": "chat_id", "upload_id": ref, "uuid": "u"}
    one = {"arguments": arguments, "chat_id": "oc_1", "anchor_message_id": None}
    with pytest.raises(checks.Rejected) as caught:
        await check(Reader([msg("om_2")]), "feishu_send_file", one)
    assert caught.value.code == "anchor_required"
    facts = await check(
        Reader([msg("om_2")]), "feishu_send_file", {**one, "anchor_message_id": "om_2"}
    )
    assert (facts.channel, facts.target, facts.anchor_message_id) == ("p2p", "李四", "om_2")
    # 作为回复发出：被回复的那条就是锚点，之后有新消息同样拒绝。
    replying = {**one, "arguments": {**arguments, "reply_to_message_id": "om_2"}}
    answered = await check(Reader([msg("om_2")]), "feishu_send_file", replying)
    assert answered.anchor_message_id == "om_2" and answered.quoted == "李四：在吗"
    with pytest.raises(checks.Rejected) as moved:
        await check(Reader([msg("om_3", text="算了"), msg("om_2")]), "feishu_send_file", replying)
    assert moved.value.code == "newer_messages_after_anchor"


def group(history: list[dict[str, Any]]) -> Reader:
    return Reader(history, mode="group")


def at_owner() -> dict[str, Any]:
    return {"mentions": [{"key": "@_user_1", "id": OWNER, "id_type": "open_id", "name": "张三"}]}


async def test_group_chatter_after_the_anchor_does_not_make_a_draft_stale() -> None:
    """群里别人接着聊别的：不算这个话头动过。私聊不变（对方每句话都是对所有者说的）。"""
    chatter = [
        msg("om_5", "ou_wang", "中午吃什么"),
        msg("om_4", "ou_zhao", "+1"),
        msg("om_3", "ou_wang", "@所有人 周五团建", mentions=[{"key": "@_all", "id": "all"}]),
        msg("om_2", PEER, "周五能给报价吗？"),
    ]
    facts = await check(group(chatter), "feishu_reply_message", reply())
    assert (facts.channel, facts.anchor_message_id) == ("group", "om_2")
    with pytest.raises(checks.Rejected) as caught:
        await check(Reader(chatter), "feishu_reply_message", reply())
    assert caught.value.code == "newer_messages_after_anchor"


@pytest.mark.parametrize(
    ("newer", "code"),
    [
        # 有人 @ 了所有者。
        (msg("om_3", "ou_wang", "@张三 这个你看下", **at_owner()), "newer_messages_after_anchor"),
        # 有人回复（引用）了锚点那条消息。
        (msg("om_3", "ou_wang", "同问", parent_id="om_2"), "newer_messages_after_anchor"),
        # 话题里的回复：根是锚点。
        (
            msg("om_3", "ou_wang", "同问", parent_id="om_x", root_id="om_2"),
            "newer_messages_after_anchor",
        ),
        # 锚点的发送人自己又说了一句。
        (msg("om_3", PEER, "算了不用了"), "newer_messages_after_anchor"),
        # 所有者亲自回了：不管 @ 没 @、引没引用。
        (msg("om_3", OWNER, "我来回"), "owner_replied_after_anchor"),
    ],
)
async def test_group_messages_about_the_anchor_still_make_a_draft_stale(
    newer: dict[str, Any], code: str
) -> None:
    history = [msg("om_4", "ou_zhao", "中午吃什么"), newer, msg("om_2", PEER, "周五能给报价吗？")]
    with pytest.raises(checks.Rejected) as caught:
        await check(group(history), "feishu_reply_message", reply())
    assert caught.value.code == code


async def test_a_recalled_anchor_in_a_group_is_still_gone() -> None:
    history = [msg("om_3", "ou_wang", "中午吃什么"), msg("om_2", PEER, deleted=True)]
    with pytest.raises(checks.Rejected) as caught:
        await check(group(history), "feishu_reply_message", reply())
    assert caught.value.code == "message_gone"


def test_group_replies_from_bots_and_recalled_or_platform_sent_ones_do_not_count() -> None:
    view = checks.ChatView(
        "group",
        "项目群",
        [
            checks.parse(msg("om_6", "cli_example", "通知", kind="app", parent_id="om_2")),
            checks.parse(msg("om_5", PEER, "撤回的", deleted=True)),
            checks.parse(msg("om_4", OWNER, "平台代发的")),
            checks.parse(msg("om_2", PEER)),
        ],
    )
    assert checks.anchor_verdict(view, "om_2", OWNER, {"om_4"}) is None
    assert checks.anchor_verdict(view, "om_2", OWNER) == "owner_replied_after_anchor"
    assert checks.anchor_verdict(view, "om_1", OWNER) == "anchor_not_found"


@pytest.mark.parametrize("thread_id", ["omt_1", None])
async def test_a_third_persons_reply_in_the_same_topic_makes_a_draft_stale(
    thread_id: str | None,
) -> None:
    """锚点是话题里的一条非根消息：同话题里别人的回复（没 @、不是回复锚点）也算这个话头动过。"""
    history = [
        msg("om_4", "ou_wang", "周五可以，报价在共享盘", parent_id="om_1", root_id="om_1"),
        msg("om_3", PEER, "周五能给报价吗？", parent_id="om_1", root_id="om_1"),
        msg("om_1", "ou_zhao", "报价的事在这里说"),
    ]
    with pytest.raises(checks.Rejected) as caught:
        await check(group(history), "feishu_reply_message", reply("om_3", thread_id=thread_id))
    assert caught.value.code == "newer_messages_after_anchor"
    # 没有新回复时照常通过。
    facts = await check(
        group(history[1:]), "feishu_reply_message", reply("om_3", thread_id="omt_1")
    )
    assert facts.anchor_message_id == "om_3"


async def test_everything_in_a_topic_view_counts_like_a_private_chat() -> None:
    # 话题视图里读到的都是这个话题里的：飞书没给 root_id 也一样算。
    history = [msg("om_4", "ou_wang", "我来答"), msg("om_3", PEER, "周五能给报价吗？")]
    with pytest.raises(checks.Rejected) as caught:
        await check(group(history), "feishu_reply_message", reply("om_3", thread_id="omt_1"))
    assert caught.value.code == "newer_messages_after_anchor"
    assert (await check(group(history), "feishu_reply_message", reply("om_3"))).channel == "group"
