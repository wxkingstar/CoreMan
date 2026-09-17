"""企微模板卡片（vote_interaction / text_notice）渲染与 task_id 编码。

卡面文字全部来自 `coreman/core/i18n/messages.py`（zh 默认文案），这里只管结构；
`text_notice` 必须带 `card_action`（缺了企微回 42045）。
"""

from __future__ import annotations

import re
from typing import Any

from coreman.core.i18n.messages import msg

TASK_ID_MAX = 128
_UNSAFE = re.compile(r"[^0-9A-Za-z_\-]")
_DIGITS = re.compile(r"[0-9]+")
WORK_URL = "https://work.weixin.qq.com"


def safe_segment(value: str) -> str:
    """task_id 的每一段只留 `[0-9A-Za-z_-]`：bot_key 与 open_userid 里的 `:` `.` 都会串段。

    非 ASCII 字符一律换成单字节的 `_`，所以下面按字符数算的长度就是字节数上界。
    """
    return _UNSAFE.sub("_", value)


def _fit(kind: str, bot_key: str, user: str, tail: str) -> str:
    """`{kind}@{bot}@{user}@{tail}` 加上最长 `@99` 后不得超过 128 字节：先截 user 再截 bot。

    企微对 task_id 有 128 字节硬上限，超了整张卡片都发不出去。`tail` 带随机量、`kind` 决定
    回调走哪条分支，两者都不能动；能牺牲的只有可读性那部分，各自至少留 8 个字符。
    """
    bot, usr = safe_segment(bot_key), safe_segment(user)
    fixed = len(kind) + len(tail) + 3 + 3  # 三个 @ 与末尾 "@99"
    room = TASK_ID_MAX - fixed
    if len(bot) + len(usr) > room:
        usr = usr[: max(8, room - len(bot))]
    if len(bot) + len(usr) > room:
        bot = bot[: max(8, room - len(usr))]
    return f"{kind}@{bot}@{usr}@{tail}"


def make_choice_prefix(bot_key: str, platform_user_id: str, *, now: float, rnd: str) -> str:
    """一轮 AskUserQuestion 的 task_id 前缀：同一轮的每道题共用它，只有末尾序号不同。"""
    return _fit("choice", bot_key, platform_user_id, f"{int(now)}{rnd[:8]}")


def make_ratelimit_prefix(bot_key: str, platform_user_id: str, *, now: float) -> str:
    return _fit("ratelimit_switch", bot_key, platform_user_id, str(int(now)))


def question_task_id(prefix: str, index: int) -> str:
    return f"{prefix}@{index}"


def parse_task_id(task_id: str) -> tuple[str, str, int] | None:
    """回调里的 task_id 拆成 `(kind, prefix, index)`；形状不对返回 None（别的卡片不归我们管）。"""
    kind, sep, _rest = task_id.partition("@")
    if not sep or kind not in ("choice", "ratelimit_switch"):
        return None
    prefix, sep, idx = task_id.rpartition("@")
    # 序号段只认 ASCII 数字：`isdigit()` 对「②」也为真，而 `int()` 只吃 Nd 类会抛 ValueError；
    # 我们自己发的 task_id 里这一段永远是 `question_task_id` 写的十进制整数。
    if not sep or not _DIGITS.fullmatch(idx) or prefix.count("@") < 3:
        return None
    return kind, prefix, int(idx)


def clip(text: str, limit: int) -> str:
    """企微对选项文案等字段有字数上限，超了整张卡片会被拒。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _source(icon_url: str, desc: str, *, color: bool = False) -> dict[str, Any]:
    """`source.icon_url` 传空串企微会报错，没配图标就整个字段不发。"""
    src: dict[str, Any] = {}
    if icon_url:
        src["icon_url"] = icon_url
    src["desc"] = desc
    if color:
        src["desc_color"] = 0
    return src


def _title(index: int, total: int, header: str, *, prefix: str, single: str) -> str:
    title = f"{prefix} {index + 1}/{total}" if total > 1 else single
    return f"{title} · {header}" if header else title


def choice_card(
    question: dict[str, Any],
    *,
    index: int,
    total: int,
    task_id: str,
    icon_url: str,
    locale: str = "zh",
) -> dict[str, Any]:
    """一道待答的选择题。末尾永远追加「其他」：用户想自己打字时点它，卡片换成 waiting_card。"""
    options = [
        {
            "id": f"opt_{i}",
            "text": clip(str(o.get("label") or msg("card_option_fallback", locale, n=i + 1)), 11),
            "is_checked": False,
        }
        for i, o in enumerate(question.get("options") or [])
    ]
    options.append(
        {"id": "opt_other", "text": msg("card_option_other", locale), "is_checked": False}
    )
    return {
        "card_type": "vote_interaction",
        "source": _source(icon_url, msg("card_source_ai", locale)),
        "main_title": {
            "title": _title(
                index,
                total,
                str(question.get("header") or ""),
                prefix=msg("card_q_prefix", locale),
                single=msg("card_q_single", locale),
            ),
            "desc": clip(str(question.get("question") or ""), 30),
        },
        "checkbox": {
            "question_key": "choice_answer",
            "option_list": options,
            "mode": 1 if question.get("multiSelect") else 0,
            "disable": False,
        },
        "submit_button": {"text": msg("card_submit_choice", locale), "key": "submit_choice"},
        "task_id": task_id,
    }


def question_brief(question: dict[str, Any], *, index: int, total: int, locale: str = "zh") -> str:
    """卡片之外再发一条 markdown：卡片选项被截到 11 个字，完整的选项说明只能靠这条。"""
    header_line = (
        msg("brief_q_prefix", locale, index=index + 1, total=total)
        if total > 1
        else msg("brief_q_single", locale)
    )
    header = str(question.get("header") or "")
    if header:
        header_line = f"{header_line} · {header}"
    lines = [header_line, str(question.get("question") or ""), ""]
    for o in question.get("options") or []:
        label, desc = str(o.get("label") or ""), str(o.get("description") or "")
        lines.append(f"- **{label}** — {desc}" if desc else f"- **{label}**")
    lines.append(msg("brief_option_other", locale))
    return "\n".join(lines)


def answered_card(
    question: dict[str, Any],
    *,
    index: int,
    total: int,
    task_id: str,
    answer: str,
    is_last: bool,
    icon_url: str,
    locale: str = "zh",
) -> dict[str, Any]:
    """答完之后把原卡片就地换成这张：按钮没了，用户不会对着同一题重复提交。"""
    sub = msg("card_answered_sub", locale, answer=answer)
    if is_last:
        # 与聊天里的提交确认共用同一句准确状态，避免卡片永久停在“生成中”。
        sub += "\n" + msg("choice_generating", locale)
    return {
        "card_type": "text_notice",
        "source": _source(icon_url, msg("card_source_ai", locale)),
        "main_title": {
            "title": _title(
                index,
                total,
                "",
                prefix=msg("card_answered_prefix", locale),
                single=msg("card_answered_single", locale),
            ),
            "desc": clip(str(question.get("question") or ""), 30),
        },
        "sub_title_text": sub,
        "card_action": {"type": 1, "url": WORK_URL},
        "task_id": task_id,
    }


def waiting_card(
    question: dict[str, Any], *, task_id: str, icon_url: str, locale: str = "zh"
) -> dict[str, Any]:
    """选了「其他」之后的占位卡：选项禁用，提示用户直接发消息。"""
    return {
        "card_type": "vote_interaction",
        "source": _source(icon_url, msg("card_source_ai", locale)),
        "main_title": {
            "title": msg("card_waiting_title", locale),
            "desc": clip(str(question.get("question") or ""), 30),
        },
        "checkbox": {
            "question_key": "choice_waiting",
            "option_list": [
                {
                    "id": "waiting_0",
                    "text": msg("card_waiting_option", locale),
                    "is_checked": True,
                }
            ],
            "mode": 0,
            "disable": True,
        },
        "submit_button": {"text": msg("card_waiting_submit", locale), "key": "submit_waiting"},
        "task_id": task_id,
    }


def expired_card(task_id: str, *, icon_url: str, locale: str = "zh") -> dict[str, Any]:
    """状态已经不在了（过期、被 reset 清掉）时回给用户的替换卡。"""
    return {
        "card_type": "text_notice",
        "source": _source(icon_url, msg("card_source_ai", locale)),
        "main_title": {
            "title": msg("card_expired_title", locale),
            "desc": msg("card_expired_desc", locale),
        },
        "card_action": {"type": 0},
        "task_id": task_id,
    }


def notice_card(
    task_id: str,
    *,
    title: str,
    desc: str,
    icon_url: str,
    source_desc: str | None = None,
    locale: str = "zh",
) -> dict[str, Any]:
    """纯告知卡（限流切换的结果回执等）：短标题放 `main_title.desc`，长文放 `sub_title_text`。"""
    return {
        "card_type": "text_notice",
        "source": _source(icon_url, source_desc or msg("card_source_dispatch", locale), color=True),
        "main_title": {"title": title, "desc": clip(desc, 30)},
        "sub_title_text": desc,
        "card_action": {"type": 1, "url": WORK_URL},
        "task_id": task_id,
    }


def switch_offer_card(
    *,
    task_id: str,
    current_name: str,
    target_name: str,
    pct_text: str,
    icon_url: str,
    locale: str = "zh",
) -> dict[str, Any]:
    """当前 relay 触发额度限制时，问用户切换还是等待。

    `main_title.desc` 与两个选项都不截断：实例名是决策依据，截了用户就不知道在切给谁；
    企微对这两个字段只是「建议」长度，原样下发即可。
    """
    return {
        "card_type": "vote_interaction",
        "source": _source(icon_url, msg("card_source_ai_dispatch", locale)),
        "main_title": {
            "title": msg("rl_offer_title", locale),
            "desc": msg(
                "rl_offer_desc", locale, current=current_name, target=target_name, pct=pct_text
            ),
        },
        "checkbox": {
            "question_key": "ratelimit_switch_choice",
            "option_list": [
                {
                    "id": "opt_switch",
                    "text": msg("rl_offer_opt_switch", locale, target=target_name),
                    "is_checked": False,
                },
                {
                    "id": "opt_wait",
                    "text": msg("rl_offer_opt_wait", locale, current=current_name),
                    "is_checked": False,
                },
            ],
            "mode": 0,
            "disable": False,
        },
        "submit_button": {"text": msg("rl_offer_submit", locale), "key": "ratelimit_switch_submit"},
        "task_id": task_id,
    }


def format_answers(questions: list[dict[str, Any]], answers: list[str], locale: str = "zh") -> str:
    """答完一轮后回给模型的文本：题目与回答一一对应，没答的显式写「(未回答)」。

    这两句是发给模型的，不是给用户看的：ja 表里与 zh 同文（理由同 media_prompt_*）。
    """
    unanswered = msg("answers_unanswered", locale)
    lines = [msg("answers_header", locale)]
    for i, q in enumerate(questions):
        answer = answers[i] if i < len(answers) and answers[i] else unanswered
        lines.append(f"{i + 1}. {q.get('question', '')} -> {answer}")
    return "\n".join(lines)
