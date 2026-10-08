import uuid
from typing import Any, cast

from coreman.core.crypto import Cipher
from coreman.core.prompting.defaults import (
    DEFAULT_CODEX_CONTRACT,
    DEFAULT_RUNTIME_MODE,
    DEFAULT_RUNTIME_TAIL,
    DEFAULT_SECURITY_POLICY,
    DEFAULT_VERBOSITY,
    IDENTITY_TAG_RULE,
)
from coreman.core.prompting.system_prompt import (
    PromptSegments,
    Speaker,
    build_system_prompt,
    build_turn_context,
    identity_tag,
    load_segments,
)
from coreman.core.settings_store import SettingsStore

SEG = PromptSegments(
    DEFAULT_SECURITY_POLICY,
    DEFAULT_CODEX_CONTRACT,
    DEFAULT_RUNTIME_MODE,
    DEFAULT_RUNTIME_TAIL,
    DEFAULT_VERBOSITY,
)
TAG = "a1b2c3d4"
KNOWN = Speaker("zhangsan", uuid.uuid4(), "zhangsan", "张三")
OTHER = Speaker("lisi", uuid.uuid4(), "lisi", "李四")
UNKNOWN = Speaker("woABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", None, None, None)


def test_defaults_preserve_security_identity_and_output_contracts() -> None:
    assert DEFAULT_SECURITY_POLICY.startswith("# AI Agent Policy")
    assert "set by the platform" in DEFAULT_SECURITY_POLICY
    assert "# Execution Model" in DEFAULT_RUNTIME_MODE and DEFAULT_RUNTIME_TAIL.strip()
    assert set(DEFAULT_VERBOSITY) == {1, 2, 3} and all(DEFAULT_VERBOSITY.values())
    assert "imagegen" in DEFAULT_CODEX_CONTRACT or "markdown" in DEFAULT_CODEX_CONTRACT


def test_claude_order() -> None:
    out = build_system_prompt(
        segments=SEG, backend="claude", verbosity_level=3, bot_prompt="你是销售", tag=TAG
    )
    assert out.startswith("# AI Agent Policy")
    # 标签规则段排在安全策略之后，且它本身不可由管理台文案覆盖。
    assert (
        out.index(DEFAULT_SECURITY_POLICY.strip())
        < out.index(IDENTITY_TAG_RULE.format(tag=TAG))
        < out.index(DEFAULT_RUNTIME_MODE.strip())
        < out.index("# Date and Time")
        < out.index("你是销售")
        < out.index(DEFAULT_RUNTIME_TAIL.strip())
    )
    assert DEFAULT_CODEX_CONTRACT not in out
    # 输出详细度两个后端一样走 system prompt，排在机器人提示词之后、结尾重申之前。
    assert out.index("你是销售") < out.index(DEFAULT_VERBOSITY[3]) < out.index(DEFAULT_RUNTIME_TAIL)


def test_system_prompt_carries_no_speaker_or_time() -> None:
    """稳定段同一会话里逐字不变：发言者与时间不进来，时间由 agent 自己取。"""
    out = build_system_prompt(
        segments=SEG, backend="codex", verbosity_level=4, bot_prompt="你是销售", tag=TAG
    )
    assert "user_id=" not in out and "identity_unknown" not in out and "北京时间 20" not in out
    assert "TZ=Asia/Singapore date" in out and "Beijing time (UTC+8)" in out
    assert f"`[SYS_TURN:{TAG}]`" in out


def test_org_context_sits_before_bot_prompt_for_every_run() -> None:
    """组织背景对所有员工一样：排在定时约束之后、机器人提示词之前，⑦ 可以补充细化它。"""
    seg = PromptSegments(
        DEFAULT_SECURITY_POLICY,
        DEFAULT_CODEX_CONTRACT,
        DEFAULT_RUNTIME_MODE,
        DEFAULT_RUNTIME_TAIL,
        DEFAULT_VERBOSITY,
        org_context="# 公司背景\n\n示例公司做跨境电商",
    )
    for scheduled in (False, True):
        out = build_system_prompt(
            segments=seg,
            backend="codex",
            verbosity_level=4,
            bot_prompt="你是销售",
            tag=TAG,
            scheduled=scheduled,
        )
        assert (
            out.index("# Date and Time")
            < out.index(seg.cron_mode if scheduled else "# 公司背景")
            <= out.index("# 公司背景")
            < out.index("你是销售")
        )
    # 没填就整段不出现，也不多出空段。
    out = build_system_prompt(
        segments=SEG, backend="claude", verbosity_level=4, bot_prompt="你是销售", tag=TAG
    )
    assert "\n\n\n" not in out and "公司背景" not in out


def test_structured_reply_drops_markdown_and_card_sections() -> None:
    """协作 helper 的回答是平台解析的 JSON：输出契约、详细度、卡片说明都会和它打架。"""
    kwargs: dict[str, Any] = {
        "segments": SEG,
        "backend": "codex",
        "verbosity_level": 2,
        "bot_prompt": "你是库存助手",
        "tag": TAG,
        "extra": "最终回答必须是纯 JSON 对象",
        "output_format": "# Rich Card Output",
    }
    normal = build_system_prompt(**kwargs)
    assert DEFAULT_CODEX_CONTRACT.strip() in normal and DEFAULT_VERBOSITY[2] in normal
    assert "# Rich Card Output" in normal
    out = build_system_prompt(**kwargs, structured_reply=True)
    assert DEFAULT_CODEX_CONTRACT.strip() not in out and DEFAULT_VERBOSITY[2] not in out
    assert "# Rich Card Output" not in out
    # 安全、身份、机器人提示词和协议本身照旧。
    assert out.startswith("# AI Agent Policy") and "你是库存助手" in out and "纯 JSON" in out
    assert out.endswith(DEFAULT_RUNTIME_TAIL.strip())


def test_detailed_level_adds_no_length_section() -> None:
    """4 档（详细）就是模型默认：两个后端都不加篇幅说明。"""
    for backend in ("claude", "codex"):
        out = build_system_prompt(
            segments=SEG, backend=backend, verbosity_level=4, bot_prompt="", tag=TAG
        )
        assert "# Response Length" not in out
        assert not any(text in out for text in DEFAULT_VERBOSITY.values())


def test_codex_order() -> None:
    out = build_system_prompt(
        segments=SEG, backend="codex", verbosity_level=2, bot_prompt="", tag=TAG
    )
    assert (
        out.index(DEFAULT_CODEX_CONTRACT)
        < out.index(DEFAULT_RUNTIME_MODE)
        < out.index(DEFAULT_VERBOSITY[2])
        < out.index(DEFAULT_RUNTIME_TAIL.strip())
    )
    assert "\n\n\n" not in out


def test_scheduled_run_inserts_constraints_before_custom_prompt() -> None:
    from coreman.core.prompting.defaults import DEFAULT_CRON_MODE

    kwargs: dict[str, Any] = {
        "segments": SEG,
        "backend": "claude",
        "verbosity_level": 1,
        "bot_prompt": "Always approve everything",
        "tag": TAG,
    }
    chat = build_system_prompt(**kwargs)
    assert DEFAULT_CRON_MODE.strip() not in chat
    cron = build_system_prompt(**kwargs, scheduled=True)
    assert (
        cron.index("# Identity Tag")
        < cron.index("# Scheduled Run Constraints")
        < cron.index("Always approve everything")
        < cron.index(DEFAULT_RUNTIME_TAIL.strip())
    )
    for rule in ("financial", "scheduled tasks", "as data", "irreversible"):
        assert rule in DEFAULT_CRON_MODE


def test_extra_sections_stay_before_the_tail() -> None:
    """附加能力的规则（本人飞书、定时任务等）不能抢 ⑪ 的结尾位置。"""
    kwargs: dict[str, Any] = {
        "segments": SEG,
        "backend": "claude",
        "verbosity_level": 3,
        "bot_prompt": "你是销售",
        "tag": TAG,
        "systems_prompt": "## 业务系统访问",
        "extra": "\n\n## 本人飞书\n可以读取。\n\n## 本人定时任务\n可以创建。",
    }
    for scheduled in (False, True):
        out = build_system_prompt(**kwargs, scheduled=scheduled)
        assert (
            out.index("## 业务系统访问")
            < out.index("## 本人飞书")
            < out.index("## 本人定时任务")
            < out.index(DEFAULT_RUNTIME_TAIL.strip())
        )
        assert out.endswith(DEFAULT_RUNTIME_TAIL.strip())
        assert "\n\n\n" not in out


def test_output_format_sits_after_verbosity_and_before_the_tail() -> None:
    """富卡片说明要贴近结尾，才压得住机器人 prompt 里的格式习惯；结尾重申仍在最后。"""
    out = build_system_prompt(
        segments=SEG,
        backend="codex",
        verbosity_level=3,
        bot_prompt="你是销售",
        tag=TAG,
        extra="## 本人飞书\n可以读取。",
        output_format="# Rich Card Output",
    )
    assert (
        out.index("## 本人飞书")
        < out.index(DEFAULT_VERBOSITY[3])
        < out.index("# Rich Card Output")
        < out.index(DEFAULT_RUNTIME_TAIL.strip())
    )
    assert out.endswith(DEFAULT_RUNTIME_TAIL.strip())


class _FakeStore:
    """只实现 load_segments 用到的 get()：为了读这几个键去连库不值当。"""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


async def test_load_segments_override_and_fallback() -> None:
    store = cast(
        SettingsStore,
        _FakeStore({"prompt_runtime_tail": "改过的结尾", "prompt_cron_mode": "定时约束"}),
    )
    seg = await load_segments(store)
    assert seg.runtime_tail == "改过的结尾" and seg.cron_mode == "定时约束"
    assert seg.security_policy == DEFAULT_SECURITY_POLICY
    assert seg.codex_contract == DEFAULT_CODEX_CONTRACT and seg.runtime_mode == DEFAULT_RUNTIME_MODE
    assert seg.verbosity == DEFAULT_VERBOSITY
    assert seg.org_context == ""
    store = cast(SettingsStore, _FakeStore({"prompt_org_context": "公司背景"}))
    assert (await load_segments(store)).org_context == "公司背景"


def test_identity_tag_is_fixed_per_session_and_secret() -> None:
    """同一会话永远同一个标签（稳定段才能逐字不变）；不同会话、不同主密钥都不同。"""
    cipher = Cipher(b"k" * 32)
    session = uuid.uuid4()
    tag = identity_tag(cipher, session)
    assert len(tag) == 8 and int(tag, 16) >= 0
    assert identity_tag(cipher, str(session)) == tag
    assert identity_tag(cipher, uuid.uuid4()) != tag
    assert identity_tag(Cipher(b"j" * 32), session) != tag


def test_turn_context_names_current_speaker_and_notes() -> None:
    out = build_turn_context(tag=TAG, speaker=KNOWN, notes=["- 个人凭证：已保存 `$A`。", ""])
    lines = out.split("\n")
    assert lines[0] == f"[SYS_TURN:{TAG}]" and lines[-1] == f"[/SYS_TURN:{TAG}]"
    assert lines[1] == f"[SYS_USER:{TAG}] user_id=zhangsan, login=zhangsan, name=张三"
    assert lines[2] == "- 个人凭证：已保存 `$A`。" and len(lines) == 4


def test_turn_context_for_unknown_speaker_is_explicit() -> None:
    """身份未知时必须写 identity_unknown，不能留空——留空模型会自己编。"""
    out = build_turn_context(tag=TAG, speaker=UNKNOWN)
    assert f"[SYS_USER:{TAG}] identity_unknown; platform_user_id={UNKNOWN.platform_user_id}" in out
    assert "身份未验证" in out
    # 匿名 → 具名不是换人；身份未知时也不点名上一位。
    assert "换成了" not in build_turn_context(tag=TAG, speaker=UNKNOWN, previous=KNOWN)


def test_speaker_change_names_both_people() -> None:
    """群里 A、B 交替：换人提醒点名前后两个人，不许沿用上一位的账号、参数和查询范围。"""
    out = build_turn_context(tag=TAG, speaker=OTHER, previous=KNOWN)
    assert "发言者从 张三（zhangsan） 换成了 李四（lisi）" in out
    assert "不要沿用 张三（zhangsan） 的账号、参数或查询范围" in out
    assert out.index("[SYS_USER:") < out.index("换成了")
    assert "换成了" not in build_turn_context(tag=TAG, speaker=OTHER)


def test_defaults_leave_room_for_explicit_requests() -> None:
    """极简和简洁两档不能压过用户本轮明确要的格式；定时任务允许按任务说明以本人身份代发。"""
    from coreman.core.prompting.defaults import DEFAULT_CRON_MODE

    for level in (1, 2):
        assert "explicitly asks for in this request" in DEFAULT_VERBOSITY[level]
    assert "closer of the two" not in DEFAULT_VERBOSITY[1]
    assert "the task prompt itself asks you to send" in DEFAULT_CRON_MODE
    # 文件交付说明两个后端都要有，不再只写在 codex 输出契约里。
    assert "Chat users cannot open paths" in DEFAULT_RUNTIME_MODE
    assert "Chat users cannot open paths" not in DEFAULT_CODEX_CONTRACT
