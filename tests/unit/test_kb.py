"""调度员知识库里不碰数据库的部分：规则过滤、文档切段、抽取结果解析、编译后检查、到点判断。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from coreman.core.kb import extract, filters, rounds, runner, sources, wiki

TODAY = date(2026, 11, 28)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("找 13800138000 确认", "手机号"),
        ("发到 someone@example.com", "邮箱"),
        ("身份证 11010519491231002X", "证件号"),
        ("见 https://example.com/orders?token=abc", "带参数的链接"),
        ("服务在 192.0.2.10 上", "网络地址"),
        ("这单退了 1,280 元", "金额"),
        ("预算 $500", "金额"),
        ("订单 202611280001 已发货", "长数字"),
        ("api_key: abcdefghijklmnop1234", "密钥"),
    ],
)
def test_filter_rules_hit(text: str, rule: str) -> None:
    assert filters.hit(text) == rule


@pytest.mark.parametrize(
    "text",
    [
        "订单同步以 A 系统的接口文档为准（T-128，K-345）",
        "2026-11-28 起合并前要人确认",
        "批量上限 5 万项，超过 50 项先试跑",
        "文档在 https://example.com/docs/design-oms.md",
        "需要 Codex CLI 0.169.1 以上",
    ],
)
def test_filter_rules_pass_ordinary_knowledge(text: str) -> None:
    assert filters.hit(text) is None


def test_scan_reports_line_numbers() -> None:
    assert filters.scan("第一行\n联系 13800138000\n\n价格 99 元") == [(2, "手机号"), (4, "金额")]


def test_sections_split_on_headings_and_keep_heading_with_body() -> None:
    text = "开头\n\n# A\n内容 a\n## B\n内容 b\n"
    assert sources.sections(text) == ["开头", "# A\n内容 a", "## B\n内容 b"]


def test_pack_respects_limit_and_splits_oversized_sections() -> None:
    assert sources.pack(["a" * 6, "b" * 6, "c" * 25], limit=10) == [
        "a" * 6,
        "b" * 6,
        "c" * 10,
        "c" * 10,
        "c" * 5,
    ]
    assert sources.pack(["aaa", "bbb"], limit=10) == ["aaa\n\nbbb"]


def test_doc_unit_only_sends_sections_not_seen_before() -> None:
    first = sources.doc_unit("rules", "k", "rules:k", "文档", "# A\n一\n# B\n二", None, TODAY)
    assert first is not None and len(first.batches) == 1
    assert "一" in first.batches[0].text and "二" in first.batches[0].text
    assert (
        sources.doc_unit("rules", "k", "rules:k", "文档", "# A\n一\n# B\n二", first.mark, TODAY)
        is None
    )
    changed = sources.doc_unit(
        "rules", "k", "rules:k", "文档", "# A\n一\n# B\n改了\n# C\n三", first.mark, TODAY
    )
    assert changed is not None
    text = changed.batches[0].text
    assert "改了" in text and "三" in text and "一" not in text
    # 只删了一段：没有要抽的，但进度要更新。
    removed = sources.doc_unit("rules", "k", "rules:k", "文档", "# A\n一", first.mark, TODAY)
    assert removed is not None and removed.batches == () and len(removed.mark) == 1


def test_render_system_has_no_addresses() -> None:
    system = SimpleNamespace(
        name="订单系统",
        key="orders",
        environment="production",
        description="管订单",
        default_for_all_bots=False,
        base_url="https://orders.example.com",
    )
    compiled = {
        "guide": "先查再改",
        "modules": [{"name": "refund", "display_name": "退款", "operations": ["a", "b"]}],
        "operations": {
            "a": {"summary": "查退款单", "risk": "read"},
            "b": {"summary": "旧接口", "risk": "write", "deprecated": True},
        },
    }
    text = sources.render_system(system, compiled, ["订单助手"])  # type: ignore[arg-type]
    assert "业务系统「订单系统」（orders）" in text
    assert "可用的 AI 员工：订单助手" in text
    assert "- 查退款单（读）" in text
    assert "旧接口" not in text and "example.com" not in text


def batch(*refs: str) -> sources.Batch:
    return sources.Batch(
        source="task", refs=refs, label="任务", text="…", dates={r: TODAY for r in refs}
    )


def test_parse_keeps_valid_items_and_fixes_single_source() -> None:
    output = (
        "好的：\n```json\n"
        '[{"kind": "decision", "topic": "订单同步", "text": "以 A 系统为准。", '
        '"confidence": "inferred", "source": "乱写的"},'
        ' {"kind": "gossip", "topic": "x", "text": "y"},'
        ' {"kind": "role", "topic": "", "text": "没有主题"},'
        ' "不是对象"]\n```'
    )
    drafts = extract.parse(output, batch("T-1"))
    assert [(d.kind, d.source_ref, d.confidence, d.occurred_on) for d in drafts] == [
        ("decision", "T-1", "inferred", TODAY)
    ]


def test_parse_drops_items_whose_source_is_not_in_the_batch() -> None:
    output = (
        '[{"kind": "term", "topic": "口径", "text": "按支付时间算。", "source": "T-2"},'
        ' {"kind": "term", "topic": "口径", "text": "不知道出自哪。", "source": "T-9"}]'
    )
    drafts = extract.parse(output, batch("T-1", "T-2"))
    assert [d.source_ref for d in drafts] == ["T-2"]


def test_parse_caps_the_number_of_items_per_batch() -> None:
    rows = [
        {"kind": "term", "topic": "口径", "text": f"第 {n} 条。", "source": "T-1"}
        for n in range(extract.MAX_ITEMS + 5)
    ]
    drafts = extract.parse(json.dumps(rows, ensure_ascii=False), batch("T-1"))
    assert len(drafts) == extract.MAX_ITEMS


def test_extract_prompt_tells_the_model_what_a_dispatcher_does_not_need() -> None:
    for phrase in ("调度员", "操作手册", "每个 AI 员工都一样的通用约定", "宁少勿多"):
        assert phrase in extract.SYSTEM_PROMPT


def test_parse_rejects_output_without_an_array() -> None:
    with pytest.raises(extract.ParseError):
        extract.parse("没有可抽的内容。", batch("T-1"))
    assert extract.parse("[]", batch("T-1")) == []


def test_extract_content_names_allowed_sources_and_wraps_material() -> None:
    text = extract.content(batch("T-1", "T-2"))
    assert "可用的来源标识：T-1、T-2" in text
    assert "<材料>" in text and "</材料>" in text


AGENTS_BEFORE = "# 调度员\n人写的：找 13800138000\n"


def agents(block: str) -> str:
    return f"{AGENTS_BEFORE}\n{wiki.ROUTES_START}\n{block}\n{wiki.ROUTES_END}\n"


def test_check_only_looks_at_changed_files_and_the_routes_block() -> None:
    before = {"AGENTS.md": AGENTS_BEFORE, "docs/old.md": "旧页 someone@example.com"}
    after = {
        "AGENTS.md": agents("订单归订单助手\n服务器 192.0.2.10"),
        "docs/old.md": "旧页 someone@example.com",
        "docs/roles.md": "# 职责\n张三负责退款（T-1，K-7）\n电话 13800138000（K-8）",
    }
    assert wiki.check(before, after) == [
        wiki.Hit("AGENTS.md", 6, "网络地址"),
        wiki.Hit("docs/roles.md", 3, "手机号"),
    ]
    # 人写的部分改了、编译维护的那段没变：不查。
    edited = {**after, "AGENTS.md": after["AGENTS.md"].replace("人写的", "人改的")}
    assert wiki.check(after, edited) == []


def test_cited_and_summary() -> None:
    tree = {"docs/a.md": "一（T-1，K-7）二（名录，K-12）", "docs/b.md": "RK-9 不算，K-x 也不算"}
    assert wiki.cited(tree) == {7, 12}
    assert wiki.summary("做完了\n<摘要>改了 docs/a.md：补了退款职责</摘要>") == (
        "改了 docs/a.md：补了退款职责"
    )
    assert wiki.summary("没按格式写") == "没按格式写"


def test_source_label_uses_names_and_falls_back_to_keys() -> None:
    def item(kind: str, ref: str) -> SimpleNamespace:
        return SimpleNamespace(source_kind=kind, source_ref=ref)

    names = {"rules:alpha": "订单助手", "catalog:orders": "订单系统"}
    assert wiki.source_label(item("task", "T-128"), names) == "T-128"  # type: ignore[arg-type]
    assert (
        wiki.source_label(item("rules", "rules:alpha/docs/a.md"), names)  # type: ignore[arg-type]
        == "规则文档 订单助手"
    )
    assert wiki.source_label(item("catalog", "catalog:orders"), names) == "系统目录 订单系统"  # type: ignore[arg-type]
    assert wiki.source_label(item("rules", "rules:beta/AGENTS.md"), names) == "规则文档 beta"  # type: ignore[arg-type]
    assert wiki.source_label(item("catalog", "catalog:crm"), {}) == "系统目录 crm"  # type: ignore[arg-type]
    assert wiki.source_label(item("roster", "roster"), names) == "名录"  # type: ignore[arg-type]


def config(hour: int = 2) -> runner.Config:
    return runner.Config(
        bot_id=None,
        enabled=True,
        quota=dict(rounds.DEFAULT_QUOTA),
        hour=hour,
        timezone="Asia/Shanghai",
        cold_start=False,
    )


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """北京时间的某一刻。"""
    return datetime(
        2026, 11, day, hour, minute, tzinfo=runner.ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)


def test_due_once_a_day_after_the_hour() -> None:
    assert runner.due(config(), {}, at(28, 1)) is True
    ran = {"at": at(28, 2, 5).isoformat(), "status": "ok"}
    assert runner.due(config(), ran, at(28, 9)) is False
    assert runner.due(config(), ran, at(29, 1, 59)) is False
    assert runner.due(config(), ran, at(29, 2, 1)) is True


def test_due_retries_hourly_within_the_window_after_a_failed_run() -> None:
    failed = {"at": at(28, 2, 5).isoformat(), "status": "skipped"}
    assert runner.due(config(), failed, at(28, 2, 30)) is False
    assert runner.due(config(), failed, at(28, 3, 10)) is True
    assert runner.due(config(), {**failed, "at": at(28, 8, 5).isoformat()}, at(28, 9, 10)) is False


def test_quota_ignores_windows_that_already_reset() -> None:
    now = at(28, 3)

    def relay(seven: int, five: int, resets: datetime) -> SimpleNamespace:
        return SimpleNamespace(
            rate_limit_7d_used_pct=Decimal(seven),
            rate_limit_7d_resets_at=resets,
            rate_limit_5h_used_pct=Decimal(five),
            rate_limit_5h_resets_at=resets,
        )

    later, earlier = at(29, 3), at(27, 3)
    quota = dict(rounds.DEFAULT_QUOTA)
    assert rounds.within_quota(relay(69, 79, later), quota, now) is True  # type: ignore[arg-type]
    assert rounds.within_quota(relay(70, 10, later), quota, now) is False  # type: ignore[arg-type]
    assert rounds.within_quota(relay(10, 80, later), quota, now) is False  # type: ignore[arg-type]
    assert rounds.within_quota(relay(99, 99, earlier), quota, now) is True  # type: ignore[arg-type]
