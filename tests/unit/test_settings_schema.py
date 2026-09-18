import pytest
from pydantic import ValidationError

from coreman.core.settings_schema import SETTING_DEFAULTS, SettingsPatch


def test_patch_partial_and_bounds() -> None:
    assert SettingsPatch(session_ttl_hours=120).changes() == {"session_ttl_hours": 120}
    assert SettingsPatch(default_effort_level=None).changes() == {"default_effort_level": None}
    assert SettingsPatch(session_ttl_hours=720).changes() == {"session_ttl_hours": 720}
    with pytest.raises(ValidationError):
        SettingsPatch(session_ttl_hours=721)
    with pytest.raises(ValidationError):
        SettingsPatch(session_ttl_hours=0)
    with pytest.raises(ValidationError):
        SettingsPatch(jwt_issuer="Bad Issuer")
    assert set(SETTING_DEFAULTS) == {
        "bootstrap_admin_enabled",
        "default_verbosity_level",
        "default_effort_level",
        "session_ttl_hours",
        "jwt_issuer",
        "max_concurrent_tasks",
        "fast_lane_slots",
        "card_icon_url",
        "wecom_qr_provisioning_enabled",
        "prompt_security_policy",
        "prompt_codex_contract",
        "prompt_runtime_mode",
        "prompt_cron_mode",
        "prompt_runtime_tail",
        "prompt_verbosity_2",
        "prompt_verbosity_3",
        "prompt_verbosity_4",
    }


def test_concurrency_keys() -> None:
    assert (
        SETTING_DEFAULTS["max_concurrent_tasks"] == 30 and SETTING_DEFAULTS["fast_lane_slots"] == 2
    )
    assert SettingsPatch(max_concurrent_tasks=10, fast_lane_slots=1).changes() == {
        "max_concurrent_tasks": 10,
        "fast_lane_slots": 1,
    }
    with pytest.raises(ValidationError):
        SettingsPatch(max_concurrent_tasks=0)


def test_card_icon_url_accepts_https_or_empty() -> None:
    assert SettingsPatch(card_icon_url="").changes() == {"card_icon_url": ""}
    assert SettingsPatch(card_icon_url="https://cdn.example.com/a.png").changes() == {
        "card_icon_url": "https://cdn.example.com/a.png"
    }
    with pytest.raises(ValidationError):
        SettingsPatch(card_icon_url="ftp://x/y.png")
    with pytest.raises(ValidationError):
        SettingsPatch(card_icon_url="https://" + "a" * 600)
    assert SETTING_DEFAULTS["card_icon_url"] == ""


def test_prompt_keys() -> None:
    from coreman.core.prompting.defaults import DEFAULT_SECURITY_POLICY
    from coreman.core.settings_schema import PROMPT_SETTING_KEYS

    assert SETTING_DEFAULTS["prompt_security_policy"] == DEFAULT_SECURITY_POLICY
    assert set(PROMPT_SETTING_KEYS) == {
        "prompt_security_policy",
        "prompt_codex_contract",
        "prompt_runtime_mode",
        "prompt_cron_mode",
        "prompt_runtime_tail",
        "prompt_verbosity_2",
        "prompt_verbosity_3",
        "prompt_verbosity_4",
    }
    assert SettingsPatch(prompt_cron_mode="x").changes() == {"prompt_cron_mode": "x"}
    with pytest.raises(ValidationError):
        SettingsPatch(prompt_cron_mode="")
    assert SettingsPatch(prompt_runtime_tail="x").changes() == {"prompt_runtime_tail": "x"}
    with pytest.raises(ValidationError):
        SettingsPatch(prompt_runtime_tail="")


def test_legacy_agent_timeout_is_not_configurable() -> None:
    assert "agent_timeout_seconds" not in SETTING_DEFAULTS
    assert "agent_timeout_seconds" not in SettingsPatch.model_json_schema()["properties"]
    assert SettingsPatch(agent_timeout_seconds=30).changes() == {}


def test_default_model_is_derived_not_stored() -> None:
    """默认模型由模型目录派生：不在默认值表里，也不能通过设置写入。"""
    from coreman.core.db.models import ModelCatalog
    from coreman.core.settings_schema import DERIVED_SETTING_KEYS, platform_default_model

    assert DERIVED_SETTING_KEYS == ("default_model",)
    assert "default_model" not in SETTING_DEFAULTS
    assert "default_model" not in SettingsPatch.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        SettingsPatch.model_validate({"default_model": "x/y"})
    with pytest.raises(ValidationError):
        SettingsPatch.model_validate({"default_model": None, "session_ttl_hours": 24})

    def row(
        provider: str, model: str, *, default: bool = False, retired: bool = False
    ) -> ModelCatalog:
        return ModelCatalog(
            provider=provider, model=model, is_default=default, retired=retired, sort_order=0
        )

    assert platform_default_model([]) is None
    codex = row("codex", "codex/a", default=True)
    assert platform_default_model([codex]) == "codex/a"
    assert platform_default_model([codex, row("claude", "c/a", default=True)]) == "c/a"
    assert platform_default_model([codex, row("claude", "c/a", retired=True)]) == "codex/a"
    assert platform_default_model([row("minimax", "m/a", default=True)]) is None
