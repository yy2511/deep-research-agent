"""确定性后处理：悬空引用/标题层级/空行，全代码修复零 LLM。"""
from dra.models import Report, ReportSection
from dra.postprocess import postprocess_report


def _rep(md: str) -> Report:
    return Report(title="t", sections=[ReportSection(heading="h", markdown=md)])


def test_dangling_citation_removed():
    rep, stats = postprocess_report(_rep("结论成立[2]，另见[9]。"), n_evidence=3)
    assert rep.sections[0].markdown == "结论成立[2]，另见。"
    assert stats["dangling_removed"] == 1


def test_table_cell_citation_preserved():
    md = "| 维度 | A |\n|---|---|\n| 成本 | 高[1] |\n\n正文解释成本影响。"
    rep, stats = postprocess_report(_rep(md), n_evidence=3)
    assert "高[1]" in rep.sections[0].markdown.splitlines()[2]
    assert "table_cites_removed" not in stats


def test_heading_demoted():
    rep, stats = postprocess_report(_rep("# 大标题\n\n## 次级\n\n### 保持"), n_evidence=1)
    md = rep.sections[0].markdown
    assert md.startswith("### 大标题")
    assert "\n### 次级" in md and md.count("####") == 0
    assert stats["headings_demoted"] == 2


def test_blank_lines_collapsed():
    rep, _ = postprocess_report(_rep("a\n\n\n\n\nb"), n_evidence=1)
    assert rep.sections[0].markdown == "a\n\nb"
