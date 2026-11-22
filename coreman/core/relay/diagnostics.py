"""Exception provenance without request bodies, URLs, credentials or frame locals."""

import errno
import socket
import ssl
import traceback
from pathlib import Path
from typing import Any

from coreman.core.runtime_nodes.errors import RuntimeRequestRejected


def exception_details(error: BaseException) -> dict[str, Any]:
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item: dict[str, Any] = {"type": type(current).__name__, "module": type(current).__module__}
        if isinstance(current, OSError) and isinstance(current.errno, int):
            item["errno"] = current.errno
            item["errno_name"] = errno.errorcode.get(current.errno, "unknown")
        if isinstance(current, socket.gaierror):
            item["reason"] = "dns_resolution_failed"
        elif isinstance(current, ssl.SSLCertVerificationError):
            item["reason"] = "tls_certificate_verification_failed"
            item["verify_code"] = current.verify_code
        elif isinstance(current, RuntimeRequestRejected):
            item.update(
                code=current.code,
                request_bytes=current.request_bytes,
                limit_bytes=current.limit_bytes,
            )
        else:
            # HTTP/core wrappers sometimes preserve only text, losing errno.
            # Recognize fixed diagnostics without copying the original text.
            diagnostic = str(current).lower()
            for marker, reason in (
                ("connection refused", "connection_refused"),
                ("connection reset", "connection_reset"),
                ("broken pipe", "broken_pipe"),
                ("certificate verify failed", "tls_certificate_verification_failed"),
                ("name or service not known", "dns_resolution_failed"),
                ("nodename nor servname", "dns_resolution_failed"),
                ("server disconnected", "peer_disconnected"),
                ("all connection attempts failed", "connection_attempts_failed"),
            ):
                if marker in diagnostic:
                    item["reason"] = reason
                    break
        # Traceback locations and types remain useful even for private chats.
        # Never serialize arbitrary exception text: it may echo prompts or auth.
        item["frames"] = [
            {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
            for f in traceback.extract_tb(current.__traceback__)[-16:]
        ]
        chain.append(item)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {"exception_chain": chain}
