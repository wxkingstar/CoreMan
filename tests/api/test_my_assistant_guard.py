"""`/api/me/assistant` 整个前缀的台账：每条路由都只认交互登录的所有者本人，写操作都要 CSRF。

新增一条路由而不登记进 `CASES`，第一个用例就失败：先想清楚它对「别人」「机器人令牌」「没带
CSRF」各自怎么表现，再登记。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, notes, settings
from coreman.core.assistants.kinds import COLD_START_KIND
from coreman.core.db.models import (
    AssistantApproval,
    AssistantJob,
    AssistantLifecycle,
    AuditLog,
    Bot,
    User,
)
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_as, login_existing
from tests.api.my_assistant_helpers import URL, as_bot_token, as_cli, code
from tests.api.test_csrf import _api_routes
from tests.integration.assistant_helpers import SECRET
from tests.integration.job_helpers import owner_with_key, reloaded, spawned, tasks_of

pytestmark = pytest.mark.usefixtures("assistant_master")
PAGE = "规则/口吻"
SAFE = frozenset({"GET"})
# （方法，路由模板）→ 一次合法调用的参数；`{job}` 在用例里换成所有者那个子任务的编号。
CASES: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", ""): {},
    ("GET", "/attention"): {},
    ("GET", "/jobs"): {},
    ("POST", "/jobs/{job_id}/cancel"): {"path": "/jobs/{job}/cancel"},
    ("GET", "/followups"): {},
    ("POST", "/followups"): {"json": {"category": "remind", "title": "新的一条"}},
    ("PATCH", "/followups/{label}"): {"path": "/followups/F001", "json": {"title": "改掉"}},
    ("POST", "/followups/{label}/close"): {"path": "/followups/F001/close", "json": {}},
    ("POST", "/followups/{label}/snooze"): {
        "path": "/followups/F001/snooze",
        "json": {"until": "2099-01-01"},
    },
    ("GET", "/approvals/recent"): {},
    ("GET", "/notes"): {},
    # 知识页的路径只放请求体：读与删都是 POST。
    ("POST", "/notes/read"): {"json": {"path": PAGE}},
    ("PUT", "/notes/page"): {"json": {"path": PAGE, "body": "改掉", "version": 1}},
    ("POST", "/notes/delete"): {"json": {"path": PAGE}},
    ("GET", "/settings"): {},
    ("PATCH", "/settings"): {"json": {"timezone": "UTC"}},
    ("POST", "/shift"): {"json": {"state": "on"}},
    ("POST", "/voice/regenerate"): {},
    # 停用放最后：有的用例按这个顺序把每条路由都调一遍。
    ("POST", "/deprovision"): {"json": {"confirm": True}},
}


def test_every_route_under_the_prefix_is_in_the_table(app: FastAPI) -> None:
    found = {
        (method, route.path.removeprefix(URL))
        for route in _api_routes(app.routes)
        if route.path.startswith(URL)
        for method in route.methods
    }
    assert found == set(CASES)


@dataclass
class Seeded:
    """一位所有者和他的全部内容；`intact` 核对它们一样都没被动过。"""

    bot: Bot
    owner: User
    job: AssistantJob

    async def intact(self, db_session: AsyncSession) -> None:
        db_session.expire_all()
        # 过期之后先读回来：之后取编号不能在同步上下文里触发加载。
        await db_session.refresh(self.bot)
        await db_session.refresh(self.job)
        bot = await db_session.get(Bot, self.bot.id, populate_existing=True)
        assert bot is not None and bot.assistant_state == "active" and bot.enabled
        row = await followups.get(db_session, self.bot.id, 1)
        assert row.status == "open" and row.title_enc == f"续约 {SECRET}"
        page = await notes.read(db_session, self.bot.id, PAGE)
        assert page is not None and page.version == 1 and page.body == f"回复要短。{SECRET}"
        assert (await reloaded(db_session, self.job)).status == "queued"
        flows = await db_session.scalars(
            select(AssistantLifecycle).where(AssistantLifecycle.bot_id == self.bot.id)
        )
        assert list(flows) == []
        prefs = await settings.get(db_session, self.bot.id)
        assert (prefs.timezone, prefs.shift_override) == ("Asia/Shanghai", None)
        assert await tasks_of(db_session, self.bot, COLD_START_KIND) == []


async def _seeded(db_session: AsyncSession) -> Seeded:
    bot, owner = await owner_with_key(db_session)
    await followups.add(
        db_session, bot.id, category="remind", title=f"续约 {SECRET}", due_on=date(2000, 1, 1)
    )
    await notes.write(
        db_session, bot.id, PAGE, body=f"回复要短。{SECRET}", summary="口吻", pinned=True
    )
    db_session.add(
        AssistantApproval(
            bot_id=bot.id,
            tool="feishu_im_send_message",
            args_enc={"abstract": f"私聊 → 李四 {SECRET}", "calls": []},
            args_hash=uuid.uuid4().hex,
            request_hash=uuid.uuid4().hex,
            channel="p2p",
            external_id=f"cm-pa-{uuid.uuid4().hex}",
            expires_at=utcnow() + timedelta(hours=72),
            status="pending",
        )
    )
    job = await spawned(db_session, bot, title=f"退货原因 {SECRET}", brief=f"查一下 {SECRET}")
    await db_session.commit()
    return Seeded(bot, owner, job)


async def _call(
    client: httpx.AsyncClient, seeded: Seeded, method: str, template: str
) -> httpx.Response:
    spec = CASES[(method, template)]
    path = spec.get("path", template).replace("{job}", str(seeded.job.id))
    return await client.request(
        method, f"{URL}{path}", params=spec.get("params"), json=spec.get("json")
    )


async def test_signed_out_gets_nothing(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    seeded = await _seeded(db_session)
    client.headers["X-CSRF-Token"] = "t"
    client.cookies.set("coreman_csrf", "t")
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert response.status_code == 401, (method, template)
        assert SECRET not in response.text
    await seeded.intact(db_session)


async def test_a_bot_token_gets_nothing_even_for_the_owner(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    as_bot_token(client, monkeypatch, seeded.owner)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert response.status_code == 403, (method, template)
        assert SECRET not in response.text
    await seeded.intact(db_session)


async def test_every_write_needs_the_csrf_header(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    del client.headers["X-CSRF-Token"]
    for method, template in CASES:
        if method in SAFE:
            continue
        response = await _call(client, seeded, method, template)
        assert response.status_code == 403, (method, template)
    await seeded.intact(db_session)


@pytest.mark.parametrize("role", ["platform_admin", "ai_committee", "team_lead", "member"])
async def test_someone_without_an_assistant_reaches_nothing_of_the_owners(
    client: httpx.AsyncClient, db_session: AsyncSession, role: str
) -> None:
    seeded = await _seeded(db_session)
    await login_as(client, db_session, role=role)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert SECRET not in response.text, (method, template)
        assert str(seeded.bot.id) not in response.text
        if (method, template) == ("GET", ""):
            assert response.json()["data"]["exists"] is False
        else:
            assert response.status_code == 404 and code(response) == "no_assistant", (
                method,
                template,
            )
    await seeded.intact(db_session)


async def test_an_admin_with_their_own_assistant_only_ever_touches_their_own(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    seeded = await _seeded(db_session)
    theirs, admin = await owner_with_key(
        db_session, chat_id="oc_admin", platform_user_id="admin_pid", open_id="ou_admin"
    )
    admin.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, admin)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert SECRET not in response.text, (method, template)
        assert str(seeded.bot.id) not in response.text
    # 最后一条是停用：停的是管理员自己的助理，所有者的原封不动。
    await seeded.intact(db_session)
    await db_session.refresh(theirs)
    assert theirs.assistant_state == "deprovisioning"


@pytest.mark.parametrize("confirm", ["true", 1, False, None, "yes"])
async def test_stopping_needs_a_literal_true(
    client: httpx.AsyncClient, db_session: AsyncSession, confirm: object
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    response = await client.post(f"{URL}/deprovision", json={"confirm": confirm})
    assert response.status_code == 422, response.text
    assert (await client.post(f"{URL}/deprovision", json={})).status_code == 422
    assert (
        await client.post(f"{URL}/deprovision", json={"confirm": True, "reason": "x"})
    ).status_code == 422
    await seeded.intact(db_session)


async def test_the_owner_stops_their_assistant_once(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    seeded = await _seeded(db_session)
    await login_existing(client, db_session, seeded.owner)
    response = await client.post(f"{URL}/deprovision", json={"confirm": True})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"state": "deprovisioning"}
    assert response.headers["cache-control"] == "no-store"
    await db_session.refresh(seeded.bot)
    assert seeded.bot.assistant_state == "deprovisioning"
    [flow] = list(
        await db_session.scalars(
            select(AssistantLifecycle).where(AssistantLifecycle.bot_id == seeded.bot.id)
        )
    )
    assert (flow.action, flow.step, flow.trigger) == ("deprovision", "drain", "owner")
    assert flow.actor_id == seeded.owner.id
    # 再点一次不重复做，也不重复记审计。
    again = await client.post(f"{URL}/deprovision", json={"confirm": True})
    assert again.json()["data"] == {"state": "deprovisioning"}
    audits = list(
        await db_session.scalars(select(AuditLog).where(AuditLog.action == "assistant.deprovision"))
    )
    assert len(audits) == 1 and audits[0].actor_id == seeded.owner.id
    assert audits[0].target_id == str(seeded.bot.id)
    # 停用开始之后：还能看，不能再改。
    assert (await client.get(f"{URL}/followups")).status_code == 200
    assert (await client.get(URL)).json()["data"]["state"] == "deprovisioning"
    refused = await client.post(f"{URL}/followups/F001/close", json={})
    assert refused.status_code == 409 and code(refused) == "assistant_not_active"


async def test_a_cli_session_gets_nothing_even_as_the_owner(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # 管理命令能以任意成员的身份建会话：那不是本人在场，整个前缀都不受理。
    seeded = await _seeded(db_session)
    await as_cli(client, db_session, seeded.owner)
    for method, template in CASES:
        response = await _call(client, seeded, method, template)
        assert response.status_code == 403, (method, template)
        assert code(response) == "cli_session_refused" and SECRET not in response.text
    await seeded.intact(db_session)
