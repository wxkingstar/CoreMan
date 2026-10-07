"""富卡片在各出口的接入：提示词、定时任务分片、管理台聊天记录、企微流式降级。"""

from datetime import UTC, datetime

from coreman.core.cron.delivery import CHAT_PART_BYTES, chat_parts
from coreman.core.prompting.rich_cards import RICH_CARDS_PROMPT, rich_cards_prompt
from coreman.core.richtext.blocks import split
from coreman.core.richtext.degrade import readable
from coreman.core.richtext.schema import BLOCK_KINDS
from coreman.core.wecom.stream_render import StreamView, render_wecom_stream

KPI = '结论。\n\n```card:kpi\n{"items": [{"label": "订单", "value": 97}]}\n```'


def test_prompt_only_for_feishu_with_switch_on() -> None:
    assert rich_cards_prompt("feishu", True) == RICH_CARDS_PROMPT
    assert rich_cards_prompt("feishu", False) == ""
    assert rich_cards_prompt("wecom", True) == ""


def test_prompt_documents_every_block_type_the_model_may_write() -> None:
    # raw 是给技能作者的进阶入口，不教给模型。
    for kind in BLOCK_KINDS - {"raw"}:
        assert f"- {kind}:" in RICH_CARDS_PROMPT, kind


def test_prompt_documents_reply_buttons_and_people_lookup() -> None:
    assert '{"text": "…", "reply": true}' in RICH_CARDS_PROMPT
    assert "sends its own text" in RICH_CARDS_PROMPT
    assert "only the person who asked" in RICH_CARDS_PROMPT
    assert "nobody is notified" in RICH_CARDS_PROMPT


def test_prompt_examples_are_valid_blocks() -> None:
    """模型会照着例子写：例子里的块必须都能通过校验。"""
    blocks = [s for s in split(RICH_CARDS_PROMPT) if s.kind != "markdown"]
    assert [s.block for s in blocks] == ["header", "callout", "kpi", "chart"]
    assert all(s.kind == "block" for s in blocks), [s.error for s in blocks]


def test_feishu_cron_result_is_not_split_across_blocks() -> None:
    content = (KPI + "\n\n" + "说明。" * 3000) * 2
    assert len(content.encode()) > CHAT_PART_BYTES
    assert chat_parts("feishu", content, "（已截断）") == [content]
    assert len(chat_parts("wecom", content, "（已截断）")) > 1


def test_readable_degrades_only_when_blocks_present() -> None:
    plain = "普通 **Markdown**，<font color='green'>不动</font>。"
    assert readable(plain) == plain
    shown = readable(KPI)
    assert "```card" not in shown
    assert "**订单**" in shown


def test_wecom_stream_shows_degraded_blocks() -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    view = StreamView(
        thinking_md="",
        pending_text="",
        final_text=KPI,
        is_complete=True,
        running_since=now,
        session_url=None,
    )
    out = render_wecom_stream(view, now)
    assert "```card" not in out
    assert "订单" in out
