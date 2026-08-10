"""确定性报告后处理（零 LLM 成本）：格式一致性交给代码而不是 prompt。

对标 lunon 的 9 段后处理链（refs/lunon orchestrate.py:346-460）与 deepdog 的
citations_match_sources 正则引用校验——它们的共同洞察：格式规则 LLM 永远遵守不全，
写成确定性代码一次修对。当前只做四段有确定性边界的修复（悬空引用/非法引用标记/标题层级/空行），
引用密度 clamp、段落长度 clamp 等待 Readability A/B 后再加。
"""

import re

from dra.models import Report

_CITE_RE = re.compile(r"\[(\d+)\]")
# Writer 的引用契约只允许 [n]。这里只拦高置信的伪引用形态，避免误伤
# Markdown 链接、图片和正常方括号术语（如 [RFC]）。
_INVALID_CITATION_MARKER_RE = re.compile(
    r"(?<!!)\[(?:[A-Za-z]|evidence(?:\s*#?\s*\d+)?|"
    r"source(?:\s*#?\s*\d+)?|ref(?:erence)?(?:\s*#?\s*\d+)?)\](?!\s*\()",
    re.IGNORECASE,
)


def find_invalid_citation_markers(md: str) -> list[str]:
    """返回正文中的高置信非数字伪引用，按首次出现顺序去重。"""
    return list(dict.fromkeys(_INVALID_CITATION_MARKER_RE.findall(md or "")))


def _fix_section(md: str, n_evidence: int, stats: dict) -> str:
    # 1) 悬空引用：[n] 超出证据编号范围 → 删标记（render 本就不会把它收进 References）
    def _cite(m: re.Match) -> str:
        if 1 <= int(m.group(1)) <= n_evidence:
            return m.group(0)
        stats["dangling_removed"] += 1
        return ""
    md = _CITE_RE.sub(_cite, md)

    # 2) 非数字伪引用：[E] / [source] 等不属于 References，终稿不得透传。
    def _invalid_cite(_m: re.Match) -> str:
        stats["invalid_citations_removed"] += 1
        return ""
    md = _INVALID_CITATION_MARKER_RE.sub(_invalid_cite, md)

    # 3) 正文标题降级：render 用 `## heading` 包节，正文内 #/## 会打乱层级 → 统一压到 ###+
    def _demote(m: re.Match) -> str:
        stats["headings_demoted"] += 1
        return "### "
    md = re.sub(r"(?m)^#{1,2} ", _demote, md)

    # 4) 折叠 3+ 连续空行
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def postprocess_report(report: Report, n_evidence: int) -> tuple[Report, dict]:
    """就地修复各 section.markdown，返回 (report, 修复计数)。绝不增删 section、绝不改事实文字。"""
    stats = {
        "dangling_removed": 0,
        "invalid_citations_removed": 0,
        "headings_demoted": 0,
    }
    for s in report.sections:
        if s.markdown:
            s.markdown = _fix_section(s.markdown, n_evidence, stats)
    return report, stats
