"""Compile an OpenAPI description into the catalog stored in `system_catalogs.compiled`.

The catalog keeps what the layered tools need and nothing else: modules, one summary line per
operation, and a bounded detail (parameters, request body, success response, example). Schemas are
expanded from local `$ref`s with depth, width and cycle limits, so one operation can never pull in
the whole description.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from coreman.core.systems_catalog.lint import METHODS, lint, operations
from coreman.core.systems_catalog.refs import MAX_DEPTH, Refs, local

# Bump when the compiled format changes: stored catalogs of an older version are fetched again
# in full (no If-None-Match) and recompiled on the next check.
COMPILER_VERSION = 1
RISKS = ("read", "write", "destructive", "financial")
DEFAULT_MODULE = "default"
GUIDE_MAX = 2000
SUMMARY_MAX = 120
DESCRIPTION_MAX = 1000
TEXT_MAX = 200
HINT_MAX = 500
ENUM_SHOWN = 20
BODY_DEPTH = 4
RESPONSE_DEPTH = 3
MAX_PROPERTIES = 40
MAX_VARIANTS = 10
EXAMPLE_MAX = 1000


@dataclass(frozen=True)
class Compiled:
    compiled: dict[str, Any]
    lint: list[dict[str, str]]
    operation_count: int
    hidden_count: int
    module_count: int


def clip(value: Any, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def same_origin(url: str, base_url: str) -> bool:
    try:
        a, b = urlsplit(url), urlsplit(base_url)
        return bool(a.hostname) and (
            a.scheme,
            a.hostname,
            a.port or (443 if a.scheme == "https" else 80),
        ) == (b.scheme, b.hostname, b.port or (443 if b.scheme == "https" else 80))
    except ValueError:
        return False


def server_url(doc: dict[str, Any], spec_url: str, base_url: str) -> str:
    """Prefix for operation paths: the first server, resolved against the description's own URL.

    Relative server URLs are relative to the document (OpenAPI 3); an absolute one on another
    origin is ignored, so calls can only ever go to the system's own origin.
    """
    base = base_url.rstrip("/")
    origin = urlsplit(base)
    fallback = f"{origin.scheme}://{origin.netloc}"
    servers = doc.get("servers")
    first = servers[0] if isinstance(servers, list) and servers else None
    if not isinstance(first, dict) or not isinstance(first.get("url"), str):
        return fallback
    url = first["url"]
    variables = first.get("variables")
    if isinstance(variables, dict):
        for name, spec in variables.items():
            if isinstance(spec, dict) and isinstance(spec.get("default"), str):
                url = url.replace("{" + name + "}", spec["default"])
    if "{" in url:
        return fallback
    resolved = urljoin(spec_url, url)
    if not same_origin(resolved, base):
        return fallback
    parts = urlsplit(resolved)
    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")


def permission_codes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [] if value == "none" else [value]
    if isinstance(value, list):
        return [p for p in value if isinstance(p, str) and p and p != "none"]
    return []


def scalar_preview(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return clip(value, TEXT_MAX) if isinstance(value, str) else value
    return None


class Schemas:
    """Bounded expansion of request and response schemas into a compact, model-readable form."""

    def __init__(self, refs: Refs) -> None:
        self.refs = refs

    def resolve(self, schema: Any, stack: tuple[str, ...]) -> tuple[Any, tuple[str, ...], Any]:
        """Follow `$ref`s. Returns (schema, stack, marker); marker replaces an unusable ref."""
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if not local(ref):
                return None, stack, {"$ref": ref, "external": True}
            if ref in stack or len(stack) >= MAX_DEPTH:
                return None, stack, {"$ref": ref, "recursive": True}
            target = self.refs.target(ref)
            if target is None:
                return None, stack, {"$ref": ref, "unresolved": True}
            schema, stack = target, (*stack, ref)
        return schema, stack, None

    def merge_all_of(self, schema: dict[str, Any], stack: tuple[str, ...]) -> dict[str, Any]:
        """Flatten `allOf` members into one schema: properties and required are unioned."""
        merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
        properties = dict(merged.get("properties") or {})
        required = list(merged.get("required") or [])
        for member in schema.get("allOf") or []:
            part, part_stack, marker = self.resolve(member, stack)
            if not isinstance(part, dict):
                continue
            if "allOf" in part:
                part = self.merge_all_of(part, part_stack)
            for key, value in part.items():
                if key == "properties" and isinstance(value, dict):
                    properties.update(value)
                elif key == "required" and isinstance(value, list):
                    required.extend(r for r in value if r not in required)
                else:
                    merged.setdefault(key, value)
        if properties:
            merged["properties"] = properties
            merged.setdefault("type", "object")
        if required:
            merged["required"] = required
        return merged

    def compact(self, schema: Any, depth: int, stack: tuple[str, ...] = ()) -> Any:
        schema, stack, marker = self.resolve(schema, stack)
        if marker is not None:
            return marker
        if not isinstance(schema, dict):
            return {}
        if "allOf" in schema:
            schema = self.merge_all_of(schema, stack)
        out: dict[str, Any] = {}
        kind = schema.get("type")
        if isinstance(kind, list):
            if "null" in kind:
                out["nullable"] = True
            kind = next((k for k in kind if k != "null"), None)
        if isinstance(kind, str):
            out["type"] = kind
        if schema.get("nullable") is True:
            out["nullable"] = True
        if isinstance(schema.get("format"), str):
            out["format"] = schema["format"]
        if schema.get("description"):
            out["description"] = clip(schema["description"], TEXT_MAX)
        enum = schema.get("enum")
        if isinstance(enum, list):
            out["enum"] = [scalar_preview(v) for v in enum[:ENUM_SHOWN]]
            if len(enum) > ENUM_SHOWN:
                out["enum_total"] = len(enum)
        if "default" in schema and scalar_preview(schema["default"]) is not None:
            out["default"] = scalar_preview(schema["default"])
        if isinstance(schema.get("x-agent-options"), str):
            out["options"] = schema["x-agent-options"]
        for key in ("minimum", "maximum", "minLength", "maxLength", "maxItems", "pattern"):
            if isinstance(schema.get(key), (int, float, str)) and not isinstance(
                schema.get(key), bool
            ):
                out[key] = schema[key]
        variants = next((k for k in ("oneOf", "anyOf") if isinstance(schema.get(k), list)), None)
        properties = schema.get("properties")
        items = schema.get("items")
        nested = variants is not None or isinstance(properties, dict) or items is not None
        if nested and depth <= 0:
            out["truncated"] = True
            return out
        if variants is not None:
            options = schema[variants]
            out[variants] = [self.compact(v, depth - 1, stack) for v in options[:MAX_VARIANTS]]
            if len(options) > MAX_VARIANTS:
                out[variants + "_total"] = len(options)
        if isinstance(properties, dict):
            names = list(properties)
            required = schema.get("required")
            if isinstance(required, list):
                out["required"] = [r for r in required if isinstance(r, str)]
            out["properties"] = {
                name: self.compact(properties[name], depth - 1, stack)
                for name in names[:MAX_PROPERTIES]
            }
            if len(names) > MAX_PROPERTIES:
                out["omitted_properties"] = len(names) - MAX_PROPERTIES
        if items is not None:
            out["items"] = self.compact(items, depth - 1, stack)
        extra = schema.get("additionalProperties")
        if extra is True:
            out["additionalProperties"] = True
        elif isinstance(extra, dict):
            out["additionalProperties"] = self.compact(extra, depth - 1, stack)
        return out

    def type_label(self, schema: Any) -> str:
        resolved, stack, marker = self.resolve(schema, ())
        if marker is not None or not isinstance(resolved, dict):
            return "any"
        kind = resolved.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "any")
        label = kind if isinstance(kind, str) else "any"
        if label == "array":
            return f"array<{self.type_label(resolved.get('items'))}>"
        if isinstance(resolved.get("format"), str):
            label += f"({resolved['format']})"
        return label


def _example(media: dict[str, Any], refs: Refs) -> str | None:
    value: Any = None
    if "example" in media:
        value = media["example"]
    elif isinstance(media.get("examples"), dict) and media["examples"]:
        first = refs.deref(next(iter(media["examples"].values())))
        value = first.get("value") if isinstance(first, dict) else None
    if value is None:
        schema = refs.deref(media.get("schema"))
        value = schema.get("example") if isinstance(schema, dict) else None
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return clip(text, EXAMPLE_MAX)


def _json_media(content: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(content, dict):
        return None
    for media_type, media in content.items():
        base = media_type.split(";", 1)[0].strip().lower()
        if (base == "application/json" or base.endswith("+json")) and isinstance(media, dict):
            return media_type, media
    return None


def _parameters(raw: list[Any], schemas: Schemas, refs: Refs) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        param = refs.deref(item)
        if not isinstance(param, dict) or not isinstance(param.get("name"), str):
            continue
        schema = param.get("schema")
        entry: dict[str, Any] = {
            "name": param["name"],
            "in": param.get("in"),
            "required": param.get("required") is True,
            "type": schemas.type_label(schema),
        }
        if param.get("description"):
            entry["description"] = clip(param["description"], TEXT_MAX)
        resolved = refs.deref(schema)
        options = param.get("x-agent-options") or (
            resolved.get("x-agent-options") if isinstance(resolved, dict) else None
        )
        if isinstance(options, str):
            entry["options"] = options
        if isinstance(resolved, dict):
            enum = resolved.get("enum")
            if enum is None and isinstance(refs.deref(resolved.get("items")), dict):
                enum = refs.deref(resolved.get("items")).get("enum")
            if isinstance(enum, list):
                entry["enum"] = [scalar_preview(v) for v in enum[:ENUM_SHOWN]]
                if len(enum) > ENUM_SHOWN:
                    entry["enum_total"] = len(enum)
            if "default" in resolved and scalar_preview(resolved["default"]) is not None:
                entry["default"] = scalar_preview(resolved["default"])
        out.append(entry)
    return out


def _merged_parameters(item: dict[str, Any], op: dict[str, Any], refs: Refs) -> list[Any]:
    """Path-level parameters overridden by operation-level ones with the same (name, in)."""
    merged: dict[tuple[Any, Any], Any] = {}
    for source in (item.get("parameters"), op.get("parameters")):
        if not isinstance(source, list):
            continue
        for raw in source:
            param = refs.deref(raw)
            if isinstance(param, dict):
                merged[(param.get("name"), param.get("in"))] = raw
    return list(merged.values())


def _detail(
    item: dict[str, Any], op: dict[str, Any], schemas: Schemas, refs: Refs
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "parameters": _parameters(_merged_parameters(item, op, refs), schemas, refs),
        "body": None,
        "response": None,
        "example": None,
    }
    body = refs.deref(op.get("requestBody"))
    if isinstance(body, dict) and isinstance(body.get("content"), dict) and body["content"]:
        picked = _json_media(body["content"]) or next(iter(body["content"].items()))
        media_type, media = picked
        media = media if isinstance(media, dict) else {}
        detail["body"] = {
            "content_type": media_type,
            "required": body.get("required") is True,
            "schema": schemas.compact(media.get("schema"), BODY_DEPTH),
        }
        if body.get("description"):
            detail["body"]["description"] = clip(body["description"], TEXT_MAX)
        detail["example"] = _example(media, refs)
    responses = op.get("responses")
    if isinstance(responses, dict):
        for code in sorted(c for c in responses if c.startswith("2")):
            response = refs.deref(responses[code])
            if not isinstance(response, dict):
                continue
            json_media = _json_media(response.get("content"))
            if json_media is not None:
                detail["response"] = {
                    "status": code,
                    "schema": schemas.compact(json_media[1].get("schema"), RESPONSE_DEPTH),
                }
            elif isinstance(response.get("content"), dict) and response["content"]:
                # Files and other non-JSON bodies: only the media type.
                detail["response"] = {
                    "status": code,
                    "content_type": next(iter(response["content"])),
                }
            else:
                detail["response"] = {"status": code}
            break
    return detail


def _permissions(doc: dict[str, Any], by_id: dict[str, tuple[str, str]]) -> dict[str, Any] | None:
    agent = doc.get("x-agent")
    spec = agent.get("permissions") if isinstance(agent, dict) else None
    if not isinstance(spec, dict):
        return None
    op_id, ptr = spec.get("operationId"), spec.get("pointer")
    if not isinstance(op_id, str) or not isinstance(ptr, str) or not ptr.startswith("/"):
        return None
    found = by_id.get(op_id)
    if found is None or found[1] != "get":
        return None
    return {"operation_id": op_id, "pointer": ptr, "method": "GET", "path": found[0]}


def compile_spec(doc: dict[str, Any], *, spec_url: str, base_url: str) -> Compiled:
    refs = Refs(doc)
    schemas = Schemas(refs)
    findings = lint(doc)
    found = operations(doc)
    counts: dict[str, int] = {}
    for _, _, op in found:
        op_id = op.get("operationId")
        if isinstance(op_id, str) and op_id:
            counts[op_id] = counts.get(op_id, 0) + 1
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    assert isinstance(paths, dict)
    by_id: dict[str, tuple[str, str]] = {}
    compiled_ops: dict[str, dict[str, Any]] = {}
    hidden = 0
    for path, method, op in found:
        op_id = op.get("operationId")
        # Missing or duplicate ids cannot be addressed unambiguously: skip every occurrence.
        if not isinstance(op_id, str) or not op_id or counts[op_id] > 1:
            continue
        by_id[op_id] = (path, method)
        agent = op.get("x-agent") if isinstance(op.get("x-agent"), dict) else {}
        assert isinstance(agent, dict)
        if agent.get("hidden") is True:
            hidden += 1
            continue
        tags = op.get("tags")
        module = tags[0] if isinstance(tags, list) and tags and isinstance(tags[0], str) else None
        risk = agent.get("risk")
        if risk not in RISKS:
            risk = "read" if method in ("get", "head") else "write"
        extra = agent.get("extra_permissions")
        compiled_ops[op_id] = {
            "method": method.upper(),
            "path": path,
            "module": module or DEFAULT_MODULE,
            "summary": clip(op.get("summary"), SUMMARY_MAX),
            "description": clip(op.get("description"), DESCRIPTION_MAX),
            "permission": permission_codes(op.get("x-permission")),
            "extra_permissions": [p for p in extra if isinstance(p, str)]
            if isinstance(extra, list)
            else [],
            "risk": risk,
            "hint": clip(agent.get("hint"), HINT_MAX),
            "deprecated": op.get("deprecated") is True,
            "detail": _detail(paths[path], op, schemas, refs),
        }
    tag_defs: dict[str, str] = {}
    for tag in doc.get("tags") or []:
        if isinstance(tag, dict) and isinstance(tag.get("name"), str):
            tag_defs.setdefault(tag["name"], clip(tag.get("description"), TEXT_MAX))
    grouped: dict[str, list[str]] = {}
    for op_id, op in compiled_ops.items():
        grouped.setdefault(op["module"], []).append(op_id)
    order = [name for name in tag_defs if name in grouped] + [
        name for name in grouped if name not in tag_defs
    ]
    modules = [
        {"name": name, "description": tag_defs.get(name, ""), "operations": grouped[name]}
        for name in order
    ]
    agent = doc.get("x-agent") if isinstance(doc.get("x-agent"), dict) else {}
    assert isinstance(agent, dict)
    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    assert isinstance(info, dict)
    compiled = {
        "title": clip(info.get("title"), TEXT_MAX),
        "guide": clip(agent.get("guide"), GUIDE_MAX),
        "server": server_url(doc, spec_url, base_url),
        "permissions": _permissions(doc, by_id),
        "modules": modules,
        "operations": compiled_ops,
    }
    return Compiled(compiled, findings, len(compiled_ops), hidden, len(modules))


__all__ = ["COMPILER_VERSION", "METHODS", "Compiled", "compile_spec", "same_origin"]
