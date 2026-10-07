"""Local `$ref` lookups inside one description. External references are never followed."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

MAX_DEPTH = 32


def local(ref: Any) -> bool:
    return isinstance(ref, str) and ref.startswith("#/")


class Refs:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc

    def target(self, ref: str) -> Any:
        """The value a local JSON Pointer reference names, or None."""
        if not local(ref):
            return None
        node: Any = self.doc
        for raw in ref[2:].split("/"):
            part = unquote(raw).replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return None
        return node

    def deref(self, node: Any) -> Any:
        """Follow a chain of `$ref` objects to the first concrete value (None if broken or cyclic).

        Sibling keys of a `$ref` are ignored, as in OpenAPI 3.0.
        """
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not local(ref) or ref in seen or len(seen) >= MAX_DEPTH:
                return None
            seen.add(ref)
            node = self.target(ref)
        return node
