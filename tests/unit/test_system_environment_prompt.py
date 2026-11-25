from types import SimpleNamespace
from typing import Any

from coreman.core.auth.system_access import TEST_ENVIRONMENT_RULE, systems_prompt


def _system(key: str, environment: str) -> Any:
    return SimpleNamespace(
        key=key,
        name="Growth",
        base_url=f"https://{key}.example.com",
        openapi_url=f"https://{key}.example.com/openapi.yaml",
        token_delivery="env",
        token_provider="builtin",
        environment=environment,
    )


def test_test_environment_systems_are_labelled_and_explained() -> None:
    prompt = systems_prompt(
        [_system("growth", "production"), _system("growth_test", "test")], mounted=True
    )
    assert "- Growth (growth)：" in prompt
    assert "- Growth (growth_test，测试环境)：" in prompt
    assert prompt.count(TEST_ENVIRONMENT_RULE) == 1


def test_no_rule_without_test_systems() -> None:
    prompt = systems_prompt([_system("growth", "production")], mounted=True)
    assert "测试环境" not in prompt
