"""群里真人只有发话人自己：这一轮的安全策略全部按私聊走（个人工具、确认卡、对话记录可见性）。

真人数每一轮实时问飞书；多一个人、外部群、查不到都按普通群。判断结果记在任务上，
服务端每次调用个人工具时凭它和来源事件一起核验。
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from coreman.core.chat import solo_group
from coreman.core.db.models import ChatLog, InboundEvent, Task
from coreman.core.feishu_docs import policy as docs_policy
from coreman.core.feishu_personal import policy
from coreman.runtime.worker.chat import solo
from coreman.runtime.worker.chat.feishu_docs import configure as configure_docs
from coreman.runtime.worker.chat.personal import configure as configure_personal
from tests.api.test_feishu_personal import grant, setup
from tests.api.test_feishu_personal_worker import intake_for
from tests.integration.worker_helpers import build_ctx

BOTH = {"claude": {"feishu_personal_tools_v1": True, "feishu_group_docs_v1": True}}


class Humans:
    """替换实时查询：按顺序回答这一次是不是单人群，并记下每次问的是谁。"""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, cipher: Any, bot: Any, chat_id: str, platform_user_id: str) -> bool:
        self.asked.append((chat_id, platform_user_id))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture
def humans(monkeypatch):
    def install(*answers: bool) -> Humans:
        fake = Humans(*answers)
        monkeypatch.setattr(solo_group, "solo", fake)
        return fake

    return install


async def group_turn(db_session, app, text="把结果写进原表格，加一列"):
    bot, user, task = await setup(db_session, app, chat_type="group")
    await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, text, BOTH)
    return bot, user, task, intake


async def payload_of(db_session, task) -> dict[str, Any]:
    row = await db_session.get(Task, task.id, populate_existing=True)
    return dict(row.payload)


# ---- 服务端核验：个人工具每次调用都按来源事件 + 这一轮的判断核验 ----


async def test_group_turn_without_the_mark_gets_no_personal_scope(db_session, app):
    _, user, task = await setup(db_session, app, chat_type="group")
    with pytest.raises(ValueError, match="feishu_private_chat_required"):
        await policy.task_scope(db_session, task.id, str(user.id))


async def test_marked_solo_group_turn_gets_the_private_scope(db_session, app):
    _, user, task = await setup(db_session, app, chat_type="group")
    task.payload = {**task.payload, solo_group.PAYLOAD_KEY: True}
    await db_session.commit()
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    assert scope.user_id == user.id and scope.chat_id == "oc_group"


async def test_the_mark_does_not_turn_a_forged_group_event_private(db_session, app):
    _, user, task = await setup(db_session, app, chat_type="group")
    task.payload = {**task.payload, solo_group.PAYLOAD_KEY: True}
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    raw = event.payload["raw"]
    # 原始事件里的会话不是这个群：照旧拒绝。
    event.payload = {
        **event.payload,
        "raw": {
            **raw,
            "event": {**raw["event"], "message": {"chat_type": "group", "chat_id": "x"}},
        },
    }
    await db_session.commit()
    with pytest.raises(ValueError, match="verified_private_origin_required"):
        await policy.task_scope(db_session, task.id, str(user.id))


async def test_the_mark_is_only_for_the_speaker(db_session, app):
    _, _, task = await setup(db_session, app, chat_type="group")
    task.payload = {**task.payload, solo_group.PAYLOAD_KEY: True}
    await db_session.commit()
    with pytest.raises(ValueError, match="personal_actor_mismatch"):
        await policy.task_scope(db_session, task.id, str(uuid.uuid4()))


async def test_systems_confirmation_cards_follow_the_private_branch(db_session, app):
    from coreman.core.systems_catalog.confirm import confirmable_origin

    _, user, task = await setup(db_session, app, chat_type="group")
    task.payload = {**task.payload, solo_group.PAYLOAD_KEY: True}
    await db_session.commit()
    assert await confirmable_origin(db_session, task.id, user.id) == ("oc_group", "single")


async def test_systems_confirmation_in_an_ordinary_group_stays_a_group_card(db_session, app):
    from coreman.core.systems_catalog.confirm import confirmable_origin

    _, user, task = await setup(db_session, app, chat_type="group")
    origin = await confirmable_origin(db_session, task.id, user.id)
    assert origin is None or origin[1] == "group"


# ---- worker：每一轮实时判断，写进任务，按私聊或群挂工具 ----


async def test_one_human_mounts_private_tools_instead_of_group_rules(
    db_session, app, db_engine, humans
):
    fake = humans(True)
    _, user, task, intake = await group_turn(db_session, app)
    ctx = build_ctx(db_engine, task)
    intake = await solo.apply(db_session, ctx, intake)
    assert intake.solo and intake.private
    assert fake.asked == [("oc_group", "human")]
    assert (await payload_of(db_session, task))[solo_group.PAYLOAD_KEY] is True
    prompt, env = await configure_personal(db_session, ctx, intake, uuid.uuid4(), "", {})
    assert "# 本人飞书" in prompt and env[policy.PREFIX + "TOKEN"]
    # 群文档的收紧规则（只认群里贴过的链接、整篇重写拒绝、没有块编号）不再挂。
    docs = await configure_docs(db_session, ctx, intake, uuid.uuid4(), "", {})
    assert docs == ("", {})
    # 个人工具服务端核验通过。
    assert (await policy.task_scope(db_session, task.id, str(user.id))).chat_id == "oc_group"


async def test_two_humans_keep_the_group_rules(db_session, app, db_engine, humans):
    humans(False)
    _, user, task, intake = await group_turn(db_session, app)
    ctx = build_ctx(db_engine, task)
    intake = await solo.apply(db_session, ctx, intake)
    assert not intake.solo and not intake.private
    assert solo_group.PAYLOAD_KEY not in await payload_of(db_session, task)
    assert await configure_personal(db_session, ctx, intake, uuid.uuid4(), "", {}) == ("", {})
    prompt, env = await configure_docs(db_session, ctx, intake, uuid.uuid4(), "", {})
    assert "# 群里的飞书文档" in prompt and env[docs_policy.PREFIX + "TOKEN"]
    with pytest.raises(ValueError):
        await policy.task_scope(db_session, task.id, str(user.id))


async def test_a_failed_lookup_counts_as_an_ordinary_group(db_session, app, db_engine):
    # 真的去查：机器人凭据解不开，查不到人数，按普通群。
    bot, _, task, intake = await group_turn(db_session, app)
    bot.credentials_enc = "broken"
    await db_session.commit()
    ctx = build_ctx(db_engine, task)
    intake = await solo.apply(db_session, ctx, intake)
    assert not intake.solo
    assert solo_group.PAYLOAD_KEY not in await payload_of(db_session, task)


async def test_membership_change_flips_the_next_check(db_session, app, db_engine, humans):
    fake = humans(True, False)
    _, user, task, intake = await group_turn(db_session, app)
    first = await solo.apply(db_session, build_ctx(db_engine, task), intake)
    assert first.solo
    await policy.task_scope(db_session, task.id, str(user.id))
    # 有人进群：再判断一次（重新认领的同一任务也一样）就回到群规则，标记被清掉。
    task = await db_session.get(Task, task.id, populate_existing=True)
    second = await solo.apply(db_session, build_ctx(db_engine, task), intake)
    assert not second.solo and len(fake.asked) == 2
    assert solo_group.PAYLOAD_KEY not in await payload_of(db_session, task)
    with pytest.raises(ValueError, match="feishu_private_chat_required"):
        await policy.task_scope(db_session, task.id, str(user.id))


async def test_a_stale_mark_is_cleared_when_the_turn_is_not_eligible(
    db_session, app, db_engine, humans
):
    fake = humans(True)
    _, _, task, intake = await group_turn(db_session, app)
    task.payload = {**task.payload, solo_group.PAYLOAD_KEY: True, "origin": "observe"}
    await db_session.commit()
    result = await solo.apply(db_session, build_ctx(db_engine, task), intake)
    assert not result.solo and fake.asked == []
    assert solo_group.PAYLOAD_KEY not in await payload_of(db_session, task)


@pytest.mark.parametrize("case", ["private", "observe", "dispatch", "assistant", "work", "unknown"])
async def test_turns_outside_the_rule_are_not_checked(db_session, app, db_engine, humans, case):
    from dataclasses import replace

    from coreman.core.prompting import Speaker

    fake = humans(True)
    bot, user, task = await setup(
        db_session, app, chat_type="single" if case == "private" else "group"
    )
    if case in ("observe", "dispatch"):
        task.payload = {**task.payload, "origin": case}
    await db_session.commit()
    intake = await intake_for(db_session, bot, task, "hi", BOTH)
    if case == "assistant":
        # 只改内存里的这一份：个人助理的其余字段约束与这里无关。
        db_session.expunge(bot)
        bot.kind = "assistant"
    if case == "work":
        intake = replace(intake, session_key="work:oc_group:T1")
    if case == "unknown":
        intake = replace(intake, speaker=Speaker("human", None, None, None))
    result = await solo.apply(db_session, build_ctx(db_engine, task), intake)
    assert not result.solo and fake.asked == []
    assert result.private is (case == "private")


async def test_the_whole_turn_runs_as_a_private_chat(db_session, app, db_engine, humans):
    """端到端：单人群里 @ 它，请求带个人工具、不带群文档规则，对话记录只有本人能看。"""
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay

    humans(True)
    bot, user, task, intake = await group_turn(db_session, app)
    message = "把结果写进原表格，加一列"
    intake.inbound.payload = {
        **intake.inbound.payload,
        "parts": [{"type": "text", "text": message}],
    }
    task.payload = {**task.payload, "message": intake.inbound.payload}
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    request = fake.requests[0]
    system = request["messages"][0]["content"]
    assert "# 本人飞书" in system and "# 群里的飞书文档" not in system
    assert "按私聊处理" in request["messages"][-1]["content"]
    assert request["env_vars"][policy.PREFIX + "TOKEN"]
    assert docs_policy.PREFIX + "TOKEN" not in request["env_vars"]
    log = (await db_session.scalars(select(ChatLog).where(ChatLog.task_id == task.id))).one()
    assert log.private is True and log.chat_type == "group"


async def test_the_whole_turn_in_a_shared_group_keeps_group_rules(
    db_session, app, db_engine, humans
):
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay

    humans(False)
    bot, user, task, intake = await group_turn(db_session, app)
    message = "把结果写进原表格，加一列"
    intake.inbound.payload = {
        **intake.inbound.payload,
        "parts": [{"type": "text", "text": message}],
    }
    task.payload = {**task.payload, "message": intake.inbound.payload}
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    request = fake.requests[0]
    system = request["messages"][0]["content"]
    assert "# 本人飞书" not in system and "# 群里的飞书文档" in system
    assert "按私聊处理" not in request["messages"][-1]["content"]
    assert policy.PREFIX + "TOKEN" not in request["env_vars"]
    log = (await db_session.scalars(select(ChatLog).where(ChatLog.task_id == task.id))).one()
    assert log.private is False


async def test_connect_feishu_works_in_a_solo_group(db_session, app, db_engine, humans):
    from coreman.runtime.worker.chat.personal import validate

    humans(True)
    _, user, task, intake = await group_turn(db_session, app, "连接飞书")
    ctx = build_ctx(db_engine, task)
    intake = await solo.apply(db_session, ctx, intake)
    scope = await validate(db_session, ctx, intake)
    assert scope.user_id == user.id


async def test_a_resume_turn_is_checked_again(db_session, app, db_engine, humans):
    from tests.integration.credential_helpers import resume_task

    fake = humans(True)
    bot, user, task = await setup(db_session, app, chat_type="group")
    resume = await resume_task(db_session, task, user)
    intake = await intake_for(db_session, bot, resume, "已提交凭证", BOTH)
    result = await solo.apply(db_session, build_ctx(db_engine, resume), intake)
    assert result.solo and len(fake.asked) == 1
    scope = await policy.task_scope(db_session, resume.id, str(user.id))
    assert scope.task.id == resume.id
