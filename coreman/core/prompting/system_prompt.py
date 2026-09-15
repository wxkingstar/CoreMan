"""system prompt 三明治（spec §8.3）。

拼装顺序固定，M2 用到其中 ①②③④⑤⑥⑦⑪ 八段：

    ① 平台安全策略        最高优先级，永远在最前面
    ② codex 输出契约      仅 codex 后端
    ③ 运行模式说明        one-shot 进程模型
    ④ 当前发言者          身份已知 / 未知两种写法
    ④' 定时执行硬约束      仅定时任务：无人值守，禁止写操作与越权，抵抗注入
    ⑤ 换人提醒            同会话换了发言者时才有
    ⑥ verbosity 风格      仅 codex（claude 走 --settings 的 output style）
    ⑦ 机器人自定义 prompt  夹在中间，Lost-in-the-Middle 降低注入收益
    ⑪ 结尾重申            recency 效应，最后再说一遍运行模式

用户自定义的 ⑦ 永远夹在安全层中间，既不能抢 ① 的开头，也不能抢 ⑪ 的结尾。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from coreman.core.prompting.defaults import (
    DEFAULT_CRON_MODE,
    IDENTITY_UNKNOWN_TEMPLATE,
    PROMPT_DEFAULTS_BY_KEY,
    SPEAKER_CHANGED_LINE,
)
from coreman.core.settings_store import SettingsStore


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
        {n: values[f"prompt_verbosity_{n}"] for n in (2, 3, 4)},
        cron_mode=values["prompt_cron_mode"],
    )


def speaker_header(speaker: Speaker) -> str:
    """发言者段落。身份未知时必须显式说 identity_unknown，不能留空——留空模型会自己编。"""
    if speaker.known:
        return (
            f"## 当前发言者\n\n[SYS_USER] user_id={speaker.platform_user_id}, "
            f"login={speaker.login_name or ''}, name={speaker.display_name or ''}"
        )
    return IDENTITY_UNKNOWN_TEMPLATE.format(platform_user_id=speaker.platform_user_id)


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
) -> str:
    """按固定顺序拼三明治；空段直接跳过，段间恒为一个空行。

    `scheduled=True` 是定时执行：硬约束紧跟发言者段，排在机器人与任务自定义内容之前，
    自定义 prompt 里的「自动批准」之类指令抢不到它前面。
    """
    codex = backend == "codex"
    parts = [
        segments.security_policy,
        segments.codex_contract if codex else "",
        segments.runtime_mode,
        speaker_header(speaker),
        segments.cron_mode if scheduled else "",
        SPEAKER_CHANGED_LINE if speaker_changed else "",
        # claude 后端的 verbosity 走 CLI 的 output style，不占 system prompt 的额度。
        segments.verbosity.get(verbosity_level, "") if codex else "",
        bot_prompt,
        systems_prompt,
        segments.runtime_tail,
    ]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
