"""The batch confirmation card: field counts, a sample that keeps rare values, one card's size."""

from __future__ import annotations

import json
import uuid
from typing import Any

from coreman.core.db.models import InteractionState
from coreman.core.systems_catalog import batch, confirm


def item(n: int, price: str) -> dict[str, Any]:
    return {
        "path_params": {"listingId": f"L{n:05d}"},
        "body": {"price": price, "title": f"商品标题 {n} " + "很长的描述" * 10},
        "target": f"挂牌 L{n:05d} 的商品" + "名" * 40,
        "before": {"price": "299.00"},
    }


def items() -> list[dict[str, Any]]:
    out = [item(n, "199.00") for n in range(997)]
    out += [item(997, "0.01"), item(998, "1.00")]
    out.append(item(5, "199.00"))  # 同一对象出现两次
    return out


def state(count: int) -> InteractionState:
    return InteractionState(
        id=uuid.uuid4(),
        kind=batch.KIND,
        state={
            "origin_task_id": 7,
            "count": count,
            "system_name": "商品后台",
            "operation_id": "updateListing",
            "summary": "修改挂牌（PUT）",
            "method": "PUT",
            "path": "/api/listings/{listingId}",
            "risk": "write",
        },
    )


def test_summary_counts_values_and_repeated_objects() -> None:
    summary = batch.summarize(items())
    price = next(f for f in summary["fields"] if f["field"] == "price")
    assert price["top"][0] == ["199.00", 998]
    assert summary["repeats"] == [[{"listingId": "L00005"}, 2]]
    lines = batch.stats_lines(summary)
    assert any(line.startswith("price：199.00（998 项）") for line in lines)
    assert any("同一对象出现多次" in line for line in lines)


def test_the_sample_keeps_rare_values_and_repeats_and_is_stable() -> None:
    rows = items()
    picked = batch.pick(rows, seed=7)
    assert len(picked) == batch.PREVIEW_MAX and picked == sorted(picked)
    prices = {rows[i]["body"]["price"] for i in picked}
    assert {"0.01", "1.00"} <= prices
    assert 5 in picked and 999 in picked
    assert batch.pick(rows, seed=7) == picked


def test_a_thousand_items_fit_one_card() -> None:
    rows = items()
    sealed = {
        "note": {"impact": "影响" * 100, "rollback": "恢复" * 100},
        "summary": batch.summarize(rows),
        "preview": [[i + 1, rows[i]] for i in batch.pick(rows, seed=7)],
    }
    card = batch.render(state(len(rows)), sealed, "https://example.com/csv?signature=x")
    size = len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= confirm.CARD_BYTES_MAX
    text = card["body"]["elements"][0]["text"]["content"]
    assert text.startswith("共 1000 项") and "【预览" in text
    assert card["header"]["title"]["content"] == "请确认 1000 项操作"


def test_csv_cells_cannot_start_a_formula() -> None:
    assert batch._cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert batch._cell("-1") == "'-1"
    assert batch._cell({"a": 1}) == '{"a": 1}'
    assert batch._cell(None) == ""
