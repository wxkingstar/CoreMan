"""飞书引用补全：本 bot 的回复从存档与投递记录恢复，其余消息走官方接口（原始卡片、图片、
文件、合并转发），群里读不到时用收到过的入站事件兜底；只有本会话的父消息可见。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD
from coreman.core.db.models import (
    FeishuDelivery,
    FeishuSentMessage,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
    User,
    UserIdentity,
)
from coreman.core.i18n.messages import msg
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.platforms.feishu_content import UPGRADE_PLACEHOLDER
from coreman.core.platforms.feishu_media import FeishuMediaFetcher
from coreman.core.wecom.media import MediaError
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import seed_bot

PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 30


async def _feishu_bot(session: AsyncSession):  # type: ignore[no-untyped-def]
    bot, _relay, cipher = await seed_bot(session)
    bot.platform = "feishu"
    bot.credentials_enc = cipher.encrypt(
        json.dumps({"app_id": "cli_quote", "app_secret": "secret"}), CREDENTIALS_AAD
    )
    await session.commit()
    return bot


async def _set_parent(session: AsyncSession, task: Task, parent_id: object, chat_id: str) -> None:
    event = await session.get(InboundEvent, task.inbound_event_id)
    assert event is not None
    event.reply_context = {
        **event.reply_context,
        "chat_id": chat_id,
        "message_id": event.platform_msg_id,
        "parent_id": parent_id,
    }
    await session.commit()


async def _delivered_card(
    session: AsyncSession,
    *,
    bot_id,
    message_id: str,
    chat_id: str,
    final_text: str,
) -> None:  # type: ignore[no-untyped-def]
    task = Task(bot_id=bot_id, kind="chat", session_key=chat_id, payload={}, status="succeeded")
    session.add(task)
    await session.flush()
    session.add(
        TaskStream(
            task_id=task.id,
            bot_id=bot_id,
            platform="feishu",
            stream_id=f"old-{task.id}",
            reply_context={"chat_id": chat_id},
            lease_generation=1,
            running_since=datetime.now(UTC),
            final_text=final_text,
            thinking_md="绝不能进入引用的思考内容",
            is_complete=True,
        )
    )
    await session.flush()
    session.add(FeishuDelivery(task_id=task.id, message_id=message_id))
    await session.commit()


async def test_same_chat_delivered_card_is_quoted_after_session_reset(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
) -> None:
    """删掉会话记忆仍从 delivery→stream.final_text 恢复，且绝不读取 thinking_md。"""
    bot = await _feishu_bot(db_session)
    await _delivered_card(
        db_session,
        bot_id=bot.id,
        message_id="om_parent",
        chat_id="oc_same",
        final_text="父卡片的最终答案",
    )
    task = await chat_task(db_session, bot, "继续解释", chat_id="oc_same")
    await _set_parent(db_session, task, "om_parent", "oc_same")

    async def no_http(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("persisted quote must not call Feishu")

    monkeypatch.setattr(FeishuClient, "call", no_http)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)

    content = relay.requests[0]["messages"][1]["content"]
    assert content == msg("quote_text_prefix", quoted="父卡片的最终答案", text="继续解释")
    assert "思考内容" not in content


async def test_sent_outbox_markdown_is_quoted_without_http(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
) -> None:
    bot = await _feishu_bot(db_session)
    db_session.add(
        OutboxItem(
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="quote-outbox",
            target={"chat_id": "oc_same"},
            payload={"markdown": "选项卡后续说明", "_feishu_message_id": "om_choice"},
            status="sent",
        )
    )
    await db_session.commit()
    task = await chat_task(db_session, bot, "为什么", chat_id="oc_same")
    await _set_parent(db_session, task, "om_choice", "oc_same")

    async def no_http(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("sent outbox quote must not call Feishu")

    monkeypatch.setattr(FeishuClient, "call", no_http)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert relay.requests[0]["messages"][1]["content"] == msg(
        "quote_text_prefix", quoted="选项卡后续说明", text="为什么"
    )


async def test_deleted_parent_gets_explicit_unavailable_quote(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
) -> None:
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "继续", chat_id="oc_same")
    await _set_parent(db_session, task, "om_deleted", "oc_same")

    async def deleted(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        raise FeishuError(230002, "deleted")

    monkeypatch.setattr(FeishuClient, "call", deleted)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert relay.requests[0]["messages"][1]["content"] == "[引用消息内容不可用]\n\n继续"


@pytest.mark.parametrize(
    ("sender_type", "sender_id"),
    [("user", "ou_human"), ("app", "cli_other_bot")],
)
async def test_official_parent_read_accepts_any_sender_in_the_same_chat_and_is_bounded(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch,
    sender_type: str,
    sender_id: str,
) -> None:
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "概括", chat_id="oc_same")
    await _set_parent(db_session, task, "om_api", "oc_same")
    calls: list[tuple[str, str]] = []

    async def parent(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((method, path))
        return {
            "data": {
                "items": [
                    {
                        "message_id": "om_api",
                        "chat_id": "oc_same",
                        "msg_type": "interactive",
                        "sender": {"sender_type": sender_type, "id": sender_id},
                        "body": {
                            "content": json.dumps(
                                {
                                    "body": {
                                        "elements": [
                                            {"tag": "markdown", "content": "甲" * 20_000},
                                            {"tag": "img", "img_key": "do-not-fetch"},
                                        ]
                                    }
                                }
                            )
                        },
                    }
                ]
            }
        }

    monkeypatch.setattr(FeishuClient, "call", parent)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    content = relay.requests[0]["messages"][1]["content"]
    assert calls == [("GET", "/open-apis/im/v1/messages/om_api")]
    assert content == msg("quote_text_prefix", quoted="甲" * 12_000, text="概括")


@pytest.mark.parametrize("parent_id", [0, [], {}])
async def test_present_malformed_parent_is_explicitly_unavailable_without_http(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch,
    parent_id: object,
) -> None:
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "继续", chat_id="oc_same")
    await _set_parent(db_session, task, parent_id, "oc_same")

    async def no_http(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("malformed parent must not call Feishu")

    monkeypatch.setattr(FeishuClient, "call", no_http)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert relay.requests[0]["messages"][1]["content"] == "[引用消息内容不可用]\n\n继续"


async def test_empty_parent_is_normal_no_quote_without_http(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
) -> None:
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "普通消息", chat_id="oc_same")
    await _set_parent(db_session, task, "", "oc_same")

    async def no_http(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("empty no-parent marker must not call Feishu")

    monkeypatch.setattr(FeishuClient, "call", no_http)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert relay.requests[0]["messages"][1]["content"] == "普通消息"


async def test_cross_chat_parent_is_refused_even_when_api_returns_text(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
) -> None:
    bot = await _feishu_bot(db_session)
    await _delivered_card(
        db_session,
        bot_id=bot.id,
        message_id="om_parent",
        chat_id="oc_other",
        final_text="另一个群的机密",
    )
    task = await chat_task(db_session, bot, "引用了什么", chat_id="oc_same")
    await _set_parent(db_session, task, "om_parent", "oc_same")

    async def wrong_chat(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "data": {
                "items": [
                    {
                        "message_id": "om_parent",
                        "chat_id": "oc_other",
                        "msg_type": "text",
                        "sender": {"sender_type": "app", "id": "cli_quote"},
                        "body": {"content": json.dumps({"text": "API 里的机密"})},
                    }
                ]
            }
        }

    monkeypatch.setattr(FeishuClient, "call", wrong_chat)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    content = relay.requests[0]["messages"][1]["content"]
    assert content == "[引用消息内容不可用]\n\n引用了什么"
    assert "机密" not in content


def _api_message(
    message_id: str,
    msg_type: str,
    content: dict[str, Any],
    *,
    chat_id: str = "oc_same",
    sender_type: str = "user",
    sender_id: str = "ou_human",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "msg_type": msg_type,
        "sender": {"sender_type": sender_type, "id": sender_id},
        "body": {"content": json.dumps(content, ensure_ascii=False)},
        **extra,
    }


def _serve(monkeypatch, *items: dict[str, Any]) -> list[tuple[str, str, Any]]:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, Any]] = []

    async def parent(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((method, path, kwargs.get("params")))
        return {"data": {"items": list(items)}}

    monkeypatch.setattr(FeishuClient, "call", parent)
    return calls


def _serve_resources(monkeypatch) -> list[tuple[str, str, str]]:  # type: ignore[no-untyped-def]
    """资源下载的替身：照样执行 fetcher 的放行名单，只把真实 HTTP 换掉。"""
    fetched: list[tuple[str, str, str]] = []

    async def resource(self, ref, kind, budget):  # type: ignore[no-untyped-def]
        fetched.append((ref["message_id"], ref["file_key"], kind))
        if ref["message_id"] not in self.allowed:
            raise MediaError("download_failed")
        return (PNG if kind == "image" else b"quarterly"), httpx.Headers()

    monkeypatch.setattr(FeishuMediaFetcher, "_resource", resource)
    return fetched


GET_PARENT = {"card_msg_content_type": "user_card_content"}


@pytest.mark.parametrize("localized", [False, True])
@pytest.mark.parametrize("content_key", ["content", "content_v2"])
@pytest.mark.parametrize("same_chat", [False, True])
async def test_human_post_quote_keeps_text_and_images_in_order(
    db_engine, db_session, monkeypatch, localized, content_key, same_chat
):
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "概括", chat_id="oc_same")
    await _set_parent(db_session, task, "om_post", "oc_same")
    post = {
        "title": "标题",
        content_key: [
            [{"tag": "text", "text": "甲" * 20000}, {"tag": "img", "image_key": "img_q"}],
            [{"tag": "media", "file_key": "file_v"}],
        ],
    }
    calls = _serve(
        monkeypatch,
        _api_message(
            "om_post",
            "post",
            {"zh_cn": post} if localized else post,
            chat_id="oc_same" if same_chat else "oc_other",
        ),
    )
    fetched = _serve_resources(monkeypatch)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert calls == [("GET", "/open-apis/im/v1/messages/om_post", GET_PARENT)]
    content = relay.requests[0]["messages"][1]["content"]
    if not same_chat:
        assert content == "[引用消息内容不可用]\n\n概括" and fetched == []
        return
    # 文字总长封顶 12000；图片从被引用的那条消息下载，挂在文字后面。
    quoted = "标题\n" + "甲" * 11998 + " " + msg("media_image_placeholder")
    assert content[0] == {
        "type": "text",
        "text": msg("quote_text_prefix", quoted=quoted, text="概括"),
    }
    assert content[1]["image_url"]["url"].startswith("data:image/png;")
    assert fetched == [("om_post", "img_q", "image")]


async def test_card_parent_is_read_as_sent_and_never_includes_thinking(
    db_engine, db_session, monkeypatch
):
    """Card JSON 2.0：带 user_card_content 读原始卡片；思考面板不是回复正文。"""
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "解释一下", chat_id="oc_same")
    await _set_parent(db_session, task, "om_card", "oc_same")
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "日报"}},
        "body": {
            "elements": [
                {
                    "tag": "collapsible_panel",
                    "element_id": "thinking_panel",
                    "elements": [
                        {"tag": "markdown", "element_id": "thinking", "content": "内部思考"}
                    ],
                },
                {"tag": "markdown", "content": "订单 **1,024** 笔"},
                {
                    "tag": "table",
                    "columns": [
                        {"name": "c0", "display_name": "渠道", "data_type": "text"},
                        {"name": "c1", "display_name": "订单", "data_type": "number"},
                        {"name": "c2", "display_name": "负责人", "data_type": "persons"},
                    ],
                    "rows": [{"c0": "Web", "c1": 1024, "c2": ["ou_x"]}],
                },
                {
                    "tag": "chart",
                    "chart_spec": {"data": [{"id": "d", "values": [{"x": "周一", "y": 512}]}]},
                },
                {"tag": "button", "text": {"tag": "plain_text", "content": "看明细"}},
            ]
        },
    }
    calls = _serve(
        monkeypatch,
        _api_message("om_card", "interactive", card, sender_type="app", sender_id="cli_quote"),
    )
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert calls == [("GET", "/open-apis/im/v1/messages/om_card", GET_PARENT)]
    quoted = "\n".join(
        [
            "日报",
            "订单 **1,024** 笔",
            "| 渠道 | 订单 |",
            "| Web | 1024 |",
            "[图表数据]",
            '{"x": "周一", "y": 512}',
            "[按钮: 看明细]",
        ]
    )
    content = relay.requests[0]["messages"][1]["content"]
    assert content == msg("quote_text_prefix", quoted=quoted, text="解释一下")
    assert "内部思考" not in content


async def test_upgrade_placeholder_is_never_passed_off_as_the_quote(
    db_engine, db_session, monkeypatch
):
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "能看到吗", chat_id="oc_same")
    await _set_parent(db_session, task, "om_old", "oc_same")
    degraded = {"title": None, "elements": [[{"tag": "text", "text": UPGRADE_PLACEHOLDER}]]}
    _serve(monkeypatch, _api_message("om_old", "interactive", degraded, sender_type="app"))
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert relay.requests[0]["messages"][1]["content"] == "[引用消息内容不可用]\n\n能看到吗"


@pytest.mark.parametrize("kind", ["image", "file"])
async def test_media_parent_is_downloaded_from_the_quoted_message(
    db_engine, db_session, monkeypatch, kind
):
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "看看这个", chat_id="oc_same")
    await _set_parent(db_session, task, "om_media", "oc_same")
    content = (
        {"image_key": "img_q"}
        if kind == "image"
        else {"file_key": "file_q", "file_name": "季报.txt"}
    )
    _serve(monkeypatch, _api_message("om_media", kind, content))
    fetched = _serve_resources(monkeypatch)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    sent = relay.requests[0]["messages"][1]["content"]
    if kind == "image":
        assert fetched == [("om_media", "img_q", "image")]
        assert sent[0] == {"type": "text", "text": msg("quote_image_prefix", text="看看这个")}
        assert sent[1]["image_url"]["url"].startswith("data:image/png;")
    else:
        assert fetched == [("om_media", "file_q", "file")]
        assert sent[0] == {
            "type": "text",
            "text": msg("quote_file_prefix", name="季报.txt", text="看看这个"),
        }
        assert sent[1]["file_url"]["filename"] == "季报.txt"


async def test_merge_forward_lists_each_message_with_its_speaker(
    db_engine, db_session, monkeypatch
):
    bot = await _feishu_bot(db_session)
    colleague = User(display_name="张三")
    db_session.add(colleague)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=colleague.id, platform="feishu", platform_user_id="u_zhang", open_id="ou_zhang"
        )
    )
    await db_session.commit()
    task = await chat_task(db_session, bot, "总结这段聊天", chat_id="oc_same")
    await _set_parent(db_session, task, "om_fw", "oc_same")
    _serve(
        monkeypatch,
        _api_message("om_fw", "merge_forward", {"content": "Merged and Forwarded Message"}),
        _api_message(
            "om_c1",
            "text",
            {"text": "@_user_1 周报发了吗"},
            sender_id="ou_zhang",
            upper_message_id="om_fw",
            mentions=[{"key": "@_user_1", "name": "李四"}],
        ),
        _api_message(
            "om_c2",
            "text",
            {"text": "发了"},
            sender_type="app",
            sender_id="cli_quote",
            upper_message_id="om_fw",
        ),
        _api_message(
            "om_c3", "image", {"image_key": "img_x"}, sender_id="ou_other", upper_message_id="om_fw"
        ),
        _api_message(
            "om_c4",
            "text",
            {"text": "已撤回"},
            sender_id="ou_other",
            upper_message_id="om_fw",
            deleted=True,
        ),
    )
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    quoted = "\n".join(
        ["[合并转发的聊天记录]", "张三: @李四 周报发了吗", f"{bot.name}: 发了", "用户 1: [图片]"]
    )
    assert relay.requests[0]["messages"][1]["content"] == msg(
        "quote_text_prefix", quoted=quoted, text="总结这段聊天"
    )


async def test_recalled_parent_is_named_as_recalled(db_engine, db_session, monkeypatch):
    bot = await _feishu_bot(db_session)
    task = await chat_task(db_session, bot, "继续", chat_id="oc_same")
    await _set_parent(db_session, task, "om_gone", "oc_same")
    _serve(monkeypatch, _api_message("om_gone", "text", {}, deleted=True))
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert relay.requests[0]["messages"][1]["content"] == "[引用的消息已撤回]\n\n继续"


@pytest.mark.parametrize("event_chat", ["oc_same", "oc_other"])
async def test_refused_group_read_falls_back_to_the_received_event(
    db_engine, db_session, monkeypatch, event_chat
):
    """应用没有读取群内所有消息的权限时，群里 @ 过机器人的消息仍从入站事件读出。"""
    bot = await _feishu_bot(db_session)
    db_session.add(
        InboundEvent(
            bot_id=bot.id,
            platform="feishu",
            platform_msg_id="om_asked",
            kind="message",
            chat_type="group",
            chat_id=event_chat,
            sender_platform_user_id="u_asker",
            payload={
                "raw": {
                    "event": {
                        "message": {
                            "message_type": "text",
                            "content": json.dumps({"text": "@_user_1 查下昨天的订单"}),
                            "mentions": [{"key": "@_user_1", "name": "小助手"}],
                        }
                    }
                }
            },
            reply_context={},
        )
    )
    await db_session.commit()
    task = await chat_task(db_session, bot, "按自然日统计", chat_id="oc_same")
    await _set_parent(db_session, task, "om_asked", "oc_same")

    async def refused(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        raise FeishuError(230027, "Lack of necessary permissions")

    monkeypatch.setattr(FeishuClient, "call", refused)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    content = relay.requests[0]["messages"][1]["content"]
    if event_chat == "oc_same":
        assert content == msg(
            "quote_text_prefix", quoted="@小助手 查下昨天的订单", text="按自然日统计"
        )
    else:
        assert content == "[引用消息内容不可用]\n\n按自然日统计"


async def test_archived_reply_is_quoted_without_http(db_engine, db_session, monkeypatch):
    """流状态清掉之后，机器人自己发过的回复仍从发出消息存档里读。"""
    bot = await _feishu_bot(db_session)
    db_session.add_all(
        [
            FeishuSentMessage(
                bot_id=bot.id, message_id="om_sent", chat_id="oc_same", text="存档里的回复"
            ),
            FeishuSentMessage(
                bot_id=bot.id, message_id="om_elsewhere", chat_id="oc_other", text="别的会话"
            ),
        ]
    )
    await db_session.commit()
    task = await chat_task(db_session, bot, "展开讲讲", chat_id="oc_same")
    await _set_parent(db_session, task, "om_sent", "oc_same")

    async def no_http(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("archived reply must not call Feishu")

    monkeypatch.setattr(FeishuClient, "call", no_http)
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert relay.requests[0]["messages"][1]["content"] == msg(
        "quote_text_prefix", quoted="存档里的回复", text="展开讲讲"
    )
