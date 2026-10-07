"""In-process BM25 over one catalog's operations. Chinese text is split into character bigrams."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_WORD = re.compile(r"[A-Za-z]+|\d+")
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
K1 = 1.2
B = 0.75


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text):
        parts = [p.lower() for p in _CAMEL.findall(word)] or [word.lower()]
        out.extend(parts)
        if len(parts) > 1:
            out.append(word.lower())
    for run in _CJK.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def _document(op_id: str, op: dict[str, Any], module_text: str) -> list[str]:
    words = tokens(op_id) * 2 + tokens(op.get("summary", "")) * 2
    words += tokens(op.get("description", ""))
    words += tokens(module_text)
    for segment in str(op.get("path", "")).split("/"):
        if segment and not segment.startswith("{"):
            words += tokens(segment)
    return words


class SearchIndex:
    def __init__(self, compiled: dict[str, Any]) -> None:
        modules = {
            m["name"]: f"{m['name']} {m.get('description', '')}"
            for m in compiled.get("modules", [])
        }
        self.docs: dict[str, Counter[str]] = {}
        self.lengths: dict[str, int] = {}
        frequency: Counter[str] = Counter()
        for op_id, op in compiled.get("operations", {}).items():
            words = _document(op_id, op, modules.get(op.get("module", ""), ""))
            counts = Counter(words)
            self.docs[op_id] = counts
            self.lengths[op_id] = len(words)
            frequency.update(counts.keys())
        total = len(self.docs)
        self.average = (sum(self.lengths.values()) / total) if total else 0.0
        self.idf = {
            term: math.log(1 + (total - n + 0.5) / (n + 0.5)) for term, n in frequency.items()
        }

    def search(self, query: str) -> list[tuple[str, float]]:
        terms = set(tokens(query))
        scored: list[tuple[str, float]] = []
        for op_id, counts in self.docs.items():
            score = 0.0
            length = self.lengths[op_id]
            for term in terms:
                tf = counts.get(term)
                if not tf:
                    continue
                norm = tf + K1 * (1 - B + B * length / (self.average or 1))
                score += self.idf[term] * tf * (K1 + 1) / norm
            if score > 0:
                scored.append((op_id, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored
