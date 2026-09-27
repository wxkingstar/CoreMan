"""飞书卡片 JSON 2.0 的字段白名单：裁剪 raw 块，发送前自检整张卡。

2.0 遇到未知字段直接报错、没有 fallback，所以：
- `clean_elements` 给 `raw` 块用：按组件的字段表递归裁剪，裁不好的整个丢掉，只留下一定能发的；
  表单与所有需要回调的组件一律丢掉（回调处理不在这一层），跳转只留 http / https。
- `validate_card` 给测试和编译器自检用：同一套字段表，只报告问题、不改卡片；它允许回调、表单
  和输入类组件，因为按钮回复与选择卡片是 CoreMan 自己生成的。

字段表依据飞书开放平台「卡片 JSON 2.0 组件」各页（2026-09 核对）；个别文档自相矛盾的字段
按宽松处理，见各常量上的注释。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from coreman.core.feishu_cards import style

# ---------------------------------------------------------------- 上限

MAX_ELEMENTS = 200
MAX_DEPTH = 5
MAX_TABLES = 5
MAX_CHARTS = 5
MAX_BYTES = 30_000
MAX_TABLE_COLUMNS = 50

# ---------------------------------------------------------------- 颜色枚举

_HUES = (
    "blue",
    "wathet",
    "turquoise",
    "green",
    "lime",
    "yellow",
    "sunflower",
    "orange",
    "red",
    "carmine",
    "violet",
    "purple",
    "indigo",
)
_HUE_STEPS = ("50", "100", "200", "300", "350", "400", "500", "600", "700", "800", "900")
_GREY_STEPS = (
    "00",
    "50",
    "100",
    "200",
    "300",
    "350",
    "400",
    "500",
    "600",
    "650",
    "700",
    "800",
    "900",
    "950",
    "1000",
)
# 色相名、色相-色阶、grey-00…grey-1000、bg-white、default。`white` 文档没给色值，不收。
COLOR_NAMES: frozenset[str] = frozenset(
    {*_HUES, "grey", "bg-white", "default"}
    | {f"{h}-{s}" for h in _HUES for s in _HUE_STEPS}
    | {f"grey-{s}" for s in _GREY_STEPS}
)

_ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")
_FONT_COLOR = re.compile(r"""(<font\s+color\s*=\s*)(['"]?)([^'"\s>]*)\2""", re.IGNORECASE)

# ---------------------------------------------------------------- 字段表
#
# 每个组件：字段名 → 取值种类。种类决定怎么递归检查：
#   scalar   字符串 / 数字 / 布尔，原样保留
#   json     任意 JSON（表格行、按钮 value 等），原样保留、不深度校验
#   spec     chart_spec：必须是对象，内容原样保留、不深度校验
#   id       element_id
#   color    颜色枚举名或已声明的自定义色
#   text     plain_text / lark_md 文本对象
#   icon     标准图标或自定义图标
#   其余种类见 `_Walker._value`。

_COMMON = {"tag": "tag", "element_id": "id", "margin": "scalar"}
_LAYOUT = {
    "direction": "scalar",
    "padding": "scalar",
    "horizontal_spacing": "scalar",
    "vertical_spacing": "scalar",
    "horizontal_align": "scalar",
    "vertical_align": "scalar",
}
_INTERACT = {
    "behaviors": "behaviors",
    "confirm": "confirm",
    "hover_tips": "text",
    "disabled": "scalar",
    "disabled_tips": "text",
}

# 展示与容器组件：raw 块里可用。
DISPLAY_TAGS: dict[str, dict[str, str]] = {
    "markdown": {
        **_COMMON,
        "content": "markdown",
        "text_size": "scalar",
        "text_align": "scalar",
        "icon": "icon",
    },
    "div": {**_COMMON, "width": "scalar", "text": "text", "icon": "icon"},
    "img": {
        **_COMMON,
        "img_key": "scalar",
        "alt": "text",
        "title": "text",
        "corner_radius": "scalar",
        "scale_type": "scalar",
        "size": "scalar",
        "transparent": "scalar",
        "preview": "scalar",
    },
    "img_combination": {
        **_COMMON,
        "combination_mode": "scalar",
        "combination_transparent": "scalar",
        "corner_radius": "scalar",
        "img_list": "img_list",
    },
    "hr": dict(_COMMON),
    "person": {
        **_COMMON,
        "user_id": "scalar",
        "size": "scalar",
        "show_avatar": "scalar",
        "show_name": "scalar",
        "style": "scalar",
    },
    "person_list": {
        **_COMMON,
        "persons": "persons",
        "drop_invalid_user_id": "scalar",
        "lines": "scalar",
        "show_name": "scalar",
        "show_avatar": "scalar",
        "size": "scalar",
        "icon": "icon",
    },
    "chart": {
        **_COMMON,
        "aspect_ratio": "scalar",
        "color_theme": "scalar",
        "chart_spec": "spec",
        "preview": "scalar",
        "height": "scalar",
    },
    "table": {
        **_COMMON,
        "page_size": "scalar",
        "row_height": "scalar",
        "row_max_height": "scalar",
        "freeze_first_column": "scalar",
        "header_style": "table_header",
        "columns": "table_columns",
        "rows": "table_rows",
    },
    "column_set": {
        **_COMMON,
        "columns": "columns",
        "flex_mode": "scalar",
        "horizontal_spacing": "scalar",
        "horizontal_align": "scalar",
        "background_style": "color",
        "action": "action",
    },
    "column": {
        **_COMMON,
        **_LAYOUT,
        "width": "scalar",
        "weight": "scalar",
        "background_style": "color",
        "elements": "elements",
        "action": "action",
    },
    "interactive_container": {
        **_COMMON,
        **_LAYOUT,
        **_INTERACT,
        "elements": "elements",
        "width": "scalar",
        "height": "scalar",
        # 除颜色外还可以是 `laser`（镭射渐变）。
        "background_style": "container_bg",
        "has_border": "scalar",
        "border_color": "color",
        "corner_radius": "scalar",
    },
    "collapsible_panel": {
        **_COMMON,
        **_LAYOUT,
        "header": "panel_header",
        "expanded": "scalar",
        "background_color": "color",
        "border": "border",
        "elements": "elements",
    },
    "button": {
        **_COMMON,
        **_INTERACT,
        "type": "scalar",
        "size": "scalar",
        "width": "scalar",
        "text": "text",
        "icon": "icon",
    },
    "overflow": {
        **_COMMON,
        "width": "scalar",
        "options": "overflow_options",
        "confirm": "confirm",
    },
}

_FORM_ITEM = {"name": "scalar", "required": "scalar", "width": "scalar"}
_PICKER = {**_COMMON, **_INTERACT, **_FORM_ITEM, "placeholder": "text", "value": "json"}
# 表单与输入类组件：要靠回调才有用。raw 块里一律丢掉；validate_card 认它们，因为选择卡片是
# CoreMan 自己生成的。
INTERACTIVE_TAGS: dict[str, dict[str, str]] = {
    "form": {**_COMMON, **_LAYOUT, "name": "scalar", "elements": "elements"},
    "input": {
        **_COMMON,
        **_INTERACT,
        **_FORM_ITEM,
        "placeholder": "text",
        "default_value": "scalar",
        "max_length": "scalar",
        "input_type": "scalar",
        "show_icon": "scalar",
        "rows": "scalar",
        "auto_resize": "scalar",
        "max_rows": "scalar",
        "label": "text",
        "label_position": "scalar",
        "value": "json",
    },
    "select_static": {
        **_COMMON,
        **_INTERACT,
        **_FORM_ITEM,
        "type": "scalar",
        "placeholder": "text",
        "initial_option": "scalar",
        "initial_index": "scalar",
        "options": "select_options",
    },
    "multi_select_static": {
        **_COMMON,
        **_INTERACT,
        **_FORM_ITEM,
        "type": "scalar",
        "placeholder": "text",
        "selected_values": "json",
        "options": "select_options",
    },
    "select_person": {
        **_COMMON,
        **_INTERACT,
        **_FORM_ITEM,
        "type": "scalar",
        "placeholder": "text",
        "initial_option": "scalar",
        "options": "json",
    },
    "multi_select_person": {
        **_COMMON,
        **_INTERACT,
        **_FORM_ITEM,
        "type": "scalar",
        "placeholder": "text",
        "selected_values": "json",
        "options": "json",
    },
    "date_picker": {**_PICKER, "initial_date": "scalar"},
    "picker_time": {**_PICKER, "initial_time": "scalar"},
    "picker_datetime": {**_PICKER, "initial_datetime": "scalar"},
    "checker": {
        **_COMMON,
        **_INTERACT,
        "name": "scalar",
        "checked": "scalar",
        "text": "text",
        "overall_checkable": "scalar",
        "button_area": "json",
        "checked_style": "json",
        "padding": "scalar",
    },
    "select_img": {
        **_COMMON,
        **_INTERACT,
        **_FORM_ITEM,
        "multi_select": "scalar",
        "layout": "scalar",
        "can_preview": "scalar",
        "aspect_ratio": "scalar",
        "value": "json",
        "options": "json",
    },
}
# 表单里的按钮多两个字段。
_FORM_BUTTON = {"name": "scalar", "form_action_type": "scalar"}

# 缺了就不成立的字段。
REQUIRED: dict[str, tuple[str, ...]] = {
    "markdown": ("content",),
    "img": ("img_key", "alt"),
    "img_combination": ("combination_mode", "img_list"),
    "person": ("user_id",),
    "person_list": ("persons",),
    "chart": ("chart_spec",),
    "table": ("columns",),
    "column_set": ("columns",),
    "interactive_container": ("behaviors", "elements"),
    "button": ("behaviors",),
    "overflow": ("options",),
    "form": ("name", "elements"),
}
CONTAINERS: frozenset[str] = frozenset(
    {"column_set", "column", "interactive_container", "collapsible_panel", "form"}
)
# 只能放在卡片根节点。
ROOT_ONLY: frozenset[str] = frozenset({"table", "form"})

_TEXT_FIELDS = {
    "tag": "tag",
    "content": "scalar",
    "text_size": "scalar",
    "text_color": "color",
    "text_align": "scalar",
    "lines": "scalar",
    "element_id": "id",
    "i18n_content": "json",
}
_ICON_FIELDS = {"tag": "tag", "token": "scalar", "color": "color", "img_key": "scalar"}
_PANEL_HEADER = {
    "title": "panel_title",
    "background_color": "color",
    "width": "scalar",
    "vertical_align": "scalar",
    "padding": "scalar",
    "icon": "panel_icon",
    "icon_position": "scalar",
    "icon_expanded_angle": "scalar",
}
_TABLE_HEADER = {
    "text_align": "scalar",
    "text_size": "scalar",
    "background_style": "scalar",
    "text_color": "scalar",
    "bold": "scalar",
    "lines": "scalar",
}
_TABLE_HEADER_ENUMS = {
    "background_style": frozenset({"none", "grey"}),
    "text_color": frozenset({"default", "grey"}),
    "text_size": frozenset({"normal", "heading"}),
}
_TABLE_COLUMN = {
    "name": "scalar",
    "display_name": "scalar",
    "width": "scalar",
    "vertical_align": "scalar",
    "horizontal_align": "scalar",
    "data_type": "scalar",
    "format": "table_format",
    "date_format": "scalar",
}
TABLE_DATA_TYPES: frozenset[str] = frozenset(
    {"text", "lark_md", "markdown", "options", "number", "persons", "date"}
)
_URL_KEYS = ("url", "pc_url", "ios_url", "android_url")
_OPEN_URL_KEYS = ("default_url", "pc_url", "ios_url", "android_url")

_CARD_FIELDS = frozenset({"schema", "config", "card_link", "header", "body"})
_CONFIG_FIELDS = frozenset(
    {
        "streaming_mode",
        "streaming_config",
        "summary",
        "locales",
        "enable_forward",
        "update_multi",
        "width_mode",
        "use_custom_translation",
        "enable_forward_interaction",
        "style",
    }
)
_BODY_FIELDS = frozenset({"elements", *_LAYOUT})
_HEADER_FIELDS = {
    "title": "text",
    "subtitle": "text",
    "text_tag_list": "text_tags",
    "template": "scalar",
    "icon": "icon",
    "padding": "scalar",
}


def is_http_url(value: Any) -> bool:
    """只认 http / https 且带主机名的链接。"""
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def valid_element_id(value: Any) -> bool:
    """字母开头、只含字母数字下划线、≤20 字符。"""
    return isinstance(value, str) and bool(_ELEMENT_ID.match(value))


# ---------------------------------------------------------------- 遍历


@dataclass
class _Walker:
    """按字段表递归处理组件树。

    strict=True（raw 块）：丢掉表单、输入类组件和回调；否则（自检）都认。
    每处不合规都记进 problems，同时返回裁剪后的结果；自检只看 problems。
    """

    strict: bool
    custom_colors: frozenset[str]
    problems: list[str] = field(default_factory=list)

    def note(self, path: str, message: str) -> None:
        self.problems.append(f"{path}: {message}")

    # ------------------------------------------------ 组件

    def elements(self, items: Any, path: str, depth: int, *, root: bool) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            self.note(path, "应为组件数组")
            return []
        out: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            node = self.component(item, f"{path}[{i}]", depth, root=root)
            if node is not None:
                out.append(node)
        return out

    def _spec(self, tag: Any) -> dict[str, str] | None:
        if not isinstance(tag, str):
            return None
        spec = DISPLAY_TAGS.get(tag)
        if spec is None and not self.strict:
            spec = INTERACTIVE_TAGS.get(tag)
        if spec is not None and tag == "button" and not self.strict:
            spec = {**spec, **_FORM_BUTTON}
        return spec

    def component(self, node: Any, path: str, depth: int, *, root: bool) -> dict[str, Any] | None:
        if not isinstance(node, dict):
            self.note(path, "组件应为对象")
            return None
        tag = node.get("tag")
        spec = self._spec(tag)
        if spec is None:
            self.note(path, f"不支持的组件 {tag!r}")
            return None
        path = f"{path}<{tag}>"
        if tag in ROOT_ONLY and not root:
            self.note(path, f"{tag} 只能放在卡片根节点")
            return None
        if tag in CONTAINERS:
            depth += 1
            if depth > MAX_DEPTH:
                self.note(path, f"容器嵌套超过 {MAX_DEPTH} 层")
                return None
        out: dict[str, Any] = {}
        for key, value in node.items():
            kind = spec.get(key)
            if kind is None:
                self.note(path, f"未知字段 {key!r}")
                continue
            if kind == "tag":
                out[key] = value
                continue
            cleaned = self._value(kind, value, f"{path}.{key}", depth)
            if cleaned is not _DROP:
                out[key] = cleaned
        return self._finish(str(tag), out, path)

    def _finish(self, tag: str, out: dict[str, Any], path: str) -> dict[str, Any] | None:
        """补默认值、检查必填；缺了必填字段的组件整个丢掉。"""
        if self.strict:
            if tag == "img" and "img_key" in out and "alt" not in out:
                out["alt"] = {"tag": "plain_text", "content": ""}
            if tag == "interactive_container":
                out.setdefault("behaviors", [])
                out.setdefault("elements", [])
        missing = [k for k in REQUIRED.get(tag, ()) if k not in out]
        if tag == "button" and self.strict and not out.get("behaviors"):
            missing = missing or ["behaviors"]
        if tag in {"column_set", "img_combination", "person_list", "overflow"}:
            key = {
                "column_set": "columns",
                "img_combination": "img_list",
                "person_list": "persons",
                "overflow": "options",
            }[tag]
            if key in out and not out[key]:
                missing.append(key)
        if missing:
            self.note(path, f"缺少必填字段 {', '.join(missing)}")
            return None
        return out

    # ------------------------------------------------ 字段值

    def _value(self, kind: str, value: Any, path: str, depth: int) -> Any:
        if kind == "scalar":
            if isinstance(value, (str, int, float, bool)):
                return value
            self.note(path, "应为字符串、数字或布尔值")
            return _DROP
        if kind == "json":
            return value
        if kind == "spec":
            if isinstance(value, dict):
                return value
            self.note(path, "应为对象")
            return _DROP
        if kind == "id":
            if valid_element_id(value):
                return value
            self.note(path, f"element_id 不合规 {value!r}")
            return _DROP
        if kind == "color":
            return value if self.color(value, path) else _DROP
        if kind == "container_bg":
            return value if value == "laser" or self.color(value, path) else _DROP
        if kind == "markdown":
            return self.markdown(value, path)
        if kind == "text":
            return self.text(value, path, {"plain_text", "lark_md"})
        if kind == "panel_title":
            return self.text(value, path, {"plain_text", "markdown"})
        if kind == "icon":
            return self.icon(value, path, sized=False)
        if kind == "panel_icon":
            return self.icon(value, path, sized=True)
        if kind == "elements":
            return self.elements(value, path, depth, root=False)
        if kind == "columns":
            return self.columns(value, path, depth)
        if kind == "behaviors":
            return self.behaviors(value, path)
        return _OBJECT_KINDS[kind](self, value, path)

    def color(self, value: Any, path: str) -> bool:
        if isinstance(value, str) and (value in COLOR_NAMES or value in self.custom_colors):
            return True
        self.note(path, f"颜色只能用枚举名或已声明的自定义色，不能是 {value!r}")
        return False

    def markdown(self, value: Any, path: str) -> Any:
        if not isinstance(value, str):
            self.note(path, "应为字符串")
            return _DROP

        def fix(match: re.Match[str]) -> str:
            color = match.group(3)
            if color in COLOR_NAMES or color in self.custom_colors:
                return match.group(0)
            self.note(path, f"<font color> 不能用 {color!r}")
            return f"{match.group(1)}'default'"

        return _FONT_COLOR.sub(fix, value)

    def text(self, value: Any, path: str, tags: set[str]) -> Any:
        if not isinstance(value, dict) or value.get("tag") not in tags:
            self.note(path, f"文本对象的 tag 应为 {'/'.join(sorted(tags))}")
            return _DROP
        out = self.fields(value, _TEXT_FIELDS, path)
        if value.get("tag") != "plain_text" and "content" in out:
            out["content"] = self.markdown(out["content"], f"{path}.content")
        return out

    def icon(self, value: Any, path: str, *, sized: bool) -> Any:
        fields = {**_ICON_FIELDS, "size": "scalar"} if sized else _ICON_FIELDS
        if not isinstance(value, dict):
            self.note(path, "图标应为对象")
            return _DROP
        tag = value.get("tag")
        if tag == "standard_icon":
            if value.get("token") not in style.icon_tokens():
                self.note(path, f"图标 {value.get('token')!r} 不在图标库里")
                return _DROP
        elif tag == "custom_icon":
            if not isinstance(value.get("img_key"), str):
                self.note(path, "自定义图标缺少 img_key")
                return _DROP
        else:
            self.note(path, f"图标 tag 不能是 {tag!r}")
            return _DROP
        return self.fields(value, fields, path)

    def fields(self, value: dict[str, Any], spec: dict[str, str], path: str) -> dict[str, Any]:
        """普通子对象：按字段表逐项检查（不含子组件）。"""
        out: dict[str, Any] = {}
        for key, item in value.items():
            kind = spec.get(key)
            if kind is None:
                self.note(path, f"未知字段 {key!r}")
                continue
            if kind == "tag":
                out[key] = item
                continue
            cleaned = self._value(kind, item, f"{path}.{key}", 0)
            if cleaned is not _DROP:
                out[key] = cleaned
        return out

    def columns(self, value: Any, path: str, depth: int) -> Any:
        if not isinstance(value, list):
            self.note(path, "应为 column 数组")
            return _DROP
        out: list[dict[str, Any]] = []
        for i, item in enumerate(value):
            if not isinstance(item, dict) or item.get("tag") != "column":
                self.note(f"{path}[{i}]", "column_set 里只能放 column")
                continue
            node = self.component(item, f"{path}[{i}]", depth, root=False)
            if node is not None:
                out.append(node)
        return out

    def behaviors(self, value: Any, path: str) -> Any:
        if not isinstance(value, list):
            self.note(path, "应为数组")
            return _DROP
        out: list[dict[str, Any]] = []
        for i, item in enumerate(value):
            where = f"{path}[{i}]"
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "open_url" and isinstance(item, dict):
                urls = {}
                for key, url in item.items():
                    if key == "type":
                        continue
                    if key not in _OPEN_URL_KEYS:
                        self.note(where, f"未知字段 {key!r}")
                    elif is_http_url(url):
                        urls[key] = url
                    else:
                        self.note(f"{where}.{key}", "只接受 http / https 链接")
                if "default_url" not in urls:
                    self.note(where, "open_url 缺少有效的 default_url")
                    continue
                out.append({"type": "open_url", **urls})
            elif kind == "callback" and isinstance(item, dict) and not self.strict:
                extra = set(item) - {"type", "value"}
                if extra:
                    self.note(where, f"未知字段 {', '.join(sorted(extra))}")
                if not isinstance(item.get("value"), dict):
                    self.note(where, "callback 的 value 应为对象")
                    continue
                out.append({"type": "callback", "value": item["value"]})
            else:
                self.note(where, f"不支持的交互 {kind!r}")
        return out

    def confirm(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            self.note(path, "应为对象")
            return _DROP
        out = self.fields(value, {"title": "text", "text": "text"}, path)
        if "title" not in out or "text" not in out:
            self.note(path, "confirm 需要 title 和 text")
            return _DROP
        return out

    def multi_url(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            self.note(path, "应为对象")
            return _DROP
        out: dict[str, str] = {}
        for key, url in value.items():
            if key not in _URL_KEYS:
                self.note(path, f"未知字段 {key!r}")
            elif is_http_url(url):
                out[key] = url
            else:
                self.note(f"{path}.{key}", "只接受 http / https 链接")
        if "url" not in out:
            self.note(path, "缺少有效的 url")
            return _DROP
        return out

    def action(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            self.note(path, "应为对象")
            return _DROP
        extra = set(value) - {"multi_url"}
        if extra:
            self.note(path, f"未知字段 {', '.join(sorted(extra))}")
        urls = self.multi_url(value.get("multi_url"), f"{path}.multi_url")
        return _DROP if urls is _DROP else {"multi_url": urls}

    def items(self, value: Any, path: str, spec: dict[str, str], required: str) -> Any:
        """对象数组（img_list、persons 等）：每项按字段表检查，缺 required 的项丢掉。"""
        if not isinstance(value, list):
            self.note(path, "应为数组")
            return _DROP
        out: list[dict[str, Any]] = []
        for i, item in enumerate(value):
            where = f"{path}[{i}]"
            if not isinstance(item, dict):
                self.note(where, "应为对象")
                continue
            cleaned = self.fields(item, spec, where)
            if required not in cleaned:
                self.note(where, f"缺少 {required}")
                continue
            out.append(cleaned)
        return out

    def overflow_options(self, value: Any, path: str) -> Any:
        spec = {"text": "text", "multi_url": "multi_url", "value": "json"}
        if not self.strict:
            return self.items(value, path, spec, "text")
        # raw 块不接回调：只留能跳转的选项，value 去掉。
        options = self.items(value, path, spec, "multi_url")
        if not isinstance(options, list):
            return options
        for option in options:
            if option.pop("value", None) is not None:
                self.note(path, "选项的 value 需要回调，已去掉")
        return options

    def select_options(self, value: Any, path: str) -> Any:
        return self.items(value, path, {"text": "text", "icon": "icon", "value": "scalar"}, "value")

    def text_tags(self, value: Any, path: str) -> Any:
        spec = {"tag": "tag", "element_id": "id", "text": "text", "color": "scalar"}
        tags = self.items(value, path, spec, "text")
        if not isinstance(tags, list):
            return tags
        for tag in tags:
            if tag.get("tag") != "text_tag":
                self.note(path, "text_tag_list 的项 tag 应为 text_tag")
            if "color" in tag and tag["color"] not in style.TAG_COLORS:
                self.note(path, f"标签颜色不能是 {tag['color']!r}")
                tag.pop("color")
        return tags

    def table_header(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            self.note(path, "应为对象")
            return _DROP
        out = self.fields(value, _TABLE_HEADER, path)
        for key, allowed in _TABLE_HEADER_ENUMS.items():
            if key in out and out[key] not in allowed:
                self.note(f"{path}.{key}", f"只能是 {'/'.join(sorted(allowed))}")
                out.pop(key)
        return out

    def table_columns(self, value: Any, path: str) -> Any:
        columns = self.items(value, path, _TABLE_COLUMN, "name")
        if not isinstance(columns, list):
            return columns
        for column in columns:
            if column.get("data_type", "text") not in TABLE_DATA_TYPES:
                self.note(path, f"不支持的列类型 {column.get('data_type')!r}")
                column["data_type"] = "text"
        if len(columns) > MAX_TABLE_COLUMNS:
            self.note(path, f"表格超过 {MAX_TABLE_COLUMNS} 列")
            columns = columns[:MAX_TABLE_COLUMNS]
        return columns or _DROP

    def table_format(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            self.note(path, "应为对象")
            return _DROP
        spec = {"precision": "scalar", "symbol": "scalar", "separator": "scalar"}
        return self.fields(value, spec, path)

    def table_rows(self, value: Any, path: str) -> Any:
        if not isinstance(value, list):
            self.note(path, "应为数组")
            return _DROP
        rows = [row for row in value if isinstance(row, dict)]
        if len(rows) != len(value):
            self.note(path, "每一行应为对象")
        return rows

    def panel_header(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            self.note(path, "应为对象")
            return _DROP
        return self.fields(value, _PANEL_HEADER, path)

    def border(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict):
            self.note(path, "应为对象")
            return _DROP
        return self.fields(value, {"color": "color", "corner_radius": "scalar"}, path)


class _Drop:
    """字段值被整项去掉的标记。"""


_DROP: Any = _Drop()


def _img_list(walker: _Walker, value: Any, path: str) -> Any:
    return walker.items(value, path, {"img_key": "scalar", "transparent": "scalar"}, "img_key")


def _persons(walker: _Walker, value: Any, path: str) -> Any:
    return walker.items(value, path, {"id": "scalar"}, "id")


_OBJECT_KINDS: dict[str, Any] = {
    "confirm": _Walker.confirm,
    "multi_url": _Walker.multi_url,
    "action": _Walker.action,
    "overflow_options": _Walker.overflow_options,
    "select_options": _Walker.select_options,
    "text_tags": _Walker.text_tags,
    "table_header": _Walker.table_header,
    "table_columns": _Walker.table_columns,
    "table_format": _Walker.table_format,
    "table_rows": _Walker.table_rows,
    "panel_header": _Walker.panel_header,
    "border": _Walker.border,
    "img_list": _img_list,
    "persons": _persons,
}


# ---------------------------------------------------------------- 对外


def clean_elements(elements: list[Any], *, root: bool = True) -> list[dict[str, Any]]:
    """把 raw 块的组件裁剪成一定能发送的样子。

    不认识的组件、表单、输入类组件整个丢掉；未知字段、非 http(s) 跳转、回调、写死的颜色、
    图库外的图标、不合规的 element_id 去掉；缺了必填字段的组件整个丢掉。
    root=False 表示这些组件要放进容器里，表格也会被丢掉。
    """
    walker = _Walker(strict=True, custom_colors=frozenset(style.CUSTOM_COLORS))
    return walker.elements(list(elements), "raw", 0, root=root)


def validate_card(card: dict[str, Any]) -> list[str]:
    """检查整张卡能不能发；返回问题列表，空列表表示合法。"""
    problems: list[str] = []
    if not isinstance(card, dict):
        return ["卡片应为对象"]
    problems += [f"card: 未知字段 {k!r}" for k in card if k not in _CARD_FIELDS]
    if card.get("schema") != "2.0":
        problems.append('card.schema 应为 "2.0"')

    config = card.get("config")
    custom: frozenset[str] = frozenset()
    if not isinstance(config, dict):
        problems.append("card.config 缺失")
    else:
        problems += [f"card.config: 未知字段 {k!r}" for k in config if k not in _CONFIG_FIELDS]
        if config.get("update_multi") is not True:
            problems.append("card.config.update_multi 应为 true")
        custom, color_problems = _custom_colors(config.get("style"))
        problems += color_problems

    walker = _Walker(strict=False, custom_colors=custom)
    header = card.get("header")
    if header is not None:
        _check_header(walker, header)
    body = card.get("body")
    if not isinstance(body, dict):
        problems.append("card.body 缺失")
    else:
        problems += [f"card.body: 未知字段 {k!r}" for k in body if k not in _BODY_FIELDS]
        walker.elements(body.get("elements", []), "body.elements", 0, root=True)
    problems += walker.problems

    nodes = list(_tagged(card))
    tags = [str(n.get("tag")) for n in nodes]
    if len(nodes) > MAX_ELEMENTS:
        problems.append(f"元素 {len(nodes)} 个，超过 {MAX_ELEMENTS}")
    if tags.count("table") > MAX_TABLES:
        problems.append(f"表格 {tags.count('table')} 个，超过 {MAX_TABLES}")
    if tags.count("chart") > MAX_CHARTS:
        problems.append(f"图表 {tags.count('chart')} 个，超过 {MAX_CHARTS}")

    seen: set[str] = set()
    for node in nodes:
        if "element_id" not in node:
            continue
        eid = node["element_id"]
        if not valid_element_id(eid):
            problems.append(f"element_id 不合规 {eid!r}")
        elif eid in seen:
            problems.append(f"element_id 重复 {eid!r}")
        seen.add(str(eid))

    try:
        # NaN / Infinity 不是合法 JSON，飞书会整卡拒收。
        payload = json.dumps(card, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError:
        problems.append("卡片含 NaN 或 Infinity")
        return problems
    size = len(payload.encode("utf-8", "replace"))
    if size > MAX_BYTES:
        problems.append(f"卡片 {size} 字节，超过 {MAX_BYTES}")
    return problems


def _check_header(walker: _Walker, header: Any) -> None:
    if not isinstance(header, dict):
        walker.note("header", "应为对象")
        return
    walker.fields(header, _HEADER_FIELDS, "header")
    if "title" not in header:
        walker.note("header", "缺少 title")
    template = header.get("template")
    if template is not None and template not in style.HEADER_TEMPLATES:
        walker.note("header.template", f"不能是 {template!r}")


def _custom_colors(value: Any) -> tuple[frozenset[str], list[str]]:
    """config.style.color：每个自定义色必须 light / dark 成对，且两个值不同。"""
    if value is None:
        return frozenset(), []
    if not isinstance(value, dict):
        return frozenset(), ["card.config.style 应为对象"]
    problems = [
        f"card.config.style: 未知字段 {k!r}" for k in value if k not in {"color", "text_size"}
    ]
    colors = value.get("color") or {}
    if not isinstance(colors, dict):
        return frozenset(), [*problems, "card.config.style.color 应为对象"]
    names: set[str] = set()
    for name, pair in colors.items():
        where = f"card.config.style.color.{name}"
        if not isinstance(pair, dict) or set(pair) != {"light_mode", "dark_mode"}:
            problems.append(f"{where} 必须且只能有 light_mode 和 dark_mode")
            continue
        if pair["light_mode"] == pair["dark_mode"]:
            problems.append(f"{where} 的 light_mode 与 dark_mode 相同，深色主题下不会变")
            continue
        names.add(str(name))
    return frozenset(names), problems


def _tagged(value: Any) -> Iterable[dict[str, Any]]:
    """卡片里所有带 tag 的节点（组件、文本对象、图标都算，与飞书的元素计数一致）。

    chart_spec 与表格行是数据，不往里数。
    """
    if isinstance(value, dict):
        if "tag" in value:
            yield value
        for key, item in value.items():
            if key in {"chart_spec", "rows"}:
                continue
            yield from _tagged(item)
    elif isinstance(value, list):
        for item in value:
            yield from _tagged(item)
