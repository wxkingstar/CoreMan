"""Shared fixtures for Runtime deployment tests: a private CA, bundles and daemons."""

from __future__ import annotations

import contextlib
import datetime
import http.server
import io
import ipaddress
import json
import ssl
import tarfile
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from runtime_daemon.daemon import Daemon


@dataclass
class Pki:
    ca: Path
    cert: Path
    key: Path

    @property
    def ca_pem(self) -> str:
        return self.ca.read_text()


def make_pki(directory: Path) -> Pki:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Private CA")])
    usage = {
        "content_commitment": False,
        "key_encipherment": False,
        "data_encipherment": False,
        "key_agreement": False,
        "encipher_only": False,
        "decipher_only": False,
    }
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, **usage),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pki = Pki(directory / "ca.pem", directory / "server.pem", directory / "server.key")
    pki.ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    pki.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pki.key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pki


@contextlib.contextmanager
def https_server(
    pki: Pki, respond: Callable[[str], tuple[int, dict[str, str], bytes]]
) -> Iterator[int]:
    """Serve ``respond(path)`` over TLS signed by the private CA; yields the port."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            status, headers, body = respond(self.path)
            self.send_response(status)
            for name, value in {**headers, "Content-Length": str(len(body))}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass  # rejected handshakes are the point of some tests

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pki.cert, pki.key)
    server = Server(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def make_bundle(files: dict[str, tuple[str, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, (content, mode) in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def release_files(install_service: str = "") -> dict[str, tuple[str, int]]:
    """A minimal release: importable modules and drivers that answer ``--version``."""
    driver = "#!/bin/sh\necho test-driver\n"
    return {
        "runtime_daemon/__init__.py": ("", 0o644),
        "runtime_daemon/daemon.py": ("", 0o644),
        "runtime_daemon/install_service.py": (install_service, 0o644),
        "runtime_daemon/requirements.txt": ("pip\n", 0o644),
        "runtime_daemon/bin/runtime-claude": (driver, 0o755),
        "runtime_daemon/bin/runtime-codex": (driver, 0o755),
        "wheels/README": ("offline wheels\n", 0o644),
    }


def make_daemon(directory: Path, **extra) -> Daemon:
    root = directory / "projects"
    root.mkdir(parents=True, exist_ok=True)
    config = {
        "workspace_root": str(root),
        "api_url": "http://localhost",
        "node_id": str(uuid.uuid4()),
        "node_token": "t" * 32,
        "backends": {"claude": {"relay_id": "relay-claude"}, "codex": {"relay_id": "relay-codex"}},
        "schedules": False,
        "home": str(directory),
        **extra,
    }
    path = directory / "config.json"
    path.write_text(json.dumps(config))
    return Daemon(path)
