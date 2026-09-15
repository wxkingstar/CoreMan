import pytest
from sqlalchemy import select

from coreman.core.bus import outbox
from coreman.core.chat import interactions
from coreman.core.db.models import OutboxItem
from coreman.core.wecom.cards import answered_card, question_task_id
from coreman.runtime.worker.choice_flow import advance_choice
from tests.integration.test_card_actions import Q1, _state
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx, seed_bot


@pytest.mark.parametrize(
    "chat,status,updated",
    [("zs", "sent", True), ("other", "sent", False), ("zs", "pending", False)],
)
async def test_text_answer_updates_only_delivered_card_in_same_chat(
    db_engine, db_session, chat, status, updated
):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    st = await _state(db_session, bot, questions=[Q1])
    await interactions.patch_state(db_session, st.id, {"waiting_for_text": True})
    await db_session.refresh(st)
    tid = question_task_id(st.task_id_prefix, 0)
    sent = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="qa-card",
        target={"chat_id": chat},
        payload={"card": {"task_id": tid}, "_feishu_message_id": "om_qa"},
    )
    sent.status = status
    t = await chat_task(db_session, bot, "NEPTUNE")
    await advance_choice(
        db_session,
        ctx=build_ctx(db_engine, t),
        bot=bot,
        st=st,
        questions=[Q1],
        answers=["NEPTUNE"],
        index=0,
        chat_id="zs",
        chat_type="single",
        platform_user_id="zs",
        icon_url="",
    )
    updates = list(
        await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    )
    assert len(updates) == int(updated)
    if updated:
        assert updates[0].target == {"message_id": "om_qa", "chat_id": "zs", "task_id": tid}
        assert updates[0].payload["card"] == answered_card(
            Q1, index=0, total=1, task_id=tid, answer="NEPTUNE", is_last=True, icon_url=""
        )
    await db_session.refresh(st)
    assert st.state["answers"] == ["NEPTUNE"]
    assert st.state["waiting_for_text"] is False
