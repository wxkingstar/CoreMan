import uuid
from typing import Any, cast

from coreman.core.prompting.defaults import (
    DEFAULT_CODEX_CONTRACT,
    DEFAULT_RUNTIME_MODE,
    DEFAULT_RUNTIME_TAIL,
    DEFAULT_SECURITY_POLICY,
    DEFAULT_VERBOSITY,
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
    assert "[SYS_USER]" in DEFAULT_SECURITY_POLICY
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
    assert "[SYS_USER] user_id=zhangsan, login=zhangsan, name=张三" in out
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
    assert "woABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" in speaker_header(UNKNOWN)
    assert "\n\n\n" not in out


class _FakeStore:
    """只实现 load_segments 用到的 get()：为了读七个键去连库不值当。"""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


async def test_load_segments_override_and_fallback() -> None:
    store = cast(SettingsStore, _FakeStore({"prompt_runtime_tail": "改过的结尾"}))
    seg = await load_segments(store)
    assert seg.runtime_tail == "改过的结尾"
    assert seg.security_policy == DEFAULT_SECURITY_POLICY
    assert seg.codex_contract == DEFAULT_CODEX_CONTRACT and seg.runtime_mode == DEFAULT_RUNTIME_MODE
    assert seg.verbosity == DEFAULT_VERBOSITY
