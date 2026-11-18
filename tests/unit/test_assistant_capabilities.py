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
    ("delivery", "mcp", "expected"),
    [
        # 没人点名的一轮：令牌会进运行环境的系统（env、mcp）不下发，标成收起而不是可读写。
        ("env", False, "withheld"),
        ("mcp", True, "withheld"),
        # 运行环境本来就不支持的仍按不支持说。
        ("mcp", False, "unsupported"),
        # 平台代理的系统照常。
        ("proxy", False, "confirm"),
    ],
)
def test_level_tokenless(delivery: str, mcp: bool, expected: str) -> None:
    got = cap.level(
        system(delivery), True, mode="attended", mcp_supported=mcp, mounted=True, tokenless=True
    )  # type: ignore[arg-type]
    assert got == expected


def test_note_lists_systems_and_products() -> None:
    lines = [
        cap.SystemLine("orders", "订单系统", "read"),
        cap.SystemLine("tickets", "工单", "confirm"),
    ]
    text = cap.note(lines, ["alidns", "domain"], mode="attended", cloud_attached=True)
    assert text.startswith(cap.HEAD)
    assert "订单系统（orders）只读" in text and "工单（tickets）可读写" in text
    assert "云签名代理：alidns、domain" in text and "确认卡" in text
    assert cap.usable(lines + [cap.SystemLine("x", "X", "unsupported")]) == {"orders", "tickets"}


def test_note_marks_withheld_systems() -> None:
    lines = [cap.SystemLine("crm", "客户系统", "withheld")]
    text = cap.note(lines, [], mode="attended", cloud_attached=False)
    assert "客户系统（crm）本轮没下发" in text and "可读写" not in text
    # 收起的系统仍是授权了的：缺口照样算解决。
    assert cap.usable(lines) == {"crm"}


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
