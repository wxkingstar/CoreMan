from pathlib import Path

import yaml  # 需在 dev 依赖中加入 pyyaml>=6.0

ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))


def test_services_and_grace_periods() -> None:
    services = _compose()["services"]
    expected = {
        "postgres",
        "migrate",
        "api-1",
        "api-2",
        "caddy",
        "gateway-wecom-a",
        "gateway-wecom-b",
        "gateway-feishu-a",
        "gateway-feishu-b",
        "worker-a",
        "worker-b",
        "scheduler",
    }
    assert expected <= set(services)
    assert services["worker-a"]["stop_grace_period"] == "7200s"
    assert services["gateway-wecom-a"]["stop_grace_period"] == "120s"
    assert services["scheduler"]["stop_grace_period"] == "30s"
    assert services["api-1"]["stop_grace_period"] == "30s"


def test_only_caddy_publishes_ports_and_standby_profiles() -> None:
    services = _compose()["services"]
    for name, svc in services.items():
        if name == "caddy":
            expected_ports = {"${CADDY_HTTP_PORT:-80}:80", "${CADDY_HTTPS_PORT:-443}:443"}
            assert set(svc["ports"]) == expected_ports
        elif name == "prometheus":
            assert svc["profiles"] == ["monitoring"]
            assert svc["ports"] == ["127.0.0.1:9090:9090"]
        else:
            assert "ports" not in svc, name
    assert services["gateway-wecom-b"]["profiles"] == ["standby"]
    assert services["gateway-feishu-b"]["profiles"] == ["standby"]
    assert "profiles" not in services["worker-b"]


def test_api_trusts_forwarded_headers_only_from_caddy_edge_network() -> None:
    compose = _compose()
    services = compose["services"]
    subnet = compose["networks"]["edge"]["ipam"]["config"][0]["subnet"]
    edge_members = {name for name, svc in services.items() if "edge" in svc.get("networks", [])}
    assert edge_members == {"caddy", "api-1", "api-2"}
    assert services["caddy"]["networks"] == ["edge"]
    for name in ("api-1", "api-2"):
        assert services[name]["environment"]["FORWARDED_ALLOW_IPS"] == subnet
    dockerfile = (ROOT / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" not in dockerfile and '"*"' not in dockerfile


async def test_edge_subnet_default_controls_client_ip() -> None:
    """uvicorn 按 CIDR 判断来源：edge 网段内的 Caddy 转发生效，其他容器伪造的头被忽略，
    client_ip 读到的就是改写后的 client.host。"""
    import re

    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from coreman.api.deps import client_ip

    expr = _compose()["networks"]["edge"]["ipam"]["config"][0]["subnet"]
    match = re.fullmatch(r"\$\{COREMAN_EDGE_SUBNET:-(.+)\}", expr)
    assert match
    seen: list[str] = []

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        seen.append(client_ip(Request(scope)))

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=match.group(1))
    network = match.group(1).rsplit(".", 1)[0]
    for peer in (f"{network}.2", "172.18.0.5"):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"203.0.113.9")],
            "client": (peer, 40000),
            "scheme": "http",
            "server": ("api-1", 8000),
        }
        await middleware(scope, None, None)  # type: ignore[arg-type]
    assert seen == ["203.0.113.9", "172.18.0.5"]


def test_runtime_entrypoints_and_health_ports() -> None:
    services = _compose()["services"]
    assert services["worker-a"]["command"][-1] == "coreman.runtime.worker"
    assert services["gateway-wecom-a"]["command"][-1] == "coreman.runtime.gateway_wecom"
    assert services["gateway-feishu-a"]["command"][-1] == "coreman.runtime.gateway_feishu"
    assert services["scheduler"]["command"][-1] == "coreman.runtime.scheduler"
    assert "9103/health" in " ".join(services["worker-b"]["healthcheck"]["test"])
    assert "9104/health" in " ".join(services["scheduler"]["healthcheck"]["test"])


def test_env_file_follows_coreman_env_file() -> None:
    """deploy/coreman 把 COREMAN_ENV_FILE 绝对化后导出，三处 env_file 必须跟着它走。
    否则 COREMAN_ENV_FILE 指向别处时，postgres 用自定义文件里的 POSTGRES_PASSWORD，
    api/migrate/runtime 却用 $ROOT/.env 里的 DATABASE_URL，凭证直接错配。"""
    services = _compose()["services"]
    for name in ("migrate", "api-1", "worker-a", "gateway-wecom-a", "scheduler"):
        assert services[name]["env_file"] == "${COREMAN_ENV_FILE:-.env}", name
