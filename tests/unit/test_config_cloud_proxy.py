import base64
import json

import pytest

from coreman.core.config import Settings, reset_settings_cache

PRODUCTS = ("alidns", "domain", "mse", "slb")
SECRETS = {
    "TOWER_CALLBACK_SECRET": "cb-secret-value",
    "TOWER_SENDER_KEY": "sender-key-value",
    "ALIYUN_PROXY_ACCESS_KEY_ID": "ak-id-value",
    "ALIYUN_PROXY_ACCESS_KEY_SECRET": "ak-secret-value",
}


def _env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    drop: str | None = None,
    products: tuple[str, ...] = PRODUCTS,
) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/coreman",
        "PUBLIC_BASE_URL": "https://coreman.example.com",
        "MASTER_KEY": base64.b64encode(b"\x01" * 32).decode(),
        "SESSION_SECRET": "s" * 32,
        "TOWER_URL": "https://tower.example.com",
        "TOWER_TARGET_KEYS": json.dumps({p: f"key-{p}-value" for p in products}),
        **SECRETS,
    }
    if drop:
        base.pop(drop)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_ready_when_everything_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _env(monkeypatch).cloud_proxy_ready is True


@pytest.mark.parametrize("missing", ["TOWER_URL", "TOWER_TARGET_KEYS", *SECRETS])
def test_not_ready_when_any_item_is_missing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    assert _env(monkeypatch, drop=missing).cloud_proxy_ready is False


def test_not_ready_when_a_product_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _env(monkeypatch, products=("alidns", "domain", "mse")).cloud_proxy_ready is False


def test_target_keys_default_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOWER_TARGET_KEYS", raising=False)
    settings = _env(monkeypatch, drop="TOWER_TARGET_KEYS")
    assert settings.tower_target_keys == {}


def test_repr_hides_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    text = repr(_env(monkeypatch))
    for value in [*SECRETS.values(), *(f"key-{p}-value" for p in PRODUCTS)]:
        assert value not in text


POLICY_JSON = {
    "alidns": {
        "managed_zones": ["example.com"],
        "trusted_cname_suffixes": [".example.net"],
        "trusted_ips": ["192.0.2.10"],
    },
    "mse": {
        "endpoint": "mse.cn-hangzhou.aliyuncs.com",
        "instance_id": "mse-example-instance",
        "namespaces": {"ns-test": "test", "ns-prod": "prod"},
        "low_risk_envs": ["test"],
    },
    "slb": {"acls": {"acl-example-test": "test"}},
}


def test_policy_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUD_PROXY_POLICY", raising=False)
    settings = _env(monkeypatch)
    assert settings.cloud_proxy_policy is None
    assert settings.cloud_proxy_ready is True


def test_policy_is_parsed_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUD_PROXY_POLICY", json.dumps(POLICY_JSON))
    policy = _env(monkeypatch).cloud_proxy_policy
    assert policy is not None
    assert policy.alidns.managed_zones == ("example.com",)
    assert policy.mse.namespaces == {"ns-test": "test", "ns-prod": "prod"}
    assert policy.mse.low_risk_envs == ("test",)
    assert policy.slb.acls == {"acl-example-test": "test"}


@pytest.mark.parametrize(
    "patch",
    [
        {"extra": {}},
        {"alidns": {"managed_zones": ["Example.COM"]}},
        {"alidns": {"trusted_cname_suffixes": ["example.net"]}},
        {"alidns": {"trusted_ips": ["not-an-ip"]}},
        {"alidns": {"trusted_ips": ["2001:db8:0::1"]}},
        {"alidns": {"unknown": []}},
        {"mse": {"endpoint": "evil.example.com"}},
        {"mse": {"namespaces": {"ns-test": "test"}, "low_risk_envs": ["prod"]}},
        {"mse": {"namespaces": {"ns-test": ""}}},
        {"mse": {"instance_id": ""}},
        {"slb": {"acls": {"": "test"}}},
        {"slb": {"acls": ["acl-example-test"]}},
    ],
)
def test_policy_is_validated_strictly(
    monkeypatch: pytest.MonkeyPatch, patch: dict[str, object]
) -> None:
    monkeypatch.setenv("CLOUD_PROXY_POLICY", json.dumps(patch))
    with pytest.raises(ValueError):
        _env(monkeypatch)
