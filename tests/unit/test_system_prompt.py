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
    assert set(DEFAULT_VERBOSITY) == {2, 3, 4} and all(DEFAULT_VERBOSITY.values())
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
    assert DEFAULT_CODEX_CONTRACT not in out
    assert DEFAULT_VERBOSITY[3] not in out and SPEAKER_CHANGED_LINE not in out


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


class _FakeStore:
    """只实现 load_segments 用到的 get()：为了读七个键去连库不值当。"""

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
