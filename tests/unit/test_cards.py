from coreman.core.wecom.cards import (
    answered_card,
    choice_card,
    clip,
    expired_card,
    format_answers,
    make_choice_prefix,
    make_ratelimit_prefix,
    notice_card,
    parse_task_id,
    question_brief,
    question_task_id,
    safe_segment,
    switch_offer_card,
    waiting_card,
)

Q = {
    "question": "用哪个框架？",
    "header": "框架",
    "options": [{"label": "Next.js", "description": "全栈"}, {"label": "Remix", "description": ""}],
    "multiSelect": False,
}
ICON = "https://cdn.example.com/i.png"


def test_prefix_and_task_id_roundtrip() -> None:
    prefix = make_choice_prefix("sales:bot", "wo.abc", now=1700000000.9, rnd="0123456789abcdef")
    assert prefix == "choice@sales_bot@wo_abc@170000000001234567"
    assert question_task_id(prefix, 2) == prefix + "@2"
    assert parse_task_id(prefix + "@2") == ("choice", prefix, 2)
    rl = make_ratelimit_prefix("b", "u", now=5.0)
    assert rl == "ratelimit_switch@b@u@5" and parse_task_id(rl + "@0") == (
        "ratelimit_switch",
        rl,
        0,
    )
    assert parse_task_id("other@x@1") is None and parse_task_id("choice@x@y") is None
    # 序号段只认 ASCII 数字：② 这类 isdigit() 为真的字符会让 int() 抛 ValueError。
    assert parse_task_id("choice@a@b@1@②") is None
    long = make_choice_prefix("b" * 60, "u" * 60, now=1.0, rnd="abcdefgh")
    assert len(long + "@99") <= 128 and long.startswith("choice@")
    assert safe_segment("a:b|c d") == "a_b_c_d"


def test_clip() -> None:
    assert clip("十一个字的选项文案ABC", 11) == "十一个字的选项文案A…"
    assert clip("短", 11) == "短"


def test_choice_card_and_brief_verbatim() -> None:
    card = choice_card(Q, index=0, total=2, task_id="choice@b@u@1@0", icon_url=ICON)
    assert card == {
        "card_type": "vote_interaction",
        "source": {"icon_url": ICON, "desc": "AI 助手"},
        "main_title": {"title": "❓ 问题 1/2 · 框架", "desc": "用哪个框架？"},
        "checkbox": {
            "question_key": "choice_answer",
            "option_list": [
                {"id": "opt_0", "text": "Next.js", "is_checked": False},
                {"id": "opt_1", "text": "Remix", "is_checked": False},
                {"id": "opt_other", "text": "其他（发消息输入）", "is_checked": False},
            ],
            "mode": 0,
            "disable": False,
        },
        "submit_button": {"text": "确认选择", "key": "submit_choice"},
        "task_id": "choice@b@u@1@0",
    }
    single = choice_card(
        {**Q, "header": "", "multiSelect": True}, index=0, total=1, task_id="t", icon_url=""
    )
    assert single["main_title"]["title"] == "❓ 请选择" and single["checkbox"]["mode"] == 1
    assert single["source"] == {"desc": "AI 助手"}
    assert question_brief(Q, index=0, total=2) == (
        "**问题 1/2** · 框架\n用哪个框架？\n\n- **Next.js** — 全栈\n- **Remix**\n"
        "- **其他** — 直接发送文字作为回答"
    )
    assert question_brief({**Q, "header": ""}, index=0, total=1).startswith("**请选择**\n")


def test_answered_waiting_expired_notice_cards() -> None:
    a = answered_card(Q, index=1, total=2, task_id="t", answer="Remix", is_last=True, icon_url="")
    assert a == {
        "card_type": "text_notice",
        "source": {"desc": "AI 助手"},
        "main_title": {"title": "✅ 已答 · 问题 2/2", "desc": "用哪个框架？"},
        "sub_title_text": "您的回答：Remix\n⏳ 正在生成结果，完成后自动推送…",
        "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        "task_id": "t",
    }
    b = answered_card(
        Q, index=0, total=1, task_id="t", answer="(未选择)", is_last=False, icon_url=""
    )
    assert b["main_title"]["title"] == "✅ 已回答" and b["sub_title_text"] == "您的回答：(未选择)"
    w = waiting_card(Q, task_id="t", icon_url="")
    assert w["main_title"] == {"title": "✏️ 请输入您的答案", "desc": "用哪个框架？"}
    assert w["checkbox"] == {
        "question_key": "choice_waiting",
        "option_list": [
            {"id": "waiting_0", "text": "等待输入中，请直接发送消息", "is_checked": True}
        ],
        "mode": 0,
        "disable": True,
    }
    assert w["submit_button"] == {"text": "等待输入", "key": "submit_waiting"}
    e = expired_card("t", icon_url="")
    assert e["main_title"] == {"title": "已过期", "desc": "选择会话已过期，请重新发送消息"}
    assert e["card_action"] == {"type": 0} and e["card_type"] == "text_notice"
    n = notice_card(
        "t", title="⏳ 已选择继续等待", desc="继续使用 **A** 等待额度恢复。", icon_url=ICON
    )
    assert n["source"] == {"icon_url": ICON, "desc": "服务调度", "desc_color": 0}
    assert n["card_action"] == {"type": 1, "url": "https://work.weixin.qq.com"}


def test_switch_offer_card_and_format_answers() -> None:
    c = switch_offer_card(
        task_id="ratelimit_switch@b@u@1@0",
        current_name="claude01",
        target_name="claude07",
        pct_text="12%",
        icon_url="",
    )
    assert c["main_title"] == {
        "title": "⚠️ 当前运行时已触发额度限制",
        "desc": (
            "运行时 **claude01** 已耗尽，是否切换到周额度更空闲的"
            "运行时 **claude07**（7天使用率 12%）？"
        ),
    }
    assert c["checkbox"]["question_key"] == "ratelimit_switch_choice" and c["checkbox"]["mode"] == 0
    assert c["checkbox"]["option_list"] == [
        {"id": "opt_switch", "text": "切换到 claude07", "is_checked": False},
        {"id": "opt_wait", "text": "保持 claude01 等待额度恢复", "is_checked": False},
    ]
    assert c["submit_button"] == {"text": "确定", "key": "ratelimit_switch_submit"}
    assert c["source"] == {"desc": "AI 助手 · 服务调度"}
    assert (
        format_answers([Q, {**Q, "question": "第二题"}], ["Remix"])
        == "[用户选择回答]\n1. 用哪个框架？ -> Remix\n2. 第二题 -> (未回答)"
    )
