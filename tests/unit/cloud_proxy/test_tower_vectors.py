"""vendored tower_gate 必须算出与 Tower 同一份向量相同的哈希，否则令牌全部 request_mismatch。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from coreman.core.cloud_proxy import tower_gate

VECTORS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "tower_vectors.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vendored_hash_matches_tower_vectors(vector: dict[str, Any]) -> None:
    got = tower_gate.request_hash(
        vector["method"],
        vector["target"],
        vector["url"],
        base64.b64decode(vector["body_b64"]),
        vector["caller"],
    )
    assert got == vector["hash"]
