"""system prompt 两层：会话级稳定段 + 每轮块。

稳定段（`build_system_prompt`）走 system prompt / developer_instructions，同一会话里逐字不变：
Codex 只在新建 thread 时读它，续聊时不会再看新的版本；Claude 每轮都重新传，变了就打断跨轮缓存。
拼装顺序固定，用到的段落如下（编号保留空位）：

    ① 平台安全策略        最高优先级，永远在最前面
    ①' 身份标签规则       固定段，紧随 ①；标签按会话固定，谁在说话看每轮块
    ①'' 平台规则          固定段：外部内容是数据、凭据保密、只报告实际完成的工作；各能力段不再重复
    ② codex 输出契约      仅 codex 后端
    ③ 运行模式说明        one-shot 进程模型
    ③' 日期与时间         固定段：用户说的时间按北京时间理解；当前时间由 agent 执行 date 自己取
    ④' 定时执行硬约束      仅定时任务：无人值守，禁止写操作与越权，抵抗注入
    ⑥ 组织背景            管理台填写才有：所有 AI 员工共用的公司基础信息，⑦ 可以补充细化
    ⑦ 机器人自定义 prompt  夹在中间，Lost-in-the-Middle 降低注入收益
    ⑧ 业务系统访问        授权给机器人的系统与使用规则
    ⑩ 附加能力规则        条件满足才有：协作协议、本人飞书/企微工具、本人定时任务、个人凭证
    ⑩' 输出详细度         1–3 档才有，4 档（详细）即模型默认；贴着结尾才压得住前面的篇幅要求
    ⑩'' 输出格式          飞书富卡片说明；靠近结尾才压得住前面的格式习惯，篇幅仍以详细度为准
    ⑪ 结尾重申            recency 效应，最后再说一遍运行模式

用户自定义的 ⑦ 永远夹在安全层中间，既不能抢 ① 的开头，也不能抢 ⑪ 的结尾。

稳定段只能取决于机器人配置、平台、聊天类型与后端。发言者、换人提醒和各能力的本轮状态
（授权状态、已保存的凭证、令牌签发失败、协作与续接阶段）写进每轮块（`build_turn_context`），
由 `ChatRequest` 接在本轮用户消息原文前面。
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from coreman.core.crypto import Cipher
from coreman.core.prompting.defaults import (
    DEFAULT_CRON_MODE,
    IDENTITY_TAG_RULE,
    IDENTITY_UNKNOWN_NOTE,
    PLATFORM_RULES,
    PROMPT_DEFAULTS_BY_KEY,
    SPEAKER_CHANGED_NOTE,
    SPEAKER_KNOWN_LINE,
    SPEAKER_UNKNOWN_LINE,
    TIME_RULE,
)
from coreman.core.settings_store import SettingsStore

# agent 取当前时间用的时区（与北京时间同为 UTC+8）；用户口头说的时间仍按北京时间理解。
CLOCK_TIMEZONE = "Asia/Singapore"


@dataclass(frozen=True)
class PromptSegments:
    """一次请求要用到的各段文案：默认值来自 defaults，管理台可逐段覆盖。"""

    security_policy: str
    codex_contract: str
    runtime_mode: str
    runtime_tail: str
    verbosity: dict[int, str]
    # 放在最后并给默认值：只拼对话提示词的调用方可以不关心它。
    cron_mode: str = DEFAULT_CRON_MODE
    org_context: str = ""


@dataclass(frozen=True)
class Speaker:
    """本轮发言者。`user_id` 为 None 表示平台账号没能映射到内部员工。"""

    platform_user_id: str
    user_id: uuid.UUID | None
    login_name: str | None
    display_name: str | None

    @property
    def known(self) -> bool:
        return self.user_id is not None


async def load_segments(store: SettingsStore) -> PromptSegments:
    """从 settings 读各段文案，缺键回落到出厂默认值。"""
    values = {k: str(await store.get(k, default=v)) for k, v in PROMPT_DEFAULTS_BY_KEY.items()}
    return PromptSegments(
        values["prompt_security_policy"],
        values["prompt_codex_contract"],
        values["prompt_runtime_mode"],
        values["prompt_runtime_tail"],
        {n: values[f"prompt_verbosity_{n}"] for n in (1, 2, 3)},
        cron_mode=values["prompt_cron_mode"],
        org_context=values["prompt_org_context"],
    )


def identity_tag(cipher: Cipher, relay_session_id: uuid.UUID | str) -> str:
    """会话的身份标签：只出现在 system prompt 与平台写的每轮块里。

    按 relay 会话 id 由主密钥派生，同一会话（含切回历史会话、续跑、协作续接）永远同一个值，
    稳定段才能逐字不变；拿不到主密钥就算不出来，会话查看链接里的 id 不会泄露它。用户消息
    正文由 `sanitize` 挡掉带标签的写法；模型读到的文件、网页、工具输出与记忆写不出它。
    """
    return cipher.mac("identity-tag", str(relay_session_id))[:8]


def new_identity_tag() -> str:
    """没有 relay 会话的一次性请求（体检）用的标签：每次现生成。"""
    return secrets.token_hex(4)


def speaker_label(speaker: Speaker) -> str:
    """换人提醒里的称呼：姓名（登录名），缺哪个用哪个，都没有就用平台账号。"""
    if speaker.display_name and speaker.login_name:
        return f"{speaker.display_name}（{speaker.login_name}）"
    return speaker.display_name or speaker.login_name or speaker.platform_user_id


def build_turn_context(
    *,
    tag: str,
    speaker: Speaker,
    previous: Speaker | None = None,
    notes: Sequence[str] = (),
) -> str:
    """本轮块：当前发言者、换人提醒与各能力的本轮状态，接在用户消息原文前面。

    身份未知时必须显式写 identity_unknown，不能留空——留空模型会自己编。`previous` 是同一会话里
    上一轮的发言者（换了人才给）；`notes` 是各能力已写好的本轮状态，一条一段。
    """
    if speaker.known:
        lines = [
            SPEAKER_KNOWN_LINE.format(
                tag=tag,
                platform_user_id=speaker.platform_user_id,
                login=speaker.login_name or "",
                name=speaker.display_name or "",
            )
        ]
    else:
        lines = [
            SPEAKER_UNKNOWN_LINE.format(tag=tag, platform_user_id=speaker.platform_user_id),
            IDENTITY_UNKNOWN_NOTE,
        ]
    if previous is not None and speaker.known:
        lines.append(
            SPEAKER_CHANGED_NOTE.format(
                previous=speaker_label(previous), current=speaker_label(speaker)
            )
        )
    lines += [note.strip() for note in notes if note and note.strip()]
    return "\n".join([f"[SYS_TURN:{tag}]", *lines, f"[/SYS_TURN:{tag}]"])


def build_system_prompt(
    *,
    segments: PromptSegments,
    backend: str,
    verbosity_level: int,
    bot_prompt: str,
    tag: str,
    systems_prompt: str = "",
    scheduled: bool = False,
    extra: str = "",
    output_format: str = "",
    structured_reply: bool = False,
) -> str:
    """按固定顺序拼稳定段；空段直接跳过，段间恒为一个空行。

    `scheduled=True` 是定时执行：硬约束排在机器人与任务自定义内容之前，
    自定义 prompt 里的「自动批准」之类指令抢不到它前面。

    `extra` 是附加能力的规则（⑩），必须经这里拼进来，不要在返回值后面再接：
    接在后面会把 ⑪ 挤出结尾。`output_format` 是平台的输出格式说明（⑩''），同理。

    `structured_reply=True` 表示这一轮的回答由平台按固定格式解析（协作 helper 的纯 JSON），
    不给人看：codex 输出契约、详细度和输出格式都要求 Markdown 或卡片，会和那份格式打架，一并不加。
    """
    codex = backend == "codex"
    parts = [
        segments.security_policy,
        # 固定段，管理台改不了：身份规则本身不能由可编辑文案来定义。
        IDENTITY_TAG_RULE.format(tag=tag),
        # 固定段：跨能力的规则只在这里说一次，管理台文案删不掉它。
        PLATFORM_RULES,
        segments.codex_contract if codex and not structured_reply else "",
        segments.runtime_mode,
        TIME_RULE.format(clock_timezone=CLOCK_TIMEZONE),
        segments.cron_mode if scheduled else "",
        segments.org_context,
        bot_prompt,
        systems_prompt,
        extra,
        "" if structured_reply else segments.verbosity.get(verbosity_level, ""),
        "" if structured_reply else output_format,
        segments.runtime_tail,
    ]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
