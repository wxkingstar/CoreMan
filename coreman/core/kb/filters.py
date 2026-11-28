"""规则过滤：事实条目入库前、Wiki 写完后各过一遍，命中即整条丢弃（或要求删掉那一行）。

这只是兜底。主要的保护是来源排除（`sources`）和抽取规则（`extract`）。丢弃而不是打码：
一条事实去掉关键部分后往往没有意义，留着反而误导。
"""

from __future__ import annotations

import re

from coreman.core.bots.rules_backup import SECRET_ASSIGNMENTS, SECRET_TOKENS

# 规则名写进 knowledge_items.dropped_reason 和运行记录，也会告诉修正回合，所以用人话。
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("密钥", SECRET_TOKENS),
    ("密钥", SECRET_ASSIGNMENTS),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)|\+\d{1,3}[ -]?\d{7,14}(?!\d)")),
    ("证件号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("带参数的链接", re.compile(r"https?://[^\s)）>\]]*\?[^\s)）>\]]+")),
    ("网络地址", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])|\blocalhost\b")),
    (
        "金额",
        re.compile(
            r"[$¥￥€£]\s?\d"
            r"|\d[\d,.]*\s*(?:万|亿)?(?:元|块钱|美元|美金|日元|欧元|英镑|港币)"
            r"|\d[\d,.]*\s*(?:USD|RMB|CNY|JPY|EUR|GBP|HKD)\b"
        ),
    ),
    # 订单号、单号、账号一类；任务编号、条目编号、日期（2026-11-28）都不到 8 位连续数字。
    ("长数字", re.compile(r"(?<![\w.-])\d{8,}(?![\w-])")),
)


def hit(text: str) -> str | None:
    """命中的第一条规则名；都没命中返回 None。"""
    for name, pattern in RULES:
        if pattern.search(text):
            return name
    return None


def scan(text: str) -> list[tuple[int, str]]:
    """逐行检查，返回 [(行号, 规则名)]，行号从 1 起；一行只报第一条命中的规则。"""
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        name = hit(line)
        if name is not None:
            found.append((number, name))
    return found
