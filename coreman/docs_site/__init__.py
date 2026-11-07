"""/docs 文档站：pages/ 下的 Markdown 是唯一源，同一份内容给人渲染成网页、给 AI 原样输出。

源文件里的 `{{COREMAN_BASE_URL}}` 换成部署的 PUBLIC_BASE_URL；页面之间用相对链接 `slug.md`，
Markdown 输出时补成绝对地址，网页里改指 `/docs/<slug>`。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

PLACEHOLDER = "{{COREMAN_BASE_URL}}"
PAGES_DIR = Path(__file__).parent / "pages"
SITE_TITLE = "CoreMan 文档"
SITE_SUMMARY = (
    "CoreMan AI 员工平台的使用与接入说明："
    "本机接入业务系统目录、通过 API 驱动 AI 员工、业务系统接入规范。"
)


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    summary: str


PAGES: tuple[Page, ...] = (
    Page("index", "概览", "CoreMan 是什么、文档有哪些主题、给 AI 的入口。"),
    Page(
        "systems-catalog-mcp",
        "本机接入业务系统目录",
        "把业务系统操作目录接成本机 Claude Code、Codex 的 MCP，用自己的令牌直连业务系统。",
    ),
    Page(
        "api-tasks",
        "通过 API 驱动 AI 员工",
        "用个人 API 令牌给飞书 AI 员工下任务、查询结果：接口、状态、错误与示例。",
    ),
    Page(
        "business-system-contract",
        "业务系统 OpenAPI 接入规范",
        "业务系统按这份 OpenAPI 契约提供描述，AI 员工即可按需发现和调用其操作。",
    ),
)
BY_SLUG = {page.slug: page for page in PAGES}
# 文档站之前就公开发布在 /integration/ 下的两份，原地址继续可用。
INTEGRATION_ALIASES = {
    "api-tasks-agent-guide.md": "api-tasks",
    "business-system-openapi-contract.md": "business-system-contract",
}

_PAGE_LINK = re.compile(r"\]\(([a-z0-9-]+)\.md(#[^)\s]*)?\)")
_HREF_PAGE = re.compile(r"^([a-z0-9-]+)\.md(#.*)?$")
_SLUG_DROP = re.compile(r"[^\w㐀-鿿\- ]")


def page_url(slug: str) -> str:
    return "/docs" if slug == "index" else f"/docs/{slug}"


def _anchor(text: str, used: set[str]) -> str:
    base = _SLUG_DROP.sub("", text.strip().lower()).strip().replace(" ", "-") or "section"
    anchor, n = base, 2
    while anchor in used:
        anchor, n = f"{base}-{n}", n + 1
    used.add(anchor)
    return anchor


@dataclass
class Rendered:
    markdown: str
    body: str
    toc: list[tuple[int, str, str]] = field(default_factory=list)


def _render(source: str) -> tuple[str, list[tuple[int, str, str]]]:
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = md.parse(source)
    used: set[str] = set()
    toc: list[tuple[int, str, str]] = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[i + 1]
            text = "".join(
                c.content for c in inline.children or [] if c.type in ("text", "code_inline")
            )
            anchor = _anchor(text, used)
            token.attrSet("id", anchor)
            level = int(token.tag[1])
            if level in (2, 3):
                toc.append((level, text, anchor))
        elif token.type == "inline":
            for child in token.children or []:
                if child.type != "link_open":
                    continue
                href = str(child.attrGet("href") or "")
                match = _HREF_PAGE.match(href)
                if match and match.group(1) in BY_SLUG:
                    child.attrSet("href", page_url(match.group(1)) + (match.group(2) or ""))
                elif href.startswith(("http://", "https://")):
                    child.attrSet("target", "_blank")
                    child.attrSet("rel", "noopener noreferrer")
    return md.renderer.render(tokens, md.options, {}), toc


class DocsSite:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.pages: dict[str, Rendered] = {}
        for page in PAGES:
            source = (PAGES_DIR / f"{page.slug}.md").read_text(encoding="utf-8")
            body, toc = _render(source)
            markdown = _PAGE_LINK.sub(
                lambda m: (
                    f"]({self.base_url}/docs/{m.group(1)}.md{m.group(2) or ''})"
                    if m.group(1) in BY_SLUG
                    else m.group(0)
                ),
                source,
            ).replace(PLACEHOLDER, self.base_url)
            body = body.replace(PLACEHOLDER, html.escape(self.base_url))
            self.pages[page.slug] = Rendered(markdown, body, toc)

    def markdown(self, slug: str) -> str:
        return self.pages[slug].markdown

    def llms_txt(self) -> str:
        lines = [f"# {SITE_TITLE}", "", f"> {SITE_SUMMARY}", ""]
        lines += [
            f"人看的网页在 {self.base_url}/docs；下面每一项都是 Markdown 原文。",
            "",
            "## 文档",
            "",
        ]
        lines += [f"- [{p.title}]({self.base_url}/docs/{p.slug}.md): {p.summary}" for p in PAGES]
        full = f"- [全部页面合一]({self.base_url}/docs/llms-full.txt): 上面所有页面的完整内容"
        lines += ["", "## Optional", "", full, ""]
        return "\n".join(lines)

    def llms_full(self) -> str:
        parts = [f"# {SITE_TITLE}\n\n> {SITE_SUMMARY}\n"]
        for page in PAGES:
            source = f"<!-- 来源：{self.base_url}/docs/{page.slug}.md -->"
            parts.append(f"{source}\n\n{self.markdown(page.slug).strip()}\n")
        return "\n---\n\n".join(parts)

    def html(self, slug: str) -> str:
        page = BY_SLUG[slug]
        rendered = self.pages[slug]
        nav = []
        for p in PAGES:
            active = p.slug == slug
            nav.append(
                f'<a class="page{" active" if active else ""}" href="{page_url(p.slug)}"'
                f"{' aria-current=page' if active else ''}>{html.escape(p.title)}</a>"
            )
            if active and rendered.toc:
                items = "".join(
                    f'<a class="toc l{level}" href="#{html.escape(anchor)}">{html.escape(text)}</a>'
                    for level, text, anchor in rendered.toc
                )
                nav.append(f'<div class="toc-list">{items}</div>')
        title = SITE_TITLE if slug == "index" else f"{page.title} · {SITE_TITLE}"
        values = {
            "__TITLE__": html.escape(title),
            "__DESCRIPTION__": html.escape(page.summary),
            "__MD_URL__": f"/docs/{slug}.md",
            "__NAV__": "".join(nav),
            "__SEARCH_INDEX__": _json_for_script(self._search_index()),
            # 正文最后替换：正文里出现上面的标记也不会被误换。
            "__BODY__": rendered.body,
        }
        out = _TEMPLATE
        for key, value in values.items():
            out = out.replace(key, value)
        return out

    def _search_index(self) -> list[dict[str, Any]]:
        index: list[dict[str, Any]] = []
        for p in PAGES:
            index.append({"t": p.title, "p": "", "u": page_url(p.slug)})
            index += [
                {"t": text, "p": p.title, "u": f"{page_url(p.slug)}#{anchor}"}
                for _, text, anchor in self.pages[p.slug].toc
            ]
        return index


def _json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


@lru_cache(maxsize=4)
def site(base_url: str) -> DocsSite:
    return DocsSite(base_url)


_TEMPLATE = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
