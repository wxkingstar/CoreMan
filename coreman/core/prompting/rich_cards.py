"""飞书富卡片输出：告诉模型可以在回复里写 ```card:类型``` 块，由平台渲染成卡片组件。

只在飞书机器人且开着富卡片时挂进 system prompt 的输出格式段（详细度之后、结尾重申之前）；
块的字段以 `coreman/core/richtext/schema.py` 为准，这里改字段要同步那边。
"""

from __future__ import annotations

RICH_CARDS_PROMPT = """# Rich Card Output

Your reply is rendered as a Feishu card. Write normal Markdown and add card blocks: a fenced \
block with info string `card:<type>` and one JSON object as its body. Readers scan blocks \
faster than paragraphs, so prefer a block over prose for anything a block can show. Give data \
and meaning only; the platform picks colors, sizes and layout for light and dark themes.

When to use them:
- After carrying out a task (changed, created, deployed, fixed, checked), open with a `header` \
stating the outcome: green done, orange done with something pending, red failed.
- What the reader must do or know before relying on the result (restart or approval still \
needed, risk, partial failure, data caveat): a `callout` near the top.
- Several fields of one result (IDs, names, settings, before/after): `table`, or `item` for one \
entity with a picture. Headline numbers: `kpi`. Trends, comparisons, shares: `chart`. Steps or \
history: `timeline`. Colleagues you name: `people`. Likely follow-ups: `actions` reply buttons.
- Plain text only for a reply of one or two sentences, small talk, or a question back.
- When a Response Length section asks for a minimal or brief reply, add a block only if the \
user explicitly asks for a chart, table or similar.

Types:
- header: {"title", "subtitle"?, "tags"?: [{"text", "color"?}], \
"color"?: blue|green|orange|red|grey|turquoise} — at most one, first.
- kpi: {"items": [{"label", "value", "delta"?, "good"?: true|false, "note"?}], \
"size"?: full|half} — `good` says whether the change is good (green) or bad (red); arrows \
follow the sign of `delta`.
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
"rows": [{key: value}], "title"?} — only when you need column types; Markdown tables are \
converted automatically. `person` cells hold an email or login name (or a list).
- callout: {"level": info|success|warning|danger, "title"?, "text"}.
- item: {"title", "image"?: https URL, "eyebrow"?, "meta"?: [strings], "code"?, \
"highlight"?: {"text", "level"}, "tags"?, "link"?} — one product, order or person.
- timeline: {"steps": [{"time"?, "text", "status": done|current|warning|error|pending}]}.
- columns: {"columns": [[child blocks], [child blocks]]} — 2–3 side-by-side columns; a child \
is {"type": "markdown", "text"} or any of kpi/chart/callout/item/timeline/people/note with a \
"type" field.
- people: {"users": [emails or login names], "title"?} — avatar and name; nobody is notified; \
unmatched names stay plain text.
- note: {"text"} — footnote, data source or caveat.
- actions: {"buttons": [{"text": "…", "url": "…"} or {"text": "…", "reply": true}, ...], \
each with "style"?: primary|danger|default} — up to 6 buttons, text at most 40 characters. \
`url` opens an http/https link. A `"reply": true` button sends its own text to you as the \
asker's next message, so write it as a complete short request (e.g. "Break down by \
category"); only the person who asked can click it. Never use one to confirm an irreversible \
action.

Rules:
- The card renders while you stream, so write each block's JSON complete and valid in one go.
- Conclusion first (header or opening sentence), then supporting blocks; do not repeat in \
prose what a block shows.
- Two adjacent "size": "half" blocks (chart, kpi, callout) sit side by side.
- Numbers go in `values`/`value` as numbers; format them with `format`, not strings.
- Inline colors: `<font color='green|red|orange|blue|grey'>text</font>`, only for good/bad/status.

Example:
```card:header
{"title": "Config moved to the correct group", "color": "orange", \
"tags": [{"text": "Restart pending", "color": "orange"}]}
```
```card:callout
{"level": "warning", "text": "order-api reads this config only at startup; restart it to apply."}
```
```card:kpi
{"items": [{"label": "Orders", "value": 97, "delta": "+12.6%", "good": true}], "size": "half"}
```
```card:chart
{"chart": "line", "x": ["9/19", "9/20", "9/21"], \
"series": [{"name": "GMV", "values": [214, 231, 298]}], "labels": "last", "size": "half"}
```"""


def rich_cards_prompt(platform: str, enabled: bool) -> str:
    """飞书且开启富卡片时返回说明段（build_system_prompt 的 output_format），否则空串。"""
    return RICH_CARDS_PROMPT if platform == "feishu" and enabled else ""


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
