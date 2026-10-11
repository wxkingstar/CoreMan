from datetime import UTC, date, datetime
from decimal import Decimal

from coreman.core.chat.chat_logs import ChatLogEntry, ChatLogWriter
from coreman.core.db.models import ChatLog, ModelPrice
from coreman.core.db.session import make_session_factory
from coreman.core.pricing import estimate
from tests.api.conftest import login_as


async def test_statistics_platform_wide_timezone_and_missing_counts(client, db_session):
    import uuid

    actor = await login_as(client, db_session, display_name="本人")
    actor.avatar_url = "https://example.com/me.png"
    other = uuid.uuid4()
    rows = (
        (15, actor.id, "wecom", 10),
        (16, actor.id, "wecom", None),
        # 别人的机器人、飞书私聊：统计只给聚合数字，普通成员也计入。
        (17, other, "feishu", 10000),
    )
    for hour, owner, platform, count in rows:
        db_session.add(
            ChatLog(
                bot_id=uuid.uuid4(),
                bot_key="bot",
                platform=platform,
                user_id=owner,
                user_name="me" if owner == actor.id else "other",
                chat_type="single",
                message_type="text",
                status="success",
                request_at=datetime(2026, 9, 1, hour, tzinfo=UTC),
                input_tokens=count,
            )
        )
    await db_session.commit()
    result = await client.get(
        "/api/admin/statistics",
        params={"start": "2026-09-01", "end": "2026-09-02", "timezone": "Asia/Shanghai"},
    )
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["total"]["messages"] == 3 and data["total"]["input_tokens"] == 10010
    assert data["total"]["input_tokens_measured"] == 2
    assert data["total"]["cost_usd"] is None and data["total"]["cost_usd_measured"] == 0
    assert [row["day"] for row in data["daily"]] == ["2026-09-01", "2026-09-02"]
    assert len(data["by_bot"]) == 3
    # 用户排行按 Token 合计倒序，消息更多的本人排在后面；有账号的用平台姓名和头像。
    assert [(row["name"], row["tokens"], row["messages"]) for row in data["by_user"]] == [
        ("other", 10000, 1),
        ("本人", 10, 2),
    ]
    assert [row["avatar_url"] for row in data["by_user"]] == [None, "https://example.com/me.png"]
    assert (
        await client.get(
            "/api/admin/statistics", params={"start": "2020-01-01", "end": "2026-09-02"}
        )
    ).status_code == 422


async def test_price_permissions_effective_date_and_unknown_usage(client, db_session):
    await login_as(client, db_session)
    body = {
        "provider": "claude",
        "model": "vllm/example",
        "effective_from": "2026-09-01",
        "input_usd": "2",
        "output_usd": "10",
        "cache_read_usd": "0.2",
        "cache_write_usd": "2.5",
    }
    assert (
        await client.put("/api/admin/model-prices", json=body, headers={"If-Match": "0"})
    ).status_code == 403
    await login_as(client, db_session, role="ai_committee")
    result = await client.put("/api/admin/model-prices", json=body, headers={"If-Match": "0"})
    assert result.status_code == 200, result.text
    assert (
        await client.put("/api/admin/model-prices", json=body, headers={"If-Match": "0"})
    ).status_code == 409
    counts = {
        "input_tokens": 1000000,
        "output_tokens": 200000,
        "cache_read_tokens": 3000000,
        "cache_creation_tokens": 400000,
    }
    assert await estimate(
        db_session, model="vllm/example", at=datetime(2026, 9, 2, tzinfo=UTC), **counts
    ) == Decimal("5.600000")
    assert (
        await estimate(
            db_session, model="vllm/example", at=datetime(2026, 8, 31, tzinfo=UTC), **counts
        )
        is None
    )
    counts["cache_creation_tokens"] = None
    assert (
        await estimate(
            db_session, model="vllm/example", at=datetime(2026, 9, 2, tzinfo=UTC), **counts
        )
        is None
    )


async def test_chat_writer_saves_cost_snapshot(db_engine, db_session):
    import uuid

    from sqlalchemy import select

    db_session.add(
        ModelPrice(
            provider="claude",
            model="vllm/example",
            effective_from=date(2026, 1, 1),
            input_usd=Decimal(2),
            output_usd=Decimal(10),
            cache_read_usd=Decimal("0.2"),
            cache_write_usd=Decimal("2.5"),
        )
    )
    await db_session.commit()
    writer = ChatLogWriter(make_session_factory(db_engine))
    assert await writer.write(
        ChatLogEntry(
            bot_id=uuid.uuid4(),
            bot_key="price",
            platform="wecom",
            chat_type="single",
            message_type="text",
            status="success",
            request_at=datetime(2026, 9, 1, tzinfo=UTC),
            model="vllm/example",
            input_tokens=1000000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
    )
    row = await db_session.scalar(select(ChatLog))
    assert row.cost_usd == Decimal("2.000000")
