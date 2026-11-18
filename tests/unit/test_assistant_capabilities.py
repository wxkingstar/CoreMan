"""能力清单：每个系统的级别按交付方式、写入授权、在场与否、运行时能力判定；说明文字只说本轮挂了什么。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coreman.core.assistants import capabilities as cap


def system(
    delivery: str, openapi: str | None = "https://orders.example.com/openapi.json"
) -> SimpleNamespace:
    return SimpleNamespace(token_delivery=delivery, openapi_url=openapi)


@pytest.mark.parametrize(
    ("delivery", "write", "mode", "mcp", "mounted", "expected"),
    [
        ("proxy", True, "attended", False, True, "confirm"),
        ("proxy", True, "unattended", False, True, "approval"),
        ("proxy", False, "attended", False, True, "read"),
        ("proxy", True, "attended", False, False, "unsupported"),
        ("env", False, "attended", False, False, "confirm"),
        ("env", True, "unattended", False, False, "read"),
        ("mcp", False, "attended", False, False, "unsupported"),
        ("mcp", False, "attended", True, False, "confirm"),
    ],
)
def test_level(
    delivery: str, write: bool, mode: str, mcp: bool, mounted: bool, expected: str
) -> None:
    got = cap.level(system(delivery), write, mode=mode, mcp_supported=mcp, mounted=mounted)  # type: ignore[arg-type]
    assert got == expected


@pytest.mark.parametrize(
    ("delivery", "mcp", "mounted", "expected"),
    [
        # 这一轮没拿到（没人点名、发言者身份不可用、签发失败、调度回合）：标成没下发，不说可读写。
        ("env", False, False, "withheld"),
        ("mcp", True, False, "withheld"),
        ("proxy", False, True, "withheld"),
        # 运行环境本来就不支持的仍按不支持说。
        ("mcp", False, False, "unsupported"),
        ("proxy", False, False, "unsupported"),
    ],
)
def test_level_not_delivered(delivery: str, mcp: bool, mounted: bool, expected: str) -> None:
    got = cap.level(
        system(delivery),  # type: ignore[arg-type]
        True,
        mode="attended",
        mcp_supported=mcp,
        mounted=mounted,
        delivered=False,
    )
    assert got == expected


@pytest.mark.parametrize(
    ("delivery", "write", "openapi", "expected"),
    [
        ("proxy", False, "https://orders.example.com/openapi.json", 1),
        ("proxy", True, "https://orders.example.com/openapi.json", 2),
        # 没有 OpenAPI 地址的代理系统调不了：和没授权一样。
        ("proxy", True, None, 0),
        ("env", False, None, 2),
        ("mcp", False, None, 2),
    ],
)
def test_grant_rank(delivery: str, write: bool, openapi: str | None, expected: int) -> None:
    assert cap.grant_rank(system(delivery, openapi), write) == expected  # type: ignore[arg-type]


def test_note_lists_systems_and_products() -> None:
    lines = [
        cap.SystemLine("orders", "订单系统", "read"),
        cap.SystemLine("tickets", "工单", "confirm"),
    ]
    text = cap.note(lines, ["alidns", "domain"], mode="attended", cloud_attached=True)
    assert text.startswith(cap.HEAD)
    assert "订单系统（orders）只读" in text and "工单（tickets）可读写" in text
    assert "云签名代理：alidns、domain" in text and "确认卡" in text


def test_note_marks_withheld_systems() -> None:
    lines = [cap.SystemLine("crm", "客户系统", "withheld")]
    text = cap.note(lines, [], mode="attended", cloud_attached=False)
    assert "客户系统（crm）本轮没下发" in text and "可读写" not in text


def test_note_when_nothing_or_withheld() -> None:
    assert "没有授权给你的" in cap.note([], [], mode="attended", cloud_attached=False)
    assert "云签名代理：本轮不可用" in cap.note(
        [], ["alidns"], mode="unattended", cloud_attached=False
    )
    assert cap.note([], ["alidns"], mode="withheld", cloud_attached=False) == cap.WITHHELD


def test_cloud_products_follow_configuration() -> None:
    off = SimpleNamespace(cloud_proxy_ready=False, cloud_proxy_policy=None)
    on = SimpleNamespace(cloud_proxy_ready=True, cloud_proxy_policy=None)
    assert cap.cloud_products(off) == []  # type: ignore[arg-type]
    # 没配 MSE 的地域 endpoint：mse 不算放行。
    assert cap.cloud_products(on) == ["alidns", "domain", "slb"]  # type: ignore[arg-type]
