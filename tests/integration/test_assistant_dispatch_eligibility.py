"""调度的资格：目标员工启用、不是助理、开启接受求助、所有者在它的可用人范围内。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import dispatch
from coreman.core.db.models import BotAllowedUser, User
from tests.integration.assistant_helpers import seed_assistant
from tests.integration.dispatch_helpers import target_bot
from tests.integration.test_assistant_platform_turn_handler import assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_an_open_employee_is_listed_and_resolves_by_key_or_name(
    db_session: AsyncSession,
) -> None:
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session, key="demo-bot", name="库存助手")
    rows = await dispatch.candidates(db_session, bot)
    assert [row.id for row in rows] == [target.id]
    assert dispatch.listing(rows) == [
        {"bot": "demo-bot", "name": "库存助手", "description": "查库存与订单"}
    ]
    assert (await dispatch.resolve(db_session, bot, "demo-bot")).id == target.id
    assert (await dispatch.resolve(db_session, bot, "  库存助手 ")).id == target.id
    found, speaker = await dispatch.authorize(db_session, bot, target.id)
    assert found.id == target.id and speaker.user_id == owner.id


async def test_an_allowed_list_must_name_the_owner(db_session: AsyncSession) -> None:
    bot, owner = await assistant(db_session)
    stranger = User(login_name="stranger", display_name="王五", source="sync")
    db_session.add(stranger)
    await db_session.flush()
    closed = await target_bot(db_session, key="closed-bot", name="财务助手", allowed=[stranger.id])
    assert await dispatch.candidates(db_session, bot) == []
    with pytest.raises(dispatch.DispatchError) as refused:
        await dispatch.resolve(db_session, bot, "closed-bot")
    assert refused.value.code == "bot_not_available"
    # 把所有者加进可用人名单之后就调得动。
    db_session.add(BotAllowedUser(bot_id=closed.id, user_id=owner.id))
    await db_session.commit()
    assert (await dispatch.resolve(db_session, bot, "closed-bot")).id == closed.id
    assert [row.id for row in await dispatch.candidates(db_session, bot)] == [closed.id]


@pytest.mark.parametrize(
    ("shape", "code"),
    [
        ({"accept": False}, "bot_not_available"),
        ({"enabled": False}, "bot_not_found"),
        ({"platform": "wecom"}, "bot_not_found"),
    ],
)
async def test_employees_that_cannot_be_dispatched(
    db_session: AsyncSession, shape: dict[str, object], code: str
) -> None:
    bot, _ = await assistant(db_session)
    target = await target_bot(db_session, **shape)  # type: ignore[arg-type]
    assert await dispatch.candidates(db_session, bot) == []
    with pytest.raises(dispatch.DispatchError) as refused:
        await dispatch.resolve(db_session, bot, target.bot_key)
    assert refused.value.code == code
    with pytest.raises(dispatch.DispatchError) as direct:
        await dispatch.authorize(db_session, bot, target.id)
    assert direct.value.code == code


async def test_an_assistant_is_never_a_target(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    other, _ = await seed_assistant(db_session)
    assert await dispatch.candidates(db_session, bot) == []
    for ref in (other.bot_key, bot.bot_key):
        with pytest.raises(dispatch.DispatchError) as refused:
            await dispatch.resolve(db_session, bot, ref)
        assert refused.value.code == "bot_not_found"
    for target_id in (other.id, bot.id, uuid.uuid4()):
        with pytest.raises(dispatch.DispatchError) as direct:
            await dispatch.authorize(db_session, bot, target_id)
        assert direct.value.code == "bot_not_found"


async def test_a_name_shared_by_two_employees_needs_the_key(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    first = await target_bot(db_session, key="demo-bot-a", name="数据助手")
    await target_bot(db_session, key="demo-bot-b", name="数据助手")
    with pytest.raises(dispatch.DispatchError) as refused:
        await dispatch.resolve(db_session, bot, "数据助手")
    assert refused.value.code == "bot_ambiguous"
    assert (await dispatch.resolve(db_session, bot, "demo-bot-a")).id == first.id
    for bad in ("", "   ", "x" * 101):
        with pytest.raises(dispatch.DispatchError) as invalid:
            await dispatch.resolve(db_session, bot, bad)
        assert invalid.value.code == "invalid_bot"


async def test_an_owner_who_is_no_longer_himself_cannot_dispatch(
    db_session: AsyncSession,
) -> None:
    bot, owner = await assistant(db_session)
    target = await target_bot(db_session)
    owner.status = "disabled"
    await db_session.commit()
    with pytest.raises(dispatch.DispatchError) as refused:
        await dispatch.authorize(db_session, bot, target.id)
    assert refused.value.code == "assistant_unavailable"
    owner.status = "active"
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    with pytest.raises(dispatch.DispatchError) as stopping:
        await dispatch.resolve(db_session, bot, target.bot_key)
    assert stopping.value.code == "assistant_unavailable"


def test_questions_are_bounded() -> None:
    assert dispatch.clean_question("  查一下库存  ") == "查一下库存"
    assert len(dispatch.clean_question("问" * dispatch.MAX_QUESTION)) == 4000
    for bad, code in (("", "invalid_question"), ("  \n ", "invalid_question")):
        with pytest.raises(dispatch.DispatchError) as refused:
            dispatch.clean_question(bad)
        assert refused.value.code == code
    with pytest.raises(dispatch.DispatchError) as long:
        dispatch.clean_question("问" * (dispatch.MAX_QUESTION + 1))
    assert long.value.code == "question_too_long"
    job_id = uuid.uuid4()
    assert dispatch.session_key_for(job_id) == f"assistant-help:{job_id}"


async def test_a_key_that_is_someone_elses_name_does_not_win_silently(
    db_session: AsyncSession,
) -> None:
    bot, _ = await assistant(db_session)
    # 一位员工的标识恰好是另一位的显示名：按这个串找人，不能悄悄落到标识那一位头上。
    keyed = await target_bot(db_session, key="stock", name="别的员工")
    named = await target_bot(db_session, key="demo-bot-b", name="Stock")
    with pytest.raises(dispatch.DispatchError) as refused:
        await dispatch.resolve(db_session, bot, "stock")
    assert refused.value.code == "bot_ambiguous"
    assert [row.bot_key for row in refused.value.bots] == ["stock", "demo-bot-b"]
    # 候选带着编号：标识与名字相撞的那一位只能靠编号指明。
    assert (await dispatch.resolve(db_session, bot, str(keyed.id))).id == keyed.id
    assert (await dispatch.resolve(db_session, bot, "demo-bot-b")).id == named.id
    assert (await dispatch.resolve(db_session, bot, "别的员工")).id == keyed.id
    # 同名的两位也带候选。
    await target_bot(db_session, key="demo-bot-c", name="别的员工")
    with pytest.raises(dispatch.DispatchError) as same:
        await dispatch.resolve(db_session, bot, "别的员工")
    assert same.value.code == "bot_ambiguous"
    assert [row.bot_key for row in same.value.bots] == ["demo-bot-c", "stock"]
    # 编号指到的员工照样要过资格。
    with pytest.raises(dispatch.DispatchError) as missing:
        await dispatch.resolve(db_session, bot, str(uuid.uuid4()))
    assert missing.value.code == "bot_not_found"
