import pytest

from coreman.core.db.models import ModelCatalog, RelayServer
from coreman.core.relay.models import (
    backend_of,
    default_model,
    effective_models,
    fit_effort,
    known_effort_flags,
    supports_effort,
)

CATALOG = [
    ModelCatalog(
        provider="claude",
        model="vllm/claude-sonnet-4-6",
        is_default=True,
        retired=False,
        sort_order=100,
    ),
    ModelCatalog(
        provider="claude",
        model="vllm/claude-opus-4-6",
        is_default=False,
        retired=False,
        sort_order=90,
        supports_xhigh=True,
    ),
    ModelCatalog(
        provider="claude", model="vllm/claude-old", is_default=False, retired=True, sort_order=10
    ),
    ModelCatalog(
        provider="codex", model="codex/gpt-5.5", is_default=True, retired=False, sort_order=100
    ),
]


def test_backend_of() -> None:
    assert (
        backend_of("codex/gpt-5.5") == "codex" and backend_of("vllm/claude-sonnet-4-6") == "claude"
    )


def test_effective_inherit_excludes_retired_and_other_provider() -> None:
    relay = RelayServer(
        name="r",
        host="h",
        clawrelay_port=1,
        model_provider="claude",
        supported_models_mode="inherit",
    )
    assert effective_models(relay, CATALOG) == ["vllm/claude-sonnet-4-6", "vllm/claude-opus-4-6"]


def test_effective_restricted_intersects_catalog_including_retired() -> None:
    relay = RelayServer(
        name="r",
        host="h",
        clawrelay_port=1,
        model_provider="claude",
        supported_models_mode="restricted",
        supported_models=[
            "vllm/claude-old",
            "vllm/claude-opus-4-6",
            "not-in-catalog",
            "codex/gpt-5.5",
        ],
    )
    assert effective_models(relay, CATALOG) == ["vllm/claude-old", "vllm/claude-opus-4-6"]


def test_default_model_and_xhigh() -> None:
    assert default_model("claude", CATALOG) == "vllm/claude-sonnet-4-6"
    assert default_model("minimax", CATALOG) is None
    only_retired_default = [
        ModelCatalog(provider="p", model="a", is_default=True, retired=True, sort_order=1),
        ModelCatalog(provider="p", model="b", is_default=False, retired=False, sort_order=5),
    ]
    assert default_model("p", only_retired_default) == "b"
    assert supports_effort("vllm/claude-opus-4-6", "xhigh", CATALOG)
    assert not supports_effort("vllm/claude-sonnet-4-6", "xhigh", CATALOG)
    assert supports_effort("vllm/claude-sonnet-4-6", "high", CATALOG)
    assert not supports_effort("vllm/claude-opus-4-6", "max", CATALOG)


def test_fit_effort_steps_down_to_highest_supported() -> None:
    catalog = [
        ModelCatalog(provider="c", model="both", supports_xhigh=True, supports_max=True),
        ModelCatalog(provider="c", model="max-only", supports_xhigh=False, supports_max=True),
        ModelCatalog(provider="c", model="xhigh-only", supports_xhigh=True, supports_max=False),
        ModelCatalog(provider="c", model="plain", supports_xhigh=False, supports_max=False),
    ]
    assert fit_effort("both", "max", catalog) == "max"
    assert fit_effort("max-only", "max", catalog) == "max"
    assert fit_effort("max-only", "xhigh", catalog) == "high"
    assert fit_effort("xhigh-only", "max", catalog) == "xhigh"
    assert fit_effort("plain", "max", catalog) == "high"
    assert fit_effort("plain", "medium", catalog) == "medium"


@pytest.mark.parametrize(
    ("model", "xhigh", "max_"),
    [
        ("claude-opus-5", True, True),
        ("claude-sonnet-5", True, True),
        ("claude-fable-5-1", True, True),
        ("claude-opus-4-7", True, True),
        # xhigh 从 Opus 4.7 起才有；Opus/Sonnet 4.6 只到 high 与 max。
        ("claude-opus-4-6", False, True),
        ("vllm/claude-sonnet-4-6", False, True),
        ("claude-opus-4-5-20251101", False, False),
        ("claude-haiku-4-5-20251001", False, False),
        ("codex/gpt-6-astra", True, True),
        ("codex/gpt-5.6-luna", True, True),
        ("codex/gpt-5.5", True, False),
        ("codex/gpt-5.3-codex", True, False),
        ("gpt-5.2-2025-12-11", True, False),
        # 没收录的模型默认都不支持，交给管理员。
        ("claude-test", False, False),
        ("minimax/MiniMax-M2.7", False, False),
    ],
)
def test_known_effort_flags(model: str, xhigh: bool, max_: bool) -> None:
    assert known_effort_flags(model) == {"supports_xhigh": xhigh, "supports_max": max_}
