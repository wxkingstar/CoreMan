"""One trust chain for every control-plane connection the Runtime makes.

httpx and requests both run with ``trust_env=False`` so proxy variables stay ignored; that
also ignores ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE``. Trust therefore comes from explicit
sources only: the bundled certifi roots, the system CA bundle (what ``install.sh`` saw) and
an optional private CA from the ``ca_file`` setting.
"""

from __future__ import annotations

import ssl
from pathlib import Path

from runtime_daemon.lifecycle import FatalConfigError, write_private

TRUST_FILENAME = "trust.pem"
PEM_MARKER = b"-----BEGIN CERTIFICATE-----"
# The compiled OpenSSL default comes first; the rest cover distributions whose Python
# links a different OpenSSL than the system one.
SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)


class TrustError(FatalConfigError):
    """The configured private CA cannot be used."""


def certifi_bundle() -> Path | None:
    try:
        import certifi
    except ImportError:
        return None
    return Path(certifi.where())


def system_bundle() -> Path | None:
    # openssl_cafile is the compiled default; ``cafile`` would honour SSL_CERT_FILE.
    for candidate in (ssl.get_default_verify_paths().openssl_cafile, *SYSTEM_BUNDLES):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def read_private_ca(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TrustError(f"无法读取私有 CA 文件 {path}：{exc.strerror or exc}") from exc
    if PEM_MARKER not in data:
        raise TrustError(f"私有 CA 文件 {path} 不含 PEM 格式证书")
    try:
        ssl.create_default_context(cadata=data.decode("ascii", "replace"))
    except (ssl.SSLError, ValueError) as exc:
        raise TrustError(f"私有 CA 文件 {path} 无法解析") from exc
    return data


def build_trust_bundle(data_dir: Path, ca_file: str | None) -> Path | None:
    """Write ``trust.pem`` (0600) and return it; ``None`` keeps library defaults."""
    parts = []
    for source in (certifi_bundle(), system_bundle()):
        if source:
            try:
                parts.append(source.read_bytes())
            except OSError:
                continue
    if ca_file:
        path = Path(ca_file)
        parts.append(read_private_ca(path if path.is_absolute() else data_dir / path))
    if not parts:
        return None
    bundle = data_dir / TRUST_FILENAME
    write_private(bundle, b"\n".join(part.rstrip(b"\n") for part in parts) + b"\n")
    return bundle


def client_context(bundle: Path | None) -> ssl.SSLContext:
    if bundle is None:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=str(bundle))
