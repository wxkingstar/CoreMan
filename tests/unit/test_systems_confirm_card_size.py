"""The systems write confirmation card stays under Feishu's card size limit."""

from __future__ import annotations

import json
import uuid
from typing import Any

from coreman.core.systems_catalog import confirm


def item(n: int, *, note: str = "改动说明" * 20) -> dict[str, Any]:
    return {
        "system": "ops",
        "system_name": "运营后台",
        "operation_id": "upsertSeries",
        "summary": "新建或更新商品系列映射（按品牌）",
        "method": "PUT",
        "path": "/api/series/{seriesId}",
        "risk": "write",
        "path_params": {"seriesId": f"S-{n}"},
        "query": {},
        "body": {"brand": "品牌", "name": f"系列 {n}", "aliases": ["别名一", "别名二"] * 10},
        "note": {
            "target": f"商品 {n} 的系列",
            "changes": [{"field": "系列", "before": None, "after": f"系列 {n}"}] * 3,
            "impact": note,
            "rollback": note,
        },
        "files": [],
        "key": str(uuid.uuid4()),
        "digest": str(n),
    }


def size(card: dict[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode())


def test_a_full_batch_is_shortened_to_fit_one_card() -> None:
    items = [item(n) for n in range(confirm.MAX_ITEMS)]
    card = confirm.card(1, uuid.uuid4(), items)
    assert size(card) <= confirm.CARD_BYTES_MAX
    text = json.dumps(card, ensure_ascii=False)
    # Every item keeps its operation, object and endpoint, even when shortened.
    assert f"{confirm.MAX_ITEMS}. 新建或更新商品系列映射" in text
    assert f"商品 {confirm.MAX_ITEMS - 1} 的系列" in text
    assert f"S-{confirm.MAX_ITEMS - 1}" in text
    assert confirm.fits(items)


def test_a_small_batch_keeps_its_details() -> None:
    card = json.dumps(confirm.card(1, uuid.uuid4(), [item(1)]), ensure_ascii=False)
    assert "请求体" in card and "系列：系列 1" in card


def test_a_batch_that_cannot_fit_is_reported() -> None:
    # Path parameters are always shown in full (up to 500 characters each).
    items = [item(n) for n in range(confirm.MAX_ITEMS)]
    for entry in items:
        entry["path_params"] = {f"p{k}": "很长的路径参数" * 80 for k in range(3)}
    assert not confirm.fits(items)
