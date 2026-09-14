from coreman.core.db.models import ModelCatalog, RelayServer
from coreman.core.relay.models import backend_of, default_model, effective_models, supports_xhigh

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
    assert supports_xhigh("vllm/claude-opus-4-6", CATALOG)
    assert not supports_xhigh("vllm/claude-sonnet-4-6", CATALOG)
