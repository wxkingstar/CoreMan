"""个人助理无人在场时经平台代理的写操作：暂存成审批单，批准后由平台原样执行一次，结果投回。

业务系统目录代理（systems_call）与云签名代理（阿里云）各走一遍：能提审批的轮次只建单、不碰上游；
批准后恰好执行一次、执行的是暂存的那一份；被拒、过期、撤回、授权在批准后失效都不执行；不扩大
权限（没开写入、destructive / financial、高风险云写、判不出类别都不建单）；所有者在场、调度
公共员工、普通员工的轮次与原来一致。
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.approvals import catalog, proxied, service, tower
from coreman.core.assistants.kinds import (
    APPROVAL_EXECUTE_KIND,
    APPROVAL_RESULT_KIND,
    BOT_HELP_KIND,
    ORIGIN_KINDS,
)
from coreman.core.assistants.tools import groups_for
from coreman.core.cloud_proxy import aliyun
from coreman.core.cloud_proxy import policy as cloud_policy
from coreman.core.cloud_proxy import service as cloud_service
from coreman.core.cloud_proxy.risk_policy import CloudProxyPolicy
from coreman.core.config import CLOUD_PROXY_PRODUCTS
from coreman.core.db.models import (
    AssistantApproval,
    Bot,
    BotSystemGrant,
    BusinessSystem,
    BusinessTokenIssue,
    SystemCall,
    Task,
    User,
)
from coreman.core.db.models.cloud_proxy import CloudCall
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.policy import Scope
from coreman.core.systems_catalog.service import Issuance
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import assistant_approvals as worker_approvals
from tests.api.test_systems_catalog import bearer, call
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.integration.approval_helpers import FakeTower, decided, install_tower, queued
from tests.integration.job_helpers import take
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")

STAGEABLE = [kind for kind in ORIGIN_KINDS if "approval" in groups_for(kind)]
WRITE = {"system": "stock", "operation_id": "createDraft", "body": {"sku": "SKU-9", "quantity": 3}}
AK = "LTAI-test-proxy-ak"
SLB = "/api/runtime/cloud/aliyun/slb"
ACL = "acl-example-test"
POLICY = CloudProxyPolicy.model_validate({"slb": {"acls": {ACL: "test"}}})
SLB_HOST = "https://slb.us-east-1.aliyuncs.com/"


def slb_form(entry: str) -> bytes:
    entries = json.dumps([{"entry": entry, "comment": "tmp"}])
    params = {"Action": "AddAccessControlListEntry", "AclId": ACL, "AclEntrys": entries}
    return urllib.parse.urlencode(sorted(params.items())).encode()


LOW = slb_form("8.8.8.8")
HIGH = slb_form("8.8.8.0/24")


def configure(app: Any, risk_policy: CloudProxyPolicy | None = POLICY) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={
            "cloud_proxy_policy": risk_policy,
            "tower_url": "https://tower.example.com",
            "tower_callback_secret": SecretStr("cb"),
            "tower_sender_key": SecretStr("sender"),
            "tower_target_keys": {
                tower.TARGET: SecretStr("target"),
                **{p: SecretStr(f"key-{p}") for p in CLOUD_PROXY_PRODUCTS},
            },
            "aliyun_proxy_access_key_id": SecretStr(AK),
            "aliyun_proxy_access_key_secret": SecretStr("test-proxy-secret"),
        }
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    install(monkeypatch, system)
    return system


@pytest.fixture
def fake_tower(monkeypatch: pytest.MonkeyPatch, app: Any) -> FakeTower:
    configure(app)
    return install_tower(monkeypatch)


@pytest.fixture
def aliyun_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    with respx.MockRouter(assert_all_called=False) as router:
        monkeypatch.setattr(
            cloud_service,
            "transport_factory",
            lambda url: httpx.MockTransport(router.async_handler),
        )
        yield router


async def owner_turn(
    session: AsyncSession, kind: str, *, write: bool = True
) -> tuple[Bot, User, Task]:
    """开通好的助理、它的所有者，以及这位助理的一轮（类型由用例定），员工能用库存系统。"""
    bot, owner = await assistant(session)
    session.add(
        BusinessSystem(
            key="stock",
            name="库存",
            base_url=BASE_URL,
            openapi_url=SPEC_URL,
            token_delivery="proxy",
            allowed_bot_ids=None,
        )
    )
    await session.flush()
    session.add(BotSystemGrant(bot_id=bot.id, system_key="stock", allow_write=write))
    task = Task(
        bot_id=bot.id,
        kind=kind,
        user_id=owner.id,
        payload={},
        status="running",
        session_key="oc_private",
    )
    session.add(task)
    await session.commit()
    return bot, owner, task


async def approvals(session: AsyncSession) -> list[AssistantApproval]:
    rows = list(
        await session.scalars(
            select(AssistantApproval)
            .order_by(AssistantApproval.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    return rows


async def system_calls(session: AsyncSession) -> list[tuple[str, str]]:
    rows = await session.scalars(
        select(SystemCall).order_by(SystemCall.id).execution_options(populate_existing=True)
    )
    return [(row.operation_id, row.outcome) for row in rows]


def posts(fake: FakeBusinessSystem) -> list[dict[str, Any]]:
    return [
        json.loads(r.content)
        for r in fake.requests
        if r.method == "POST" and r.url.path == "/api/stock/documents"
    ]


def issuance(app: Any) -> Issuance:
    settings = app.state.settings
    return Issuance(
        cipher=app.state.cipher,
        issuer="coreman",
        external_key=settings.external_jwt_key,
        providers=settings.business_token_providers,
    )


async def approve_and_run(
    app: Any, engine: AsyncEngine, session: AsyncSession, row: AssistantApproval
) -> tuple[str, Task]:
    """所有者批准（回调排下执行任务），执行一遍；返回审批单的状态与执行任务。"""
    await decided(session, row)
    [job] = await queued(session, APPROVAL_EXECUTE_KIND)
    status = await proxied.run(
        make_session_factory(engine),
        row.id,
        job,
        tower.config(app.state.settings),
        settings=app.state.settings,
        issuance=issuance(app),
    )
    return status, job


async def staged_system(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, kind: str = STAGEABLE[0]
) -> tuple[Bot, User, Task, AssistantApproval]:
    bot, owner, task = await owner_turn(db_session, kind)
    out = await call(client, bearer(app, task, owner), "systems_call", WRITE)
    assert out["status"] == "pending_approval", out
    [row] = await approvals(db_session)
    return bot, owner, task, row


# 业务系统目录代理 --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ORIGIN_KINDS)
async def test_an_unattended_write_is_staged_only_where_approvals_can_be_submitted(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
    kind: str,
) -> None:
    """每一种无人在场的类型都过一遍：能提审批的建单不发，不能的（子任务、冷启动）照旧拒绝。"""
    _, owner, task = await owner_turn(db_session, kind)
    out = await call(client, bearer(app, task, owner), "systems_call", WRITE)
    assert posts(fake) == []
    if kind not in STAGEABLE:
        assert out["error"] == "owner_turn_required" and "所有者不在场" in out["message"]
        assert await approvals(db_session) == [] and fake_tower.submitted == []
        assert await system_calls(db_session) == [("createDraft", "denied")]
        return
    assert "error" not in out
    assert out["status"] == "pending_approval" and "不要重试" in out["hint"]
    [row] = await approvals(db_session)
    assert str(row.id) == out["approval_id"]
    assert (row.tool, row.channel, row.status) == (catalog.SYSTEM_CALL, "other", "pending")
    assert row.session_key == "oc_private" and row.origin_event_id is None
    [sent] = fake_tower.submitted
    assert (
        sent["requester"] == "owner"
        and sent["abstract"] == "业务系统 · 库存 · 创建一张出入库单据草稿"
    )
    assert await system_calls(db_session) == [("createDraft", "staged")]
    # 完整请求加密暂存：库里读不到明文。
    stored = await db_session.scalar(
        text("SELECT args_enc::text FROM assistant_approvals WHERE id = :id"), {"id": row.id}
    )
    assert "SKU-9" not in str(stored)
    arguments = row.args_enc["calls"][0]["arguments"]
    assert arguments == {
        "system": "stock",
        "operation_id": "createDraft",
        "path_params": None,
        "query": None,
        "body": {"sku": "SKU-9", "quantity": 3},
        "method": "POST",
        "url": BASE_URL + "/api/stock/documents",
    }
    # describe 说清这一轮写操作会提交审批。
    detail = await call(
        client,
        bearer(app, task, owner),
        "systems_describe",
        {"system": "stock", "operation_id": "createDraft"},
    )
    assert detail["call"]["allowed"] is True and "提交给所有者审批" in detail["call"]["message"]


async def test_a_repeated_call_reuses_the_live_approval(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    _, owner, task = await owner_turn(db_session, STAGEABLE[0])
    auth = bearer(app, task, owner)
    first = await call(client, auth, "systems_call", WRITE)
    again = await call(client, auth, "systems_call", WRITE)
    assert again["approval_id"] == first["approval_id"] and again["duplicate"] is True
    assert len(await approvals(db_session)) == 1 and len(fake_tower.submitted) == 1
    # 换了参数就是另一件事，另建一张。
    other = await call(
        client, auth, "systems_call", {**WRITE, "body": {"sku": "SKU-1", "quantity": 1}}
    )
    assert other["approval_id"] != first["approval_id"]
    assert len(await approvals(db_session)) == 2 and posts(fake) == []


def owner_approval(bot: Bot, n: int) -> AssistantApproval:
    """所有者在场时自己提交的一张待批飞书写审批（只要状态与工具名）。"""
    row_id = uuid.uuid4()
    return AssistantApproval(
        id=row_id,
        bot_id=bot.id,
        tool="feishu_send_message",
        external_id=service.EXTERNAL_PREFIX + row_id.hex,
        args_enc={"calls": [], "abstract": f"私聊 → 某人 {n}"},
        args_hash=f"owner-{n}",
        request_hash="h",
        channel="p2p",
        expires_at=utcnow() + service.TTL,
    )


async def test_proxied_writes_have_their_own_smaller_pending_cap(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    """无人在场的代理写最多同时待批 5 张，刷不满各类共用的 10 张。"""
    bot, owner, task = await owner_turn(db_session, STAGEABLE[0])
    auth = bearer(app, task, owner)
    for n in range(proxied.MAX_PENDING):
        out = await call(
            client, auth, "systems_call", {**WRITE, "body": {"sku": f"S{n}", "quantity": 1}}
        )
        assert out["status"] == "pending_approval"
    full = await call(client, auth, "systems_call", WRITE)
    assert full["error"] == "too_many_pending" and full["limit"] == proxied.MAX_PENDING
    assert "所有者不在场" in full["hint"]
    assert len(await approvals(db_session)) == proxied.MAX_PENDING
    assert len(fake_tower.submitted) == proxied.MAX_PENDING and posts(fake) == []
    assert proxied.MAX_PENDING < service.MAX_PENDING
    # 所有者自己的审批还有位置：各类共用的上限照旧是 10 张。
    db_session.add_all(
        [owner_approval(bot, n) for n in range(service.MAX_PENDING - proxied.MAX_PENDING)]
    )
    await db_session.commit()
    assert len(await approvals(db_session)) == service.MAX_PENDING


async def test_the_shared_pending_cap_still_applies(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    bot, owner, task = await owner_turn(db_session, STAGEABLE[0])
    db_session.add_all([owner_approval(bot, n) for n in range(service.MAX_PENDING)])
    await db_session.commit()
    full = await call(client, bearer(app, task, owner), "systems_call", WRITE)
    assert full["error"] == "too_many_pending" and full["limit"] == service.MAX_PENDING
    assert fake_tower.submitted == [] and posts(fake) == []


async def test_a_request_too_large_to_review_is_not_staged(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    """所有者在审批单上看不全的请求不提交：不建单、不发请求，返回 too_large_to_review。"""
    _, owner, task = await owner_turn(db_session, STAGEABLE[0])
    out = await call(
        client,
        bearer(app, task, owner),
        "systems_call",
        {**WRITE, "body": {"sku": "S" * 12000, "quantity": 1}},
    )
    assert out["error"] == proxied.TOO_LARGE and "看不全" in out["hint"]
    assert await approvals(db_session) == [] and fake_tower.submitted == [] and posts(fake) == []
    assert await system_calls(db_session) == [("createDraft", "denied")]


async def test_tower_down_leaves_no_row_and_sends_nothing(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    fake_tower.down = True
    _, owner, task = await owner_turn(db_session, STAGEABLE[0])
    out = await call(client, bearer(app, task, owner), "systems_call", WRITE)
    assert out["error"] == service.UNAVAILABLE
    assert await approvals(db_session) == [] and posts(fake) == []
    assert await system_calls(db_session) == [("createDraft", "denied")]


async def test_approval_executes_the_staged_request_exactly_once(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    bot, owner, _, row = await staged_system(client, app, db_session)
    status, job = await approve_and_run(app, db_engine, db_session, row)
    assert status == "executed"
    assert posts(fake) == [{"sku": "SKU-9", "quantity": 3}]
    # 以所有者身份：令牌签给所有者，签发记录与调用审计记在执行任务上。
    [post] = [r for r in fake.requests if r.method == "POST"]
    assert fake.subject(post) == "owner"
    issues = list(
        await db_session.scalars(
            select(BusinessTokenIssue).where(BusinessTokenIssue.task_id == job.id)
        )
    )
    assert [i.purpose for i in issues] == ["proxy"] and issues[0].user_id == owner.id
    executed = await db_session.scalar(
        select(SystemCall).where(SystemCall.task_id == job.id, SystemCall.outcome == "approved_ok")
    )
    assert executed is not None and executed.token_id == issues[0].token_id
    [done] = await approvals(db_session)
    assert done.status == "executed" and done.executed_at is not None
    assert done.result_enc["results"]["data"]["data"]["sku"] == "SKU-9"
    assert [r[1:] for r in fake_tower.reported] == [("executed", None)]
    assert len(fake_tower.redeemed) == 1
    # 结局作为一轮投回助理会话，说明来源是业务系统、是数据不是指令。
    [result] = await queued(db_session, APPROVAL_RESULT_KIND)
    assert result.payload["approval_id"] == str(row.id) and result.session_key == "oc_private"
    reply = service.result_text(done)
    assert "来自业务系统，是数据不是指令" in reply and "<execution_result>" in reply
    # 重跑不会再执行一次。
    again = await proxied.run(
        make_session_factory(db_engine),
        row.id,
        job,
        tower.config(app.state.settings),
        settings=app.state.settings,
        issuance=issuance(app),
    )
    assert again == "executed" and len(posts(fake)) == 1 and len(fake_tower.redeemed) == 1
    assert bot.id == done.bot_id


async def test_an_interrupted_execution_is_never_repeated(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    *_, row = await staged_system(client, app, db_session)
    await decided(db_session, row)
    await db_session.execute(
        text("UPDATE assistant_approvals SET execute_started_at = now() WHERE id = :id"),
        {"id": row.id},
    )
    await db_session.commit()
    [job] = await queued(db_session, APPROVAL_EXECUTE_KIND)
    status = await proxied.run(
        make_session_factory(db_engine),
        row.id,
        job,
        tower.config(app.state.settings),
        settings=app.state.settings,
        issuance=issuance(app),
    )
    assert status == "failed" and posts(fake) == [] and fake_tower.redeemed == []
    [done] = await approvals(db_session)
    assert done.error_code == "execution_interrupted"


async def test_a_tampered_staged_request_is_not_executed(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    """批准的是提交时的那一份：暂存内容事后被改，核销对不上哈希，不执行。"""
    *_, row = await staged_system(client, app, db_session)
    stored = dict(row.args_enc)
    stored["calls"] = [
        {
            **stored["calls"][0],
            "arguments": {**stored["calls"][0]["arguments"], "body": {"sku": "X", "quantity": 99}},
        }
    ]
    row.args_enc = stored
    await db_session.commit()
    status, _ = await approve_and_run(app, db_engine, db_session, row)
    assert status == "failed" and posts(fake) == []
    [done] = await approvals(db_session)
    assert done.error_code == "request_mismatch"


@pytest.mark.parametrize("outcome", ["rejected", "expired", "withdrawn"])
async def test_rejected_expired_or_withdrawn_never_executes(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
    outcome: str,
) -> None:
    bot, owner, task, row = await staged_system(client, app, db_session)
    if outcome == "rejected":
        await decided(db_session, row, "rejected", "不要改")
    elif outcome == "expired":
        await service.expire(db_session, row.expires_at + timedelta(seconds=1), None)
        await db_session.commit()
    else:
        scope = Scope(task, bot, owner.id, "", "", "", "", "")
        out = await service.withdraw(db_session, scope, str(row.id), "不需要了", cfg=None)
        await db_session.commit()
        assert out["status"] == "withdrawn"
        # 撤回之后迟到的批准不会排执行。
        await decided(db_session, row)
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    [done] = await approvals(db_session)
    assert done.status == outcome
    fake_job = Task(bot_id=bot.id, kind=APPROVAL_EXECUTE_KIND, payload={}, status="running")
    db_session.add(fake_job)
    await db_session.commit()
    status = await proxied.run(
        make_session_factory(db_engine),
        row.id,
        fake_job,
        tower.config(app.state.settings),
        settings=app.state.settings,
        issuance=issuance(app),
    )
    assert status == outcome and posts(fake) == [] and fake_tower.redeemed == []


async def _revoke_grant(session: AsyncSession, bot: Bot, owner: User) -> None:
    grant = await session.get(BotSystemGrant, (bot.id, "stock"))
    assert grant is not None
    await session.delete(grant)


async def _no_write(session: AsyncSession, bot: Bot, owner: User) -> None:
    grant = await session.get(BotSystemGrant, (bot.id, "stock"))
    assert grant is not None
    grant.allow_write = False


async def _system_off(session: AsyncSession, bot: Bot, owner: User) -> None:
    system = await session.get(BusinessSystem, "stock")
    assert system is not None
    system.enabled = False


async def _env_delivery(session: AsyncSession, bot: Bot, owner: User) -> None:
    system = await session.get(BusinessSystem, "stock")
    assert system is not None
    system.token_delivery = "env"


async def _stopping(session: AsyncSession, bot: Bot, owner: User) -> None:
    row = await session.get(Bot, bot.id)
    assert row is not None
    row.assistant_state = "deprovisioning"


async def _owner_left(session: AsyncSession, bot: Bot, owner: User) -> None:
    user = await session.get(User, owner.id)
    assert user is not None
    user.status = "disabled"


async def _new_owner(session: AsyncSession, bot: Bot, owner: User) -> None:
    other = User(login_name="other", display_name="别人", email="other@example.test")
    session.add(other)
    await session.flush()
    row = await session.get(Bot, bot.id)
    assert row is not None
    row.owner_id = other.id


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_revoke_grant, "grant_revoked"),
        (_no_write, "write_not_allowed"),
        (_system_off, "system_unavailable"),
        (_env_delivery, "system_unavailable"),
        (_stopping, "assistant_inactive"),
        (_owner_left, "owner_changed"),
        (_new_owner, "owner_changed"),
    ],
)
async def test_a_change_between_approval_and_execution_stops_it(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
    change: Any,
    code: str,
) -> None:
    bot, owner, _, row = await staged_system(client, app, db_session)
    await decided(db_session, row)
    await change(db_session, bot, owner)
    await db_session.commit()
    [job] = await queued(db_session, APPROVAL_EXECUTE_KIND)
    status = await proxied.run(
        make_session_factory(db_engine),
        row.id,
        job,
        tower.config(app.state.settings),
        settings=app.state.settings,
        issuance=issuance(app),
    )
    assert status == "failed" and posts(fake) == []
    # 复查在核销之前：批准的令牌没有被用掉。
    assert fake_tower.redeemed == []
    [done] = await approvals(db_session)
    assert done.error_code == code
    assert [r[1:] for r in fake_tower.reported] == [("failed", code)]


async def test_a_disabled_assistant_never_executes(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    bot, *_, row = await staged_system(client, app, db_session)
    await decided(db_session, row)
    found = await db_session.get(Bot, bot.id)
    assert found is not None
    found.enabled = False
    await db_session.commit()
    [job] = await queued(db_session, APPROVAL_EXECUTE_KIND)
    status = await proxied.run(
        make_session_factory(db_engine),
        row.id,
        job,
        tower.config(app.state.settings),
        settings=app.state.settings,
        issuance=issuance(app),
    )
    assert status == "failed" and posts(fake) == [] and fake_tower.redeemed == []


async def test_a_changed_request_is_refused_by_the_proxy_itself(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    """执行走目录代理的同一条路径：现在拼出的请求与批准的那份不一致时不发出。"""
    bot, _, task, row = await staged_system(client, app, db_session)
    arguments = dict(row.args_enc["calls"][0]["arguments"])
    job = Task(bot_id=bot.id, kind=APPROVAL_EXECUTE_KIND, payload={}, status="running")
    db_session.add(job)
    await db_session.commit()
    out = await proxied._execute_system(
        make_session_factory(db_engine),
        bot.id,
        job,
        {**arguments, "url": BASE_URL + "/api/stock/elsewhere"},
        issuance(app),
        str(row.id),
    )
    assert out["error"] == "request_changed" and posts(fake) == []
    assert task.id != job.id
    outcomes = list(
        await db_session.scalars(
            select(SystemCall.outcome)
            .where(SystemCall.task_id == job.id)
            .execution_options(populate_existing=True)
        )
    )
    assert outcomes == ["denied"]


async def test_no_write_permission_no_approval(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    """审批不扩大权限：员工没开写入，无人在场时照旧拒绝，不建单。"""
    _, owner, task = await owner_turn(db_session, STAGEABLE[0], write=False)
    out = await call(client, bearer(app, task, owner), "systems_call", WRITE)
    assert out["error"] == "owner_turn_required"
    assert await approvals(db_session) == [] and fake_tower.submitted == [] and posts(fake) == []


@pytest.mark.parametrize(
    ("op_id", "risk", "code"),
    [
        # 所有者在场时 destructive 走确认卡；无人在场没人点卡，也不提审批，照旧拒绝。
        ("cancelDocument", "destructive", "owner_turn_required"),
        ("refundSupplier", "financial", "risk_not_allowed"),
    ],
)
async def test_destructive_and_financial_are_never_staged(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
    op_id: str,
    risk: str,
    code: str,
) -> None:
    _, owner, task = await owner_turn(db_session, STAGEABLE[0])
    out = await call(
        client,
        bearer(app, task, owner),
        "systems_call",
        {"system": "stock", "operation_id": op_id, "path_params": {"id": "D1"}},
    )
    assert out["error"] == code and out["risk"] == risk
    assert await approvals(db_session) == [] and fake_tower.submitted == []
    assert not [r for r in fake.requests if r.method == "POST"]


async def test_invalid_arguments_are_refused_before_staging(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    _, owner, task = await owner_turn(db_session, STAGEABLE[0])
    out = await call(
        client, bearer(app, task, owner), "systems_call", {**WRITE, "body": {"sku": "S"}}
    )
    assert out["error"] == "invalid_arguments"
    assert await approvals(db_session) == [] and fake_tower.submitted == []


@pytest.mark.parametrize("kind", ["chat", BOT_HELP_KIND])
async def test_turns_that_are_not_unattended_behave_as_before(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
    kind: str,
) -> None:
    """所有者在场走确认卡（这里的一轮没有可点卡的飞书来源，按 main 返回 confirmation_unavailable）；
    调度公共员工没批准写就不能写。都不执行、不建审批单。"""
    _, owner, task = await owner_turn(db_session, kind)
    out = await call(client, bearer(app, task, owner), "systems_call", WRITE)
    if kind == "chat":
        assert out["error"] == "confirmation_unavailable" and posts(fake) == []
    else:
        assert out["error"] == "write_not_allowed" and posts(fake) == []
    assert await approvals(db_session) == [] and fake_tower.submitted == []


def with_idempotent_op(fake: FakeBusinessSystem) -> list[str | None]:
    """给假系统加一个声明了 Idempotency-Key 的写操作，返回它收到的键。"""
    seen: list[str | None] = []

    def adjust(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("idempotency-key"))
        return httpx.Response(200, json={"code": 0, "data": {"adjusted": True}})

    fake.spec["paths"]["/api/stock/adjust"] = {  # type: ignore[index]
        "post": {
            "operationId": "adjustStock",
            "tags": ["documents"],
            "summary": "调整库存",
            "x-permission": "stock:doc:write",
            "parameters": [
                {"name": "Idempotency-Key", "in": "header", "required": True, "schema": {}}
            ],
            "responses": {"200": {"description": "OK"}},
        }
    }
    fake.routes[("POST", "/api/stock/adjust")] = adjust
    return seen


async def test_an_approved_write_uses_a_stable_idempotency_key(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    """幂等请求头的值取审批单号：同一张单的执行重来一次，业务系统收到的仍是同一个键。"""
    seen = with_idempotent_op(fake)
    bot, owner, task = await owner_turn(db_session, STAGEABLE[0])
    out = await call(
        client,
        bearer(app, task, owner),
        "systems_call",
        {"system": "stock", "operation_id": "adjustStock"},
    )
    assert out["status"] == "pending_approval" and seen == []
    [row] = await approvals(db_session)
    status, job = await approve_and_run(app, db_engine, db_session, row)
    assert status == "executed" and seen == [str(row.id)]
    # 模拟执行进程在发出之后、记结果之前没了，同一张单的请求再发一次：键不变。
    again = await proxied._execute_system(
        make_session_factory(db_engine),
        bot.id,
        job,
        dict(row.args_enc["calls"][0]["arguments"]),
        issuance(app),
        str(row.id),
    )
    assert again["ok"] is True and seen == [str(row.id), str(row.id)]
    outcomes = list(
        await db_session.scalars(
            select(SystemCall.outcome)
            .where(SystemCall.task_id == job.id)
            .execution_options(populate_existing=True)
        )
    )
    assert outcomes == ["approved_ok", "approved_ok"]


async def test_an_attended_write_still_goes_to_the_confirmation_card(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
) -> None:
    """提问人在飞书里亲自发来的一轮：照 main 进确认卡的待确认列表，不建审批单、不发请求。"""
    from tests.api.test_systems_write_confirm import turn as attended_turn

    _, user, task = await attended_turn(db_session, app)
    for op in ("createDraft", "cancelDocument"):
        args: dict[str, Any] = {"system": "stock", "operation_id": op}
        if op == "createDraft":
            args["body"] = {"sku": "SKU-9", "quantity": 1}
        else:
            args["path_params"] = {"id": "D1"}
            args["body"] = {"reason": "录入错误"}
        out = await call(client, bearer(app, task, user), "systems_call", args)
        assert out.get("status") == "pending_confirmation", out
    assert posts(fake) == [] and await approvals(db_session) == []
    assert fake_tower.submitted == []


# 云签名代理 -------------------------------------------------------------------------------------


def cloud_bearer(app: Any, bot: Bot, owner: User, task: Task) -> dict[str, str]:
    cap = cloud_policy.Capability(
        task_id=task.id,
        bot_id=bot.id,
        user_id=owner.id,
        origin_kind=task.kind,
        session_key=task.session_key,
        relay_session_id=None,
        event_id=None,
        cron_job_id=None,
    )
    token = cloud_policy.issue_capability(app.state.cipher, cap, ttl_seconds=1800)
    return {"Authorization": f"Bearer {token}"}


def writes(router: respx.MockRouter) -> list[dict[str, str]]:
    sent = [dict(urllib.parse.parse_qsl(c.request.content.decode())) for c in router.calls]
    return [
        form for form in sent if aliyun.classify(aliyun.PRODUCTS["slb"], form["Action"]) == "write"
    ]


async def cloud_calls(session: AsyncSession) -> list[CloudCall]:
    return list(
        await session.scalars(
            select(CloudCall).order_by(CloudCall.id).execution_options(populate_existing=True)
        )
    )


@pytest.mark.parametrize("kind", ORIGIN_KINDS)
async def test_an_unattended_low_risk_cloud_write_is_staged(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
    kind: str,
) -> None:
    aliyun_mock.post(SLB_HOST).mock(return_value=httpx.Response(200, json={"RequestId": "r1"}))
    bot, owner, task = await owner_turn(db_session, kind)
    r = await client.post(SLB, content=LOW, headers=cloud_bearer(app, bot, owner, task))
    assert writes(aliyun_mock) == []
    if kind not in STAGEABLE:
        assert r.status_code == 403 and r.json()["error"] == "owner_turn_required"
        # 不能提审批的轮次在风险判定之前就拒，没判定过的按高风险记账。
        assert [(c.risk, c.outcome) for c in await cloud_calls(db_session)] == [
            ("write_high", "denied")
        ]
        assert await approvals(db_session) == [] and fake_tower.submitted == []
        return
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "pending_approval" and "不要重试" in data["hint"]
    [row] = await approvals(db_session)
    assert (row.tool, row.status, str(row.id)) == (
        catalog.CLOUD_CALL,
        "pending",
        data["approval_id"],
    )
    assert row.args_enc["calls"][0]["arguments"] == {
        "product": "slb",
        "params": dict(urllib.parse.parse_qsl(LOW.decode())),
    }
    [sent] = fake_tower.submitted
    assert sent["abstract"] == "云服务 · slb · AddAccessControlListEntry"
    assert "8.8.8.8" in sent["content"] and "低风险" in sent["content"]
    assert [(c.risk, c.outcome, c.status_code) for c in await cloud_calls(db_session)] == [
        ("write_low", "approval_required", 202)
    ]
    # 同一请求再来一次复用那一张。
    again = await client.post(SLB, content=LOW, headers=cloud_bearer(app, bot, owner, task))
    assert again.status_code == 202 and again.json()["duplicate"] is True
    assert len(fake_tower.submitted) == 1


async def test_a_high_risk_cloud_write_is_refused_without_an_approval(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, owner, task = await owner_turn(db_session, STAGEABLE[0])
    r = await client.post(SLB, content=HIGH, headers=cloud_bearer(app, bot, owner, task))
    assert r.status_code == 403 and r.json()["error"] == "owner_turn_required"
    assert "高风险" in r.json()["message"] and r.json()["risk_reasons"]
    assert writes(aliyun_mock) == [] and await approvals(db_session) == []
    assert fake_tower.submitted == []


async def test_an_unknown_action_is_refused_without_an_approval(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, owner, task = await owner_turn(db_session, STAGEABLE[0])
    body = urllib.parse.urlencode({"Action": "DeleteLoadBalancer", "LoadBalancerId": "x"}).encode()
    r = await client.post(SLB, content=body, headers=cloud_bearer(app, bot, owner, task))
    assert r.status_code == 403 and r.json()["error"] == "action_not_allowed"
    assert not aliyun_mock.calls and await approvals(db_session) == []


async def test_an_owner_present_cloud_write_is_never_sent_straight_through(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    """所有者在场的低风险写走确认卡（见 test_assistant_cloud_write_confirm）；这一轮没有可以点卡的
    来源消息，所以不执行、不暂存，也不提审批。"""
    aliyun_mock.post(SLB_HOST).mock(return_value=httpx.Response(200, json={"RequestId": "r1"}))
    bot, owner, task = await owner_turn(db_session, "chat")
    r = await client.post(SLB, content=LOW, headers=cloud_bearer(app, bot, owner, task))
    assert r.status_code == 403 and r.json()["error"] == "confirmation_unavailable"
    assert writes(aliyun_mock) == []
    assert await approvals(db_session) == [] and fake_tower.submitted == []
    assert [(c.risk, c.outcome) for c in await cloud_calls(db_session)] == [("write_low", "denied")]


async def _cloud_staged(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession
) -> tuple[Bot, AssistantApproval]:
    bot, owner, task = await owner_turn(db_session, STAGEABLE[0])
    r = await client.post(SLB, content=LOW, headers=cloud_bearer(app, bot, owner, task))
    assert r.status_code == 202, r.text
    [row] = await approvals(db_session)
    return bot, row


async def test_an_approved_cloud_write_runs_once_as_staged(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    aliyun_mock.post(SLB_HOST).mock(return_value=httpx.Response(200, json={"RequestId": "r9"}))
    bot, row = await _cloud_staged(client, app, db_session)
    status, job = await approve_and_run(app, db_engine, db_session, row)
    assert status == "executed"
    [form] = writes(aliyun_mock)
    assert {k: form[k] for k in ("Action", "AclId", "AclEntrys")} == dict(
        urllib.parse.parse_qsl(LOW.decode())
    )
    executed = [c for c in await cloud_calls(db_session) if c.task_id == job.id]
    assert [(c.risk, c.outcome, c.status_code, c.approval_id) for c in executed] == [
        ("write_low", "ok", 200, row.approval_id)
    ]
    [done] = await approvals(db_session)
    assert done.status == "executed" and "r9" in done.result_enc["results"]["body"]
    assert AK not in json.dumps(done.result_enc)
    assert "来自云服务" in service.result_text(done)
    assert len(await queued(db_session, APPROVAL_RESULT_KIND)) == 1
    again = await proxied.run(
        make_session_factory(db_engine),
        row.id,
        job,
        tower.config(app.state.settings),
        settings=app.state.settings,
        issuance=issuance(app),
    )
    assert again == "executed" and len(writes(aliyun_mock)) == 1
    assert bot.id == done.bot_id


async def test_a_cloud_write_that_became_high_risk_is_not_executed(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    _, row = await _cloud_staged(client, app, db_session)
    # 批准之后部署策略变了：这个 ACL 不再受管，现在执行算高风险。
    configure(app, risk_policy=None)
    status, _ = await approve_and_run(app, db_engine, db_session, row)
    assert status == "failed" and writes(aliyun_mock) == []
    [done] = await approvals(db_session)
    assert done.error_code == "risk_changed"


async def test_a_rejected_cloud_write_is_not_executed(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    _, row = await _cloud_staged(client, app, db_session)
    await decided(db_session, row, "rejected")
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    assert writes(aliyun_mock) == []
    assert uuid.UUID(str(row.id))


async def test_a_cloud_write_is_not_executed_once_the_proxy_is_unconfigured(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    _, row = await _cloud_staged(client, app, db_session)
    app.state.settings = app.state.settings.model_copy(
        update={"aliyun_proxy_access_key_secret": None}
    )
    status, _ = await approve_and_run(app, db_engine, db_session, row)
    assert status == "failed" and writes(aliyun_mock) == [] and fake_tower.redeemed == []
    [done] = await approvals(db_session)
    assert done.error_code == "cloud_proxy_disabled"


async def test_the_worker_routes_proxied_approvals_to_their_proxy(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    fake: FakeBusinessSystem,
    fake_tower: FakeTower,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, row = await staged_system(client, app, db_session)
    await decided(db_session, row)
    monkeypatch.setattr(worker_approvals, "get_settings", lambda: app.state.settings)
    [job] = await queued(db_session, APPROVAL_EXECUTE_KIND)
    claimed = await take(db_session, job.id)
    await worker_approvals.AssistantApprovalExecuteHandler().run(build_ctx(db_engine, claimed))
    [done] = await approvals(db_session)
    assert done.status == "executed" and posts(fake) == [{"sku": "SKU-9", "quantity": 3}]
    assert fake_tower.redeemed[-1][1] == row.request_hash


async def test_a_cloud_write_too_large_to_review_is_not_staged(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    fake_tower: FakeTower,
    aliyun_mock: respx.MockRouter,
) -> None:
    """一个参数值太长（低风险写里只有 ACL 条目的备注能变长）：整张不提交，不调阿里云。"""
    bot, owner, task = await owner_turn(db_session, STAGEABLE[0])
    entries = json.dumps([{"entry": "8.8.8.8", "comment": "c" * 12000}])
    params = {"Action": "AddAccessControlListEntry", "AclId": ACL, "AclEntrys": entries}
    body = urllib.parse.urlencode(sorted(params.items())).encode()
    r = await client.post(SLB, content=body, headers=cloud_bearer(app, bot, owner, task))
    assert r.status_code == 403 and r.json()["error"] == proxied.TOO_LARGE
    assert not aliyun_mock.calls and await approvals(db_session) == []
    assert fake_tower.submitted == []
    assert [(c.risk, c.outcome) for c in await cloud_calls(db_session)] == [("write_low", "denied")]
