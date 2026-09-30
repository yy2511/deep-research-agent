"""逐段证据及确定性的快照上下文；不检索远处论据，不生成原话。"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_QUOTES = 8
MAX_QUOTE_CHARS = 24000
MAX_CONTEXT_CHARS = 6000
NEARBY_CHARS = 500


class QuoteContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    location: str
    kind: Literal["nearby", "structure"] = "nearby"


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str
    submitted_quote: str | None = None
    source_ref: str
    source_url: str = ""
    source_title: str = ""
    published_at: str | None = None
    location: str = ""
    deep_link: str = ""
    summary_ref: str | None = None
    context: list[QuoteContext] = Field(default_factory=list)


def chars_of(location: str) -> tuple[int, int]:
    match = re.fullmatch(r"chars=(\d+)-(\d+)", location)
    if not match:
        raise ValueError("引文没有可验证的字符位置")
    return tuple(map(int, match.groups()))


def _uncovered(lo, hi, covered):
    for a, b in sorted(covered):
        if b <= lo:
            continue
        if a >= hi:
            break
        if a > lo:
            yield lo, a
        lo = max(lo, b)
    if lo < hi:
        yield lo, hi


def _structure(raw: str, lo: int):
    # 只认明确的 Markdown 标题层级；不把随便一个短句猜作章节名。
    stack = []
    for m in re.finditer(r"(?m)^ {0,3}(#{1,6})[ \t]+[^\n]{1,240}$", raw[:lo]):
        level = len(m[1])
        stack = [row for row in stack if row[0] < level]
        stack.append((level, m.start(), m.end()))
    yield from ((a, b) for _, a, b in stack[-3:])
    # 表头必须和引文处于同一个连续 Markdown 表格中，不跨表猜列关系。
    line_start = raw.rfind("\n", 0, lo) + 1
    line_end = raw.find("\n", lo)
    if line_end < 0:
        line_end = len(raw)
    if "|" not in raw[line_start:line_end]:
        return
    start = line_start
    while start > max(0, lo - 3000):
        previous = raw.rfind("\n", 0, start - 1) + 1
        if "|" not in raw[previous:start]:
            break
        start = previous
    prefix = raw[start:line_end]
    header = re.match(r"[^\n]*\|[^\n]*\n[ \t|:\-]+\n", prefix)
    if header and re.search(r"-{3,}", header[0]) and len(header[0]) <= 600:
        yield start, start + header.end()


def add_context(quotes: list[EvidenceQuote], raw_sources: dict[str, str]) -> None:
    """原话原封保留，额外上下文同源去重；不同位置始终分块显示。"""
    covered: dict[str, list[tuple[int, int]]] = {}
    for q in quotes:
        covered.setdefault(q.source_ref, []).append(chars_of(q.location))
    remaining = MAX_CONTEXT_CHARS
    for i, q in enumerate(quotes):
        raw = raw_sources[q.source_ref]
        lo, hi = chars_of(q.location)
        before, after = max(0, lo - NEARBY_CHARS), min(len(raw), hi + NEARBY_CHARS)
        # 尽量保留完整行；超长单行按字符有界裁剪，位置明确表示仅片段。
        boundary = raw.find("\n", before, lo)
        if before and raw[before - 1] != "\n" and boundary >= 0:
            before = boundary + 1
        boundary = raw.rfind("\n", hi, after)
        if after < len(raw) and boundary >= 0:
            after = boundary + 1
        candidates = [(a, b, "structure") for a, b in _structure(raw, lo)]
        candidates += [(before, lo, "nearby"), (hi, after, "nearby")]
        allowance = remaining // (len(quotes) - i)
        q.context = []
        for a, b, kind in candidates:
            for c, d in list(_uncovered(a, b, covered[q.source_ref])):
                if allowance <= 0:
                    break
                if d - c > allowance:
                    if d <= lo and kind == "nearby":
                        c = d - allowance
                    else:
                        d = c + allowance
                if not raw[c:d].strip():
                    continue
                q.context.append(QuoteContext(text=raw[c:d], location=f"chars={c}-{d}", kind=kind))
                covered[q.source_ref].append((c, d))
                remaining -= d - c
                allowance -= d - c
        q.context.sort(key=lambda block: chars_of(block.location))
