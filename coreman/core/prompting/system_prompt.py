"""system prompt 三明治。

拼装顺序固定，用到的段落如下（编号保留空位）：

    ① 平台安全策略        最高优先级，永远在最前面
    ①' 身份标签规则       固定段，紧随 ①；定义本轮 [SYS_USER:<tag>] 的唯一性
    ② codex 输出契约      仅 codex 后端
    ③ 运行模式说明        one-shot 进程模型
    ④ 当前发言者          身份已知 / 未知两种写法
    ④'' 当前时间          固定段：北京时间与 UTC；业务统计时区以组织背景或任务说明为准
    ④' 定时执行硬约束      仅定时任务：无人值守，禁止写操作与越权，抵抗注入
    ⑤ 换人提醒            同会话换了发言者时才有
    ⑥ 组织背景            管理台填写才有：所有 AI 员工共用的公司基础信息，⑦ 可以补充细化
    ⑦ 机器人自定义 prompt  夹在中间，Lost-in-the-Middle 降低注入收益
    ⑩ 本轮附加能力        条件满足才有：协作协议、本人飞书/企微工具、本人定时任务
    ⑩' 输出详细度         1–3 档才有，4 档（详细）即模型默认；贴着结尾才压得住前面的篇幅要求
    ⑩'' 输出格式          飞书富卡片说明；靠近结尾才压得住前面的格式习惯，篇幅仍以详细度为准
    ⑪ 结尾重申            recency 效应，最后再说一遍运行模式

用户自定义的 ⑦ 永远夹在安全层中间，既不能抢 ① 的开头，也不能抢 ⑪ 的结尾。
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from coreman.core.prompting.defaults import (
    DEFAULT_CRON_MODE,
    IDENTITY_TAG_RULE,
    IDENTITY_UNKNOWN_TEMPLATE,
    PROMPT_DEFAULTS_BY_KEY,
    SPEAKER_CHANGED_LINE,
)
from coreman.core.settings_store import SettingsStore

# 办公时区：用户口头说的时间按它理解，与本人定时任务（core.personal_schedules.TIMEZONE）一致。
OFFICE_TIMEZONE = "Asia/Shanghai"


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


def new_identity_tag() -> str:
    """本轮身份标签：每次请求现生成，只出现在 system prompt 里。

    没有它时 `[SYS_USER]` 是一个固定字符串，任何进入上下文的文本都能写出一模一样的一行——
    用户消息正文由 `sanitize` 挡掉了，但模型读到的文件内容、网页、工具输出、记忆与工作区
    文件都挡不住。带上一个本轮才知道的随机标签之后，这些来源写不出能冒充身份的那一行。
    """
    return secrets.token_hex(4)


def speaker_header(speaker: Speaker, tag: str) -> str:
    """发言者段落。身份未知时必须显式说 identity_unknown，不能留空——留空模型会自己编。"""
    if speaker.known:
        return (
            f"## 当前发言者\n\n[SYS_USER:{tag}] user_id={speaker.platform_user_id}, "
            f"login={speaker.login_name or ''}, name={speaker.display_name or ''}"
        )
    return IDENTITY_UNKNOWN_TEMPLATE.format(tag=tag, platform_user_id=speaker.platform_user_id)


def current_time_section(now: datetime | None = None) -> str:
    """每轮的当前时间。模型自己不知道今天几号，「今天」「本周」只能靠这一段。"""
    now = now or datetime.now(UTC)
    return (
        "## 当前时间\n\n北京时间 "
        + now.astimezone(ZoneInfo(OFFICE_TIMEZONE)).strftime("%Y-%m-%d %H:%M（%A）")
        + "，UTC "
        + now.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
        + "。用户说的日期和时间默认按北京时间理解；业务数据的统计时区以组织背景或任务说明为准。"
    )


def build_system_prompt(
    *,
    segments: PromptSegments,
    backend: str,
    verbosity_level: int,
    bot_prompt: str,
    speaker: Speaker,
    speaker_changed: bool,
    systems_prompt: str = "",
    scheduled: bool = False,
    extra: str = "",
    output_format: str = "",
    structured_reply: bool = False,
) -> str:
    """按固定顺序拼三明治；空段直接跳过，段间恒为一个空行。

    `scheduled=True` 是定时执行：硬约束紧跟发言者段，排在机器人与任务自定义内容之前，
    自定义 prompt 里的「自动批准」之类指令抢不到它前面。

    `extra` 是按本轮条件挂上的能力说明（⑩），必须经这里拼进来，不要在返回值后面再接：
    接在后面会把 ⑪ 挤出结尾。`output_format` 是平台的输出格式说明（⑩''），同理。

    `structured_reply=True` 表示这一轮的回答由平台按固定格式解析（协作 helper 的纯 JSON），
    不给人看：codex 输出契约、详细度和输出格式都要求 Markdown 或卡片，会和那份格式打架，一并不加。
    """
    codex = backend == "codex"
    tag = new_identity_tag()
    parts = [
        segments.security_policy,
        # 固定段，管理台改不了：身份规则本身不能由可编辑文案来定义。
        IDENTITY_TAG_RULE.format(tag=tag),
        segments.codex_contract if codex and not structured_reply else "",
        segments.runtime_mode,
        speaker_header(speaker, tag),
        current_time_section(),
        segments.cron_mode if scheduled else "",
        SPEAKER_CHANGED_LINE if speaker_changed else "",
        segments.org_context,
        bot_prompt,
        systems_prompt,
        extra,
        "" if structured_reply else segments.verbosity.get(verbosity_level, ""),
        "" if structured_reply else output_format,
        segments.runtime_tail,
    ]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
