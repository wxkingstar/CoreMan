"""A member's own agent's writes approved in Tower when the member uses it, with the Feishu card
as the fallback: one route at a time, never both."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants.approvals import tower
from coreman.core.cloud_proxy import decisions
from coreman.core.db.models import InteractionState, OutboxItem, Setting, Task
from coreman.core.systems_catalog import confirm, member_tower
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.member_write_tower import MemberWriteExecuteHandler
from tests.api.test_systems_catalog_member import (  # noqa: F401 （夹具）
    agent,
    call,
    click,
    fake,
    fresh_caches,
    setup,
)
from tests.fakes.business_system import FakeBusinessSystem
from tests.integration.worker_helpers import MASTER, build_ctx

CFG = tower.Config(
    "https://tower.example.com",
    "sender-key",
    "target-key",
    "https://coreman.example.com",
    member_tower.TARGET,
)
WRITE = {"system": "stock", "operation_id": "createDraft", "body": {"sku": "TW-1", "quantity": 4}}
WAIT = timedelta(minutes=member_tower.WAIT_DEFAULT)
assert MASTER  # build_ctx 用同一把主密钥，读得出暂存的写操作


class FakeTower:
    """Tower as the member-write flow sees it: activity, submit, token, redeem, report, withdraw."""

    def __init__(self) -> None:
        self.last_seen: datetime | None = utcnow() - timedelta(hours=5)
        self.down = False
        self.submit_refused: str | None = None
        self.withdraw_refused: str | None = None
        self.submitted: list[dict[str, Any]] = []
        self.withdrawn: list[str] = []
        self.reported: list[tuple[str, str, str | None]] = []
        self.looked_up: list[str] = []
        self._hashes: dict[str, str] = {}

    def _check(self) -> None:
        if self.down:
            raise tower.TowerError("tower_unavailable", 503)

    async def last_active(
        self, cfg: tower.Config, username: str, *, wait_seconds: float
    ) -> datetime | None:
        self.looked_up.append(username)
        return None if self.down else self.last_seen

    async def submit(self, cfg: tower.Config, **kw: Any) -> tower.Submitted:
        self._check()
        if self.submit_refused:
            raise tower.TowerError(self.submit_refused, 400)
        self.submitted.append(kw)
        approval_id = "apv-" + kw["external_id"][-6:]
        # 像真的 Tower 一样记下批准的是哪份请求（名义地址、请求体、调用方）。
        self._hashes[approval_id] = tower.request_hash(
            cfg, kw["external_id"], kw["body"], kw["caller"], url=kw["url"]
        )
        return tower.Submitted(approval_id, False)

    async def token(self, cfg: tower.Config, approval_id: str) -> str:
        self._check()
        return "twa_" + approval_id

    async def redeem(self, cfg: tower.Config, token: str, req_hash: str, caller: str) -> str:
        self._check()
        approval_id = token.removeprefix("twa_")
        if self._hashes.get(approval_id) != req_hash:
            raise tower.TowerError("request_mismatch", 403)
        return approval_id

    async def report(
        self, cfg: tower.Config, approval_id: str, status: str, error: str | None = None
    ) -> None:
        self.reported.append((approval_id, status, error))

    async def withdraw(self, cfg: tower.Config, external_id: str) -> None:
        self._check()
        if self.withdraw_refused:
            raise tower.TowerError(self.withdraw_refused, 409)
        self.withdrawn.append(external_id)


@pytest.fixture
def towered(monkeypatch: pytest.MonkeyPatch) -> FakeTower:
    fake_tower = FakeTower()
    monkeypatch.setattr(member_tower, "config", lambda: CFG)
    for name in ("last_active", "submit", "token", "redeem", "report", "withdraw"):
        monkeypatch.setattr(tower, name, getattr(fake_tower, name))
    return fake_tower


async def cards(session: AsyncSession, bot_id: uuid.UUID) -> list[OutboxItem]:
    return list(
        await session.scalars(
            select(OutboxItem).where(OutboxItem.kind == "send", OutboxItem.bot_id == bot_id)
        )
    )


async def state_of(session: AsyncSession, confirmation: str) -> InteractionState:
    state = await session.get(InteractionState, uuid.UUID(confirmation), populate_existing=True)
    assert state is not None
    return state


async def decide(session: AsyncSession, confirmation: str, status: str, **kw: Any) -> Any:
    state = await state_of(session, confirmation)
    applied = await decisions.apply(
        session,
        approval_id=kw.get("approval_id", state.state["approval_id"]),
        external_id=member_tower.external_id(state.id),
        status=status,
        reason=kw.get("reason"),
        decided_at=None,
    )
    await session.commit()
    return applied


async def execute(session: AsyncSession, engine: AsyncEngine) -> Task:
    task = await session.scalar(select(Task).where(Task.kind == member_tower.EXECUTE_KIND))
    assert task is not None
    task.status = "running"
    await session.commit()
    await MemberWriteExecuteHandler().run(build_ctx(engine, task))
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None
    return done


async def fall_back(session: AsyncSession, app: Any, after: timedelta) -> int:
    moved = await member_tower.fall_back(session, utcnow() + after, app.state.cipher, WAIT)
    await session.commit()
    return moved


def posts(fake_system: FakeBusinessSystem) -> list[Any]:
    return [r for r in fake_system.requests if r.method == "POST"]


async def test_active_member_approves_in_tower_and_it_runs_once(
    app: Any,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    fake: FakeBusinessSystem,  # noqa: F811
    towered: FakeTower,
) -> None:
    bot, _, _, access = await setup(db_session)
    async with agent(app, access) as client:
        staged = await call(client, "systems_call", WRITE)
        assert staged["status"] == "pending_confirmation" and staged["via"] == "tower"
        assert "Tower" in staged["message"]
        confirmation = staged["confirmation_id"]
        # Only Tower can approve it: no card went to Feishu.
        assert await cards(db_session, bot.id) == []
        [sent] = towered.submitted
        # The Tower request outlives the wait (6 hours by default) by an hour.
        lives = sent["expires_at"] - utcnow()
        assert WAIT + timedelta(minutes=59) < lives <= WAIT + member_tower.MARGIN
        assert towered.looked_up == ["zhangsan"]
        assert sent["requester"] == "zhangsan" and sent["caller"] == bot.bot_key
        assert sent["external_id"] == member_tower.external_id(uuid.UUID(confirmation))
        assert json.loads(sent["body"])["body"] == WRITE["body"]
        assert "TW-1" in sent["content"]
        waiting = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert waiting["status"] == "pending" and waiting["via"] == "tower"
        assert not posts(fake)

        assert (await decide(db_session, confirmation, "approved")).resumed
        done = await execute(db_session, db_engine)
        assert done.result == {"member_write": "member_write_ok"}
        [post] = posts(fake)
        assert json.loads(post.content) == WRITE["body"] and fake.subject(post) == "zhangsan"
        state = await state_of(db_session, confirmation)
        assert towered.reported == [(state.state["approval_id"], "executed", None)]
        result = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert result["status"] == "ok" and result["via"] == "tower"
        assert "TW-1" in result["response"]

    # A replayed approval runs nothing more.
    assert not (await decide(db_session, confirmation, "approved")).resumed
    assert len(posts(fake)) == 1
    # Tower never gets a card fallback for a decided request.
    assert await fall_back(db_session, app, WAIT + timedelta(minutes=1)) == 0


@pytest.mark.parametrize(
    "case", ["never_used", "stale", "lookup_down", "submit_refused", "no_login_name"]
)
async def test_card_when_tower_is_not_the_members_route(
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,  # noqa: F811
    towered: FakeTower,
    case: str,
) -> None:
    bot, member, _, access = await setup(db_session)
    if case == "never_used":
        towered.last_seen = None
    elif case == "stale":
        towered.last_seen = utcnow() - member_tower.ACTIVE_WITHIN - timedelta(minutes=1)
    elif case == "lookup_down":
        towered.down = True
    elif case == "submit_refused":
        towered.submit_refused = "target_not_found"
    else:
        member.login_name = ""
        await db_session.commit()
    async with agent(app, access) as client:
        staged = await call(client, "systems_call", WRITE)
        assert staged["status"] == "pending_confirmation" and staged["via"] == "feishu"
        assert len(await cards(db_session, bot.id)) == 1
        assert not towered.submitted
        assert await click(app, db_session, bot, staged["confirmation_id"], "confirm") == (
            "member_write_ok"
        )
        done = await call(
            client, "systems_call_result", {"confirmation_id": staged["confirmation_id"]}
        )
        assert done["status"] == "ok" and done["via"] == "feishu"


async def test_unanswered_tower_request_is_withdrawn_before_the_card(
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,  # noqa: F811
    towered: FakeTower,
) -> None:
    bot, _, _, access = await setup(db_session)
    async with agent(app, access) as client:
        confirmation = (await call(client, "systems_call", WRITE))["confirmation_id"]
        external = member_tower.external_id(uuid.UUID(confirmation))
        # Not yet past the wait: still Tower's.
        assert await fall_back(db_session, app, WAIT - timedelta(minutes=1)) == 0
        assert towered.withdrawn == [] and await cards(db_session, bot.id) == []
        # Tower unreachable: no card yet, tried again next tick.
        towered.down = True
        assert await fall_back(db_session, app, WAIT + timedelta(minutes=1)) == 0
        assert await cards(db_session, bot.id) == []
        towered.down = False
        assert await fall_back(db_session, app, WAIT + timedelta(minutes=1)) == 1
        assert towered.withdrawn == [external]
        [card] = await cards(db_session, bot.id)
        assert card.target == {"user_id": "ou_zs"} and "TW-1" in str(card.payload["card"])
        state = await state_of(db_session, confirmation)
        assert state.state["route"] == "feishu"
        # The card's 60 minutes count from when it was sent, not from the first submission.
        assert state.expires_at is not None
        left = state.expires_at - (utcnow() + WAIT + timedelta(minutes=1))
        assert timedelta(minutes=59) < left <= confirm.TTL
        waiting = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert waiting["status"] == "pending" and waiting["via"] == "feishu"

        # A late Tower decision after the withdrawal runs nothing; the card does.
        assert not (await decide(db_session, confirmation, "approved")).resumed
        assert (
            await db_session.scalar(select(Task).where(Task.kind == member_tower.EXECUTE_KIND))
            is None
        )
        assert await click(app, db_session, bot, confirmation, "confirm") == "member_write_ok"
        assert len(posts(fake)) == 1


async def test_refused_withdrawal_keeps_tower_and_never_sends_the_card(
    app: Any,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    fake: FakeBusinessSystem,  # noqa: F811
    towered: FakeTower,
) -> None:
    bot, _, _, access = await setup(db_session)
    async with agent(app, access) as client:
        confirmation = (await call(client, "systems_call", WRITE))["confirmation_id"]
        towered.withdraw_refused = "already_final"
        assert await fall_back(db_session, app, WAIT + timedelta(minutes=1)) == 0
        assert await cards(db_session, bot.id) == []
        state = await state_of(db_session, confirmation)
        assert state.state["fallback_refused"] == "already_final"
        # Not tried again, and the card cannot be confirmed either.
        towered.withdraw_refused = None
        assert await fall_back(db_session, app, WAIT + timedelta(minutes=2)) == 0
        assert towered.withdrawn == []
        assert await click(app, db_session, bot, confirmation, "confirm") == "ignored"
        # Tower's decision stands.
        assert (await decide(db_session, confirmation, "approved")).resumed
        assert (await execute(db_session, db_engine)).result == {"member_write": "member_write_ok"}
        done = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert done["status"] == "ok" and done["via"] == "tower"


async def test_rejected_or_expired_in_tower_runs_nothing(
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,  # noqa: F811
    towered: FakeTower,
) -> None:
    _, _, _, access = await setup(db_session)
    async with agent(app, access) as client:
        rejected = (await call(client, "systems_call", WRITE))["confirmation_id"]
        expired = (
            await call(client, "systems_call", {**WRITE, "body": {"sku": "TW-2", "quantity": 1}})
        )["confirmation_id"]
        assert not (await decide(db_session, rejected, "rejected", reason="数量不对")).resumed
        assert not (await decide(db_session, expired, "expired")).resumed
        first = await call(client, "systems_call_result", {"confirmation_id": rejected})
        assert first["status"] == "rejected" and "数量不对" in first["message"]
        assert first["via"] == "tower"
        second = await call(client, "systems_call_result", {"confirmation_id": expired})
        assert second["status"] == "expired" and second["via"] == "tower"
    # A decision for an approval this platform did not submit is not ours.
    other = await decide(db_session, rejected, "approved", approval_id="apv-other")
    assert not other.known
    assert not posts(fake)


async def test_token_for_another_request_is_refused(
    app: Any,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    fake: FakeBusinessSystem,  # noqa: F811
    towered: FakeTower,
) -> None:
    _, _, _, access = await setup(db_session)
    async with agent(app, access) as client:
        confirmation = (await call(client, "systems_call", WRITE))["confirmation_id"]
        # What Tower approved is not what is staged here any more.
        approval = (await state_of(db_session, confirmation)).state["approval_id"]
        towered._hashes[approval] = "0" * 64
        assert (await decide(db_session, confirmation, "approved")).resumed
        assert (await execute(db_session, db_engine)).result == {
            "member_write": "member_write_refused"
        }
        assert not posts(fake)
        assert towered.reported == [(approval, "failed", "failed")]
        done = await call(client, "systems_call_result", {"confirmation_id": confirmation})
        assert done["status"] == "failed" and "request_mismatch" in done["message"]


def test_reconciliation_picks_up_member_write_decisions() -> None:
    from coreman.core.cloud_proxy import reconcile

    assert member_tower.EXTERNAL_PREFIX.startswith(reconcile.EXTERNAL_PREFIX)
    state_id = uuid.uuid4()
    assert member_tower.state_id_of(member_tower.external_id(state_id)) == state_id
    assert member_tower.state_id_of("cm-mw-" + str(state_id)) is None
    assert member_tower.state_id_of("cm-pa-" + state_id.hex) is None


async def test_the_wait_is_a_setting(
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,  # noqa: F811
    towered: FakeTower,
) -> None:
    bot, _, _, access = await setup(db_session)
    db_session.add(Setting(key=member_tower.WAIT_SETTING, value=30))
    await db_session.commit()
    wait = await member_tower.wait_of(db_session)
    assert wait == timedelta(minutes=30)
    async with agent(app, access) as client:
        await call(client, "systems_call", WRITE)
    [sent] = towered.submitted
    assert sent["expires_at"] - utcnow() <= wait + member_tower.MARGIN
    moved = await member_tower.fall_back(
        db_session, utcnow() + timedelta(minutes=31), app.state.cipher, wait
    )
    await db_session.commit()
    assert moved == 1 and len(await cards(db_session, bot.id)) == 1
