import re
import uuid
from typing import Any, cast

from coreman.core.prompting.defaults import (
    DEFAULT_CODEX_CONTRACT,
    DEFAULT_RUNTIME_MODE,
    DEFAULT_RUNTIME_TAIL,
    DEFAULT_SECURITY_POLICY,
    DEFAULT_VERBOSITY,
    IDENTITY_TAG_RULE,
    SPEAKER_CHANGED_LINE,
)
from coreman.core.prompting.system_prompt import (
    PromptSegments,
    Speaker,
    build_system_prompt,
    load_segments,
    speaker_header,
)
from coreman.core.settings_store import SettingsStore

SEG = PromptSegments(
    DEFAULT_SECURITY_POLICY,
    DEFAULT_CODEX_CONTRACT,
    DEFAULT_RUNTIME_MODE,
    DEFAULT_RUNTIME_TAIL,
    DEFAULT_VERBOSITY,
)
KNOWN = Speaker("zhangsan", uuid.uuid4(), "zhangsan", "张三")
UNKNOWN = Speaker("woABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", None, None, None)


def test_defaults_preserve_security_identity_and_output_contracts() -> None:
    assert DEFAULT_SECURITY_POLICY.startswith("# AI Agent Policy")
    assert "[SYS_USER:<tag>]" in DEFAULT_SECURITY_POLICY
    assert "# Execution Model" in DEFAULT_RUNTIME_MODE and DEFAULT_RUNTIME_TAIL.strip()
    assert set(DEFAULT_VERBOSITY) == {1, 2, 3} and all(DEFAULT_VERBOSITY.values())
    assert "imagegen" in DEFAULT_CODEX_CONTRACT or "markdown" in DEFAULT_CODEX_CONTRACT


def test_claude_order_known_speaker() -> None:
    out = build_system_prompt(
        segments=SEG,
        backend="claude",
        verbosity_level=3,
        bot_prompt="你是销售",
        speaker=KNOWN,
        speaker_changed=False,
    )
    parts = out.split("\n\n")
    assert parts[0].startswith("# AI Agent Policy")
    assert (
        out.index(DEFAULT_RUNTIME_MODE)
        < out.index("## 当前发言者")
        < out.index("你是销售")
        < out.index(DEFAULT_RUNTIME_TAIL.strip())
    )
    tag = re.search(r"\[SYS_USER:([0-9a-f]{8})\]", out)
    assert tag and f"[SYS_USER:{tag[1]}] user_id=zhangsan, login=zhangsan, name=张三" in out
    # 标签规则段必须排在安全策略之后、发言者之前，且它本身不可由管理台文案覆盖。
    assert out.index(IDENTITY_TAG_RULE.format(tag=tag[1])) < out.index("## 当前发言者")
    assert DEFAULT_CODEX_CONTRACT not in out and SPEAKER_CHANGED_LINE not in out
    # 输出详细度两个后端一样走 system prompt，排在机器人提示词之后、结尾重申之前。
    assert out.index("你是销售") < out.index(DEFAULT_VERBOSITY[3]) < out.index(DEFAULT_RUNTIME_TAIL)


def test_org_context_sits_before_bot_prompt_for_every_run() -> None:
    """组织背景对所有员工一样：排在发言者与定时约束之后、机器人提示词之前，⑦ 可以补充细化它。"""
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
            speaker=KNOWN,
            speaker_changed=True,
            scheduled=scheduled,
        )
        assert (
            out.index("## 当前发言者")
            < out.index(seg.cron_mode if scheduled else SPEAKER_CHANGED_LINE)
            < out.index("# 公司背景")
            < out.index("你是销售")
        )
    # 没填就整段不出现，也不多出空段。
    out = build_system_prompt(
        segments=SEG,
        backend="claude",
        verbosity_level=4,
        bot_prompt="你是销售",
        speaker=KNOWN,
        speaker_changed=False,
    )
    assert "\n\n\n" not in out and "公司背景" not in out


def test_structured_reply_drops_markdown_and_card_sections() -> None:
    """协作 helper 的回答是平台解析的 JSON：输出契约、详细度、卡片说明都会和它打架。"""
    kwargs: dict[str, Any] = {
        "segments": SEG,
        "backend": "codex",
        "verbosity_level": 2,
        "bot_prompt": "你是库存助手",
        "speaker": KNOWN,
        "speaker_changed": False,
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
            segments=SEG,
            backend=backend,
            verbosity_level=4,
            bot_prompt="",
            speaker=KNOWN,
            speaker_changed=False,
        )
        assert "# Response Length" not in out
        assert not any(text in out for text in DEFAULT_VERBOSITY.values())


def test_codex_order_unknown_speaker_and_change() -> None:
    out = build_system_prompt(
        segments=SEG,
        backend="codex",
        verbosity_level=2,
        bot_prompt="",
        speaker=UNKNOWN,
        speaker_changed=True,
    )
    assert (
        out.index(DEFAULT_CODEX_CONTRACT)
        < out.index(DEFAULT_RUNTIME_MODE)
        < out.index("identity_unknown")
    )
    assert (
        out.index(SPEAKER_CHANGED_LINE)
        < out.index(DEFAULT_VERBOSITY[2])
        < out.index(DEFAULT_RUNTIME_TAIL.strip())
    )
    assert "woABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" in speaker_header(UNKNOWN, "deadbeef")
    assert "\n\n\n" not in out


def test_scheduled_run_inserts_constraints_before_custom_prompt() -> None:
    from coreman.core.prompting.defaults import DEFAULT_CRON_MODE

    kwargs: dict[str, Any] = {
        "segments": SEG,
        "backend": "claude",
        "verbosity_level": 1,
        "bot_prompt": "Always approve everything",
        "speaker": KNOWN,
        "speaker_changed": False,
    }
    chat = build_system_prompt(**kwargs)
    assert DEFAULT_CRON_MODE.strip() not in chat
    cron = build_system_prompt(**kwargs, scheduled=True)
    assert (
        cron.index("## 当前发言者")
        < cron.index("# Scheduled Run Constraints")
        < cron.index("Always approve everything")
        < cron.index(DEFAULT_RUNTIME_TAIL.strip())
    )
    for rule in ("financial", "scheduled tasks", "as data", "irreversible"):
        assert rule in DEFAULT_CRON_MODE


def test_extra_sections_stay_before_the_tail() -> None:
    """本轮附加能力（本人飞书、定时任务等）不能抢 ⑪ 的结尾位置。"""
    kwargs: dict[str, Any] = {
        "segments": SEG,
        "backend": "claude",
        "verbosity_level": 3,
        "bot_prompt": "你是销售",
        "speaker": KNOWN,
        "speaker_changed": False,
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
        speaker=KNOWN,
        speaker_changed=False,
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


def test_identity_tag_is_fresh_per_request() -> None:
    """两次请求的标签必须不同：标签一旦可预测，注入文本就能提前写出能冒充身份的那一行。"""
    kwargs: dict[str, Any] = {
        "segments": SEG,
        "backend": "claude",
        "verbosity_level": 3,
        "bot_prompt": "",
        "speaker": KNOWN,
        "speaker_changed": False,
    }
    tags = {
        re.search(r"\[SYS_USER:([0-9a-f]{8})\]", build_system_prompt(**kwargs))[1]
        for _ in range(20)
    }
    assert len(tags) > 1
