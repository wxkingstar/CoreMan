"""Parse an OpenAPI description into plain JSON values.

The document comes from a business system, so parsing is bounded: the body size is capped by the
fetcher, YAML goes through the safe loader, and the node count is checked on the event stream
before anything is constructed. An alias counts at the full size of what it points to, so a
"billion laughs" document is rejected without ever being expanded.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import yaml

MAX_BYTES = 10 * 1024 * 1024
# Expanded node budget: a 10 MiB description has well under a million nodes.
MAX_NODES = 2_000_000

_BASE_LOADER: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class SpecError(Exception):
    """A description that cannot be used. `code` is safe to store and show; no document text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Loader(_BASE_LOADER):  # type: ignore[valid-type,misc]
    """YAML 1.2 style scalars: only true/false are booleans and dates stay strings."""


_BOOL = "tag:yaml.org,2002:bool"
_TIMESTAMP = "tag:yaml.org,2002:timestamp"
_Loader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag not in (_BOOL, _TIMESTAMP)]
    for key, resolvers in _BASE_LOADER.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    _BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def _reject_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


def _check_yaml_size(text: str) -> None:
    sizes: dict[str, int] = {}
    stack: list[list[Any]] = []
    total = 0

    def add(size: int) -> None:
        nonlocal total
        if stack:
            stack[-1][1] += size
            if stack[-1][1] > MAX_NODES:
                raise SpecError("too_many_nodes")
        else:
            total += size
            if total > MAX_NODES:
                raise SpecError("too_many_nodes")

    for event in yaml.parse(text, Loader=_Loader):
        if isinstance(event, yaml.ScalarEvent):
            if event.anchor:
                sizes[event.anchor] = 1
            add(1)
        elif isinstance(event, yaml.AliasEvent):
            if event.anchor not in sizes:
                raise SpecError("invalid_yaml")
            add(sizes[event.anchor])
        elif isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
            stack.append([event.anchor, 1])
        elif isinstance(event, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
            anchor, size = stack.pop()
            if anchor:
                sizes[anchor] = size
            add(size)


def _plain(value: Any) -> Any:
    """Keys become strings (`200:` in YAML is an int); everything else must be JSON-shaped."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SpecError("unsupported_value")


def load(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise SpecError("too_large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SpecError("not_utf8") from None
    try:
        if text.lstrip().startswith("{"):
            data = json.loads(text, parse_constant=_reject_constant)
        else:
            _check_yaml_size(text)
            data = yaml.load(text, Loader=_Loader)  # noqa: S506 - safe loader subclass
        data = _plain(data)
    except SpecError:
        raise
    except RecursionError:
        raise SpecError("too_deep") from None
    except (ValueError, yaml.YAMLError):
        raise SpecError("invalid_document") from None
    if not isinstance(data, dict):
        raise SpecError("invalid_document")
    return data
