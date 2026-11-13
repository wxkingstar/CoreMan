"""过期、待批提醒，以及停用流程用的 withdraw_pending。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import approvals, keys, sealed
from coreman.core.assistants.approvals import service, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.db.models import AssistantApproval, OutboxItem
from coreman.core.timeutils import utcnow
from tests.integration.approval_helpers import (
    CFG,
    STORE,
    assistant_turn,
    decided,
    install,
    install_files,
    pending,
    queued,
    reply,
)
from tests.integration.assistant_helpers import SECRET

pytestmark = pytest.mark.usefixtures("assistant_master")
LINK = "https://tower.example.com"


async def reload(session: AsyncSession, row: AssistantApproval) -> AssistantApproval:
    found = await session.get(AssistantApproval, row.id, populate_existing=True)
    assert found is not None
    return found


async def sent(session: AsyncSession) -> list[OutboxItem]:
    rows = list(await session.scalars(select(OutboxItem).order_by(OutboxItem.id)))
    await sealed.open_rows(session, rows)
    return rows


async def test_unhandled_approval_expires_after_72_hours(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    old = await pending(db_session, scope)
    fresh = await pending(db_session, scope, {**reply(), "text": "另一件事"})
    now = utcnow()
    assert await service.expire(db_session, now + timedelta(hours=71), CFG) == 0
    assert await service.expire(db_session, now + timedelta(hours=73), CFG) == 2
    await db_session.commit()
    for row in (old, fresh):
        row = await reload(db_session, row)
        assert row.status == "expired" and row.decided_at is not None
    # 按撤回处理：让审批系统也撤回，并通知助理。
    assert sorted(fake.withdrawn) == sorted([old.external_id, fresh.external_id])
    turns = await queued(db_session, APPROVAL_RESULT_KIND)
    assert sorted(t.payload["approval_id"] for t in turns) == sorted([str(old.id), str(fresh.id)])
    # 过期之后迟到的批准不执行。
    await decided(db_session, old)
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []


async def test_reminder_is_merged_contentless_and_rate_limited(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    await pending(db_session, scope)
    await pending(db_session, scope, {**reply(), "text": f"第二件 {SECRET}"})
    now = utcnow()
    # 刚提交：这一轮可能还在接着提交，先不发。
    assert await service.remind(db_session, now, LINK) == 0
    later = now + timedelta(seconds=40)
    assert await service.remind(db_session, later, LINK) == 1
    await db_session.commit()
    items = await sent(db_session)
    assert len(items) == 1 and items[0].bot_id == bot.id
    # 发到所有者与助理的私聊；只有张数和链接，没有任何内容。
    assert items[0].target == {"chat_id": "oc_assistant"}
    assert items[0].payload == {"markdown": f"有 2 张审批单等你处理：{LINK}"}
    # 都提醒过了：不再发。
    assert await service.remind(db_session, later + timedelta(minutes=10), LINK) == 0
    # 5 分钟内又来一张：等到间隔过了再发，张数是当时全部待批的。
    await pending(db_session, scope, {**reply(), "text": "第三件"})
    assert await service.remind(db_session, later + timedelta(minutes=2), LINK) == 0
    assert await service.remind(db_session, later + timedelta(minutes=6), LINK) == 1
    await db_session.commit()
    items = await sent(db_session)
    assert items[1].payload == {"markdown": f"有 3 张审批单等你处理：{LINK}"}


@pytest.mark.usefixtures("runtime_settings")
async def test_withdraw_pending_voids_everything_for_one_assistant(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    monkeypatch.setattr(tower, "config", lambda _settings: CFG)
    bot, _, scope = await assistant_turn(db_session)
    _, _, stranger = await assistant_turn(db_session, "lisi")
    first = await pending(db_session, scope)
    second = await pending(db_session, scope, {**reply(), "text": "另一件事"})
    done = await pending(db_session, scope, {**reply(), "text": "已有决定的"})
    await decided(db_session, done, "rejected")
    theirs = await pending(db_session, stranger)
    before = len(await queued(db_session, APPROVAL_RESULT_KIND))
    assert await approvals.withdraw_pending(db_session, bot.id, "disabled") == 2
    await db_session.commit()
    for row in (first, second):
        row = await reload(db_session, row)
        assert (row.status, row.error_code) == ("withdrawn", "disabled")
    assert sorted(fake.withdrawn) == sorted([first.external_id, second.external_id])
    # 已有决定的、别人的都不动；正在停用的助理不再收结果回合。
    assert (await reload(db_session, done)).status == "rejected"
    assert (await reload(db_session, theirs)).status == "pending"
    assert len(await queued(db_session, APPROVAL_RESULT_KIND)) == before
    assert await approvals.withdraw_pending(db_session, bot.id, "disabled") == 0


@pytest.mark.usefixtures("runtime_settings")
async def test_tick_runs_without_an_approval_service(db_session: AsyncSession) -> None:
    """没配审批系统的部署、没有助理的部署：每轮空转，不报错。"""
    assert await service.tick(db_session, utcnow()) == {
        "assistant_approvals_expired": 0,
        "assistant_approvals_reminded": 0,
        "assistant_approval_files_swept": 0,
        "assistant_approvals_recovered": 0,
    }


async def test_files_of_closed_approvals_are_swept_from_the_bucket(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    relay, bucket = install_files(monkeypatch)
    _, owner, scope = await assistant_turn(db_session)
    rows = []
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        arguments = {"upload_id": relay.add(name, name.encode()), "folder_token": "fld_1"}
        out = await approvals.submit(
            db_session,
            None,
            scope,
            "feishu_save_to_drive",
            arguments,
            cfg=CFG,
            files=relay,  # type: ignore[arg-type]
        )
        rows.append(await db_session.get(AssistantApproval, out["approval_id"]))
    await db_session.commit()
    waiting, rejected, executed = rows
    assert len(bucket.objects) == 3 and all(row.has_files for row in rows)
    # 待批的单不动：批准之后还要用。
    assert await service.sweep_files(db_session, STORE) == 0 and len(bucket.objects) == 3
    await decided(db_session, rejected, "rejected")
    executed.status = "executed"
    await db_session.commit()
    # 桶连不上：标记留着，下一轮再试。
    bucket.down = True
    assert await service.sweep_files(db_session, STORE) == 0
    bucket.down = False
    assert await service.sweep_files(db_session, STORE) == 2
    await db_session.commit()
    kept = (await reload(db_session, waiting)).args_enc["calls"][0]["arguments"]["upload_id"]  # type: ignore[index]
    assert list(bucket.objects) == [kept["key"]] and len(bucket.deleted) == 2
    assert not (await reload(db_session, rejected)).has_files
    assert await service.sweep_files(db_session, STORE) == 0
    # 没配临时桶的部署不扫；数据密钥已销毁的单读不出对象名，只清标记，交给桶的自动清理。
    assert await service.sweep_files(db_session, None) == 0
    await keys.destroy_key(db_session, owner.id)
    (await reload(db_session, waiting)).status = "withdrawn"
    await db_session.commit()
    assert await service.sweep_files(db_session, STORE) == 1 and len(bucket.objects) == 1


@pytest.mark.usefixtures("runtime_settings")
async def test_tick_closes_approved_tickets_whose_execution_died(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    await decided(db_session, row)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[0]
    row = await reload(db_session, row)
    row.decided_at = utcnow() - timedelta(minutes=20)
    await db_session.execute(text("UPDATE tasks SET status='failed' WHERE id=:i"), {"i": task.id})
    await db_session.commit()
    counts = await service.tick(db_session, utcnow())
    assert counts["assistant_approvals_recovered"] == 1
    await db_session.commit()
    row = await reload(db_session, row)
    assert (row.status, row.error_code) == ("failed", "execution_not_started")


async def test_no_reminder_or_expiry_turn_once_the_assistant_is_being_stopped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    now = utcnow()
    assert await service.remind(db_session, now + timedelta(seconds=40), LINK) == 0
    assert await sent(db_session) == []
    # 过期照常记下，只是不再往正在停用的助理会话里投结果。
    assert await service.expire(db_session, now + timedelta(hours=73), CFG) == 1
    await db_session.commit()
    assert (await reload(db_session, row)).status == "expired"
    assert fake.withdrawn == [row.external_id]
    assert await queued(db_session, APPROVAL_RESULT_KIND) == []


@pytest.mark.parametrize(
    ("web_url", "expected"),
    [
        ("https://tower.example.com/", "有 1 张审批单等你处理：https://tower.example.com/"),
        # 没配网页入口：不带链接，更不回落到审批系统的接口地址。
        (None, "有 1 张审批单等你处理。"),
        ("javascript:alert(1)", "有 1 张审批单等你处理。"),
    ],
)
@pytest.mark.usefixtures("runtime_settings")
async def test_the_reminder_links_to_the_web_entry_never_the_api_address(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    web_url: str | None,
    expected: str,
) -> None:
    from coreman.core.config import reset_settings_cache

    install(monkeypatch)
    monkeypatch.setenv("TOWER_URL", "https://tower-api.example.com")
    if web_url is None:
        monkeypatch.delenv("TOWER_WEB_URL", raising=False)
    else:
        monkeypatch.setenv("TOWER_WEB_URL", web_url)
    reset_settings_cache()
    _bot, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    row.created_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()
    counts = await service.tick(db_session, utcnow())
    await db_session.commit()
    assert counts["assistant_approvals_reminded"] == 1
    [item] = await sent(db_session)
    assert item.payload == {"markdown": expected}
    assert "tower-api" not in str(item.payload)
