"""安装链接的私有 CA：校验 PEM、随配置注入安装脚本，安装命令提示 curl --cacert。"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.api.conftest import login_as

ENDPOINT = "/api/admin/runtime-nodes/install-links"


def ca_certificate() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Private CA")])
    moment = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - timedelta(minutes=1))
        .not_valid_after(moment + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, private


def link_body(ca_pem: str) -> dict[str, object]:
    return {"workspace_root": "/home/ai/projects", "options": {"ca_pem": ca_pem}}


async def test_ca_pem_reaches_install_script_and_command_uses_cacert(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    pem, _ = ca_certificate()
    response = await client.post(ENDPOINT, json=link_body(pem.replace("\n", "\r\n")))
    assert response.status_code == 200, response.text
    link = response.json()["data"]
    assert link["command"].startswith("curl --cacert coreman-ca.pem -fsSL ")
    assert "coreman-ca.pem" in link["ca_hint"]
    script = (await client.get(link["url"].replace("http://testserver", ""))).text
    encoded = re.search(r"b64decode\('([A-Za-z0-9+/=]+)'\)", script)
    assert encoded, "install script must embed the base64 config"
    config = json.loads(base64.b64decode(encoded.group(1)))
    assert config["ca_pem"] == pem  # 规范为 LF 换行；安装脚本 pop 后写成 ca.pem / ca_file
    listed = await client.get(ENDPOINT)
    assert "BEGIN CERTIFICATE" not in listed.text

    plain = (await client.post(ENDPOINT, json=link_body(""))).json()["data"]
    assert plain["command"].startswith("curl -fsSL ") and "ca_hint" not in plain


async def test_ca_pem_rejects_keys_garbage_and_oversize(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    pem, private = ca_certificate()
    broken = "-----BEGIN CERTIFICATE-----\nnot-a-certificate\n-----END CERTIFICATE-----\n"
    for bad in (
        "hello",
        private,
        pem + private,
        pem + "trailing text",
        broken,
        pem * (64 * 1024 // len(pem) + 1),
    ):
        response = await client.post(ENDPOINT, json=link_body(bad))
        assert response.status_code == 422, (bad[:40], response.text)
    # 多张证书组成的信任链可以。
    assert (await client.post(ENDPOINT, json=link_body(pem + pem))).status_code == 200
