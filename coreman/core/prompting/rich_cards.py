"""飞书富卡片输出：告诉模型可以在回复里写 ```card:类型``` 块，由平台渲染成卡片组件。

只在飞书机器人且开着富卡片时挂进 system prompt 的「本轮附加能力」段；块的字段以
`coreman/core/richtext/schema.py` 为准，这里改字段要同步那边。
"""

from __future__ import annotations

RICH_CARDS_PROMPT = """# Rich Card Output

Your reply is rendered as a Feishu card. Write normal Markdown; when structured content helps \
the reader, add a fenced block whose info string is `card:<type>` and whose body is one JSON \
object. Give data and meaning only — the platform decides colors, sizes and layout, and adapts \
to light and dark themes. Use blocks when they make the answer clearer, not for decoration; a \
short plain answer needs none.

Types:
- header: {"title", "subtitle"?, "tags"?: [{"text", "color"?}], \
"color"?: blue|green|orange|red|grey|turquoise} — card title bar; green=done, \
orange=warning, red=problem. At most one, put it first.
- kpi: {"items": [{"label", "value", "delta"?, "good"?: true|false, "note"?}], \
"size"?: full|half} — metric tiles. `good` says whether the change is good (green) or bad \
(red); arrows follow the sign of `delta`.
- chart: {"chart": line|area|bar|hbar|pie|donut|funnel|combo|progress|ring|radar|scatter, \
"title"?, then data: "x": [labels] + "series": [{"name", "values": [numbers or null], \
"kind"?: bar|line, "axis"?: left|right}] for line/area/bar/combo/radar; \
"items": [{"name", "value"}] for pie/donut/funnel/hbar/progress/ring; \
"points": [{"x", "y", "size"?, "group"?}] for scatter. Optional: "format": {"unit", "prefix", \
"decimals", "scale": auto|none|wan|yi, "percent_input": ratio|percent}, \
"labels": auto|none|all|last|max, "highlight": last|max|[names], \
"stack": none|stack|percent, "top_n", "center": {"value", "caption"}, "summary", \
"size": full|half}.
- table: {"columns": [{"key", "title", \
"type"?: text|number|money|percent|date|person|tag|markdown, "align"?}], \
"rows": [{key: value}], "title"?} — only when you need column types; plain Markdown tables \
are converted automatically. `person` cells hold an email or login name (or a list of them).
- callout: {"level": info|success|warning|danger, "title"?, "text"} — a highlighted note or \
warning.
- item: {"title", "image"?: https URL, "eyebrow"?, "meta"?: [strings], "code"?, \
"highlight"?: {"text", "level"}, "tags"?, "link"?} — one entity (product, order, person) with \
picture and details.
- timeline: {"steps": [{"time"?, "text", "status": done|current|warning|error|pending}]}.
- columns: {"columns": [[child blocks], [child blocks]]} — 2–3 side-by-side columns; a child \
is {"type": "markdown", "text"} or any of kpi/chart/callout/item/timeline/people/note with a \
"type" field.
- people: {"users": [emails or login names], "title"?} — colleagues shown with avatar and \
name; nobody is notified. Names that cannot be matched are shown as plain text.
- note: {"text"} — footnote, data source or caveat.
- actions: {"buttons": [{"text": "…", "url": "…"} or {"text": "…", "reply": true}, ...], \
each with "style"?: primary|danger|default} — up to 6 buttons, text at most 40 characters. \
`url` opens an http/https link. A `"reply": true` button sends its own text to you as the \
asker's next message, so write the text as a complete short request (e.g. "Break down by \
category"); only the person who asked this question can click it. Use them for likely \
follow-ups, never to confirm an irreversible action.

Rules:
- Each block's JSON must be complete and valid; write it in one go and do not edit it \
afterwards.
- State the conclusion in text first, then add the block that supports it.
- Two adjacent blocks with "size": "half" (chart, kpi, callout) are placed side by side.
- Put numbers in `values`/`value` as numbers; format labels via `format` instead of strings.
- Inline colors: `<font color='green|red|orange|blue|grey'>text</font>`, only to mark \
good/bad/status.

Example:
```card:kpi
{"items": [{"label": "Orders", "value": "97", "delta": "+12.6%", "good": true}, \
{"label": "Refund rate", "value": "2.1%", "delta": "+0.4pt", "good": false}]}
```
```card:chart
{"chart": "line", "title": "GMV, last 7 days (k$)", "x": ["9/19", "9/20", "9/21"], \
"series": [{"name": "GMV", "values": [214, 231, 298]}], "labels": "last"}
```"""


def rich_cards_prompt(platform: str, enabled: bool) -> str:
    """飞书且开启富卡片时返回说明段，否则空串（调用方直接拼进 extra）。"""
    return RICH_CARDS_PROMPT if platform == "feishu" and enabled else ""


def with_rich_cards(extra: str, platform: str, enabled: bool) -> str:
    """把说明段接在已有的附加能力后面。"""
    segment = rich_cards_prompt(platform, enabled)
    if not segment:
        return extra
    return f"{extra}\n\n{segment}" if extra.strip() else segment


# 只有 codex 驱动会把图片随流带回来（生图产物、正文里链接的本地图片），见 relay-codex/images.go。
REPLY_IMAGES_PROMPT = """# Images in Your Reply

Images you create with the image generation tool are attached to your reply automatically, \
after your text; never say you cannot deliver them and never paste their file paths. To show \
another image file from your working directory, put `![short caption](relative/path.png)` on \
its own line; it is replaced by the image itself."""


def with_reply_images(extra: str, platform: str, backend: str) -> str:
    """飞书机器人走 codex 时，把回复图片的说明段接在附加能力后面。"""
    if platform != "feishu" or backend != "codex":
        return extra
    return f"{extra}\n\n{REPLY_IMAGES_PROMPT}" if extra.strip() else REPLY_IMAGES_PROMPT
