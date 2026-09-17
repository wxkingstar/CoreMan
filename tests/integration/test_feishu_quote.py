"""飞书引用消息从持久化投递记录恢复；只有本 bot、本会话的父消息可见。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD
from coreman.core.db.models import FeishuDelivery, InboundEvent, OutboxItem, Task, TaskStream
from coreman.core.i18n.messages import msg
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import seed_bot


async def _feishu_bot(session: AsyncSession):  # type: ignore[no-untyped-def]
    bot, _relay, cipher = await seed_bot(session)
    bot.platform = "feishu"
    bot.credentials_enc = cipher.encrypt(
        json.dumps({"app_id": "cli_quote", "app_secret": "secret"}), CREDENTIALS_AAD
    )
    await session.commit()
    return bot


async def _set_parent(session: AsyncSession, task: Task, parent_id: str, chat_id: str) -> None:
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
    assert content == msg(
        "quote_text_prefix", quoted="父卡片的最终答案", text="继续解释"
    )
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


async def test_official_parent_read_is_identity_checked_and_bounded(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch
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
                        "sender": {"sender_type": "app", "id": "cli_quote"},
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
