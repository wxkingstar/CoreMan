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


async def test_builtin_prices_match_aliases_and_custom_overrides(client, db_session):
    counts = {
        "input_tokens": 1000000,
        "output_tokens": 1000000,
        "cache_read_tokens": 1000000,
        "cache_creation_tokens": 1000000,
    }
    at = datetime(2026, 9, 2, tzinfo=UTC)
    # 内置官方价：路由前缀、快照后缀和 [1m] 标记都认作同一个模型。
    for model in ("claude-opus-4-6", "vllm/claude-opus-4-6", "claude-opus-4-6[1m]"):
        assert await estimate(db_session, model=model, at=at, **counts) == Decimal("36.750000")
    assert await estimate(
        db_session, model="claude-haiku-4-5-20251001", at=at, **counts
    ) == Decimal("7.350000")
    # Codex 与 Claude 不互相套价；不认识的模型仍是未知。
    assert await estimate(db_session, model="codex/gpt-5.5", at=at, **counts) == Decimal(
        "35.500000"
    )
    assert await estimate(db_session, model="opencode/gpt-5.5", at=at, **counts) is None
    assert await estimate(db_session, model="claude-unknown-9", at=at, **counts) is None

    # 管理员录入的价格从生效日起覆盖内置价，之前仍按内置价。
    db_session.add(
        ModelPrice(
            provider="claude",
            model="claude-opus-4-6",
            effective_from=date(2026, 9, 1),
            input_usd=Decimal(1),
            output_usd=Decimal(1),
            cache_read_usd=Decimal(1),
            cache_write_usd=Decimal(1),
        )
    )
    await db_session.commit()
    assert await estimate(db_session, model="vllm/claude-opus-4-6", at=at, **counts) == Decimal(
        "4.000000"
    )
    assert await estimate(
        db_session, model="claude-opus-4-6", at=datetime(2026, 8, 31, tzinfo=UTC), **counts
    ) == Decimal("36.750000")

    await login_as(client, db_session)
    result = await client.get("/api/admin/model-prices")
    assert result.status_code == 200, result.text
    rows = [row for row in result.json()["data"] if row["model"] == "claude-opus-4-6"]
    assert [(row["effective_from"], row["source"]) for row in rows] == [
        ("2026-09-01", "custom"),
        ("2025-01-01", "builtin"),
    ]


async def test_backfill_fills_only_finished_complete_records(client, db_session):
    import uuid

    from sqlalchemy import select

    from coreman.core.db.models import CronRun
    from coreman.core.pricing import backfill_costs

    def log(task_id: int, **values):
        base = {
            "bot_id": uuid.uuid4(),
            "bot_key": "bot",
            "platform": "wecom",
            "chat_type": "single",
            "message_type": "text",
            "status": "success",
            "request_at": datetime(2026, 9, 1, tzinfo=UTC),
            "task_id": task_id,
            "model": "claude-sonnet-4-6",
            "input_tokens": 1000000,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        return ChatLog(**{**base, **values})

    db_session.add_all(
        [
            log(1),
            log(2, status="running"),
            log(3, cache_read_tokens=None),
            log(4, cost_usd=Decimal("9")),
            log(5, model="claude-unknown-9"),
        ]
    )
    db_session.add(
        CronRun(
            bot_id=uuid.uuid4(),
            job_name="job",
            task_id=1,
            status="success",
            prompt="p",
            started_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    await db_session.commit()
    assert await backfill_costs(db_session) == 1
    await db_session.commit()
    costs = dict((await db_session.execute(select(ChatLog.task_id, ChatLog.cost_usd))).all())
    assert costs == {
        1: Decimal("3.000000"),
        2: None,
        3: None,
        4: Decimal("9.000000"),
        5: None,
    }
    assert await db_session.scalar(select(CronRun.cost_usd)) == Decimal("3.000000")

    # 改价后补算尚无成本的记录，已有成本保持快照。
    await login_as(client, db_session, role="ai_committee")
    body = {
        "provider": "claude",
        "model": "claude-unknown-9",
        "effective_from": "2026-01-01",
        "input_usd": "1",
        "output_usd": "1",
        "cache_read_usd": "1",
        "cache_write_usd": "1",
    }
    result = await client.put("/api/admin/model-prices", json=body, headers={"If-Match": "0"})
    assert result.status_code == 200, result.text
    db_session.expire_all()
    assert await db_session.scalar(select(ChatLog.cost_usd).where(ChatLog.task_id == 5)) == Decimal(
        "1.000000"
    )
    assert await db_session.scalar(select(ChatLog.cost_usd).where(ChatLog.task_id == 1)) == Decimal(
        "3.000000"
    )
