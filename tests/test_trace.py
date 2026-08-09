"""dra.trace —— ResearchState → 自包含 HTML 轨迹查看器 的测试（纯逻辑，不调 LLM）。

- build_trace：从 dict / ResearchState 两种输入抽出渲染结构
- render_html：含 query / 研究任务 / 证据 claim+quote / 报告规划 / 报告 / 状态徽章
- HTML 转义：claim 里的 <b> 被转义（防注入 / 防破版）
- save_trace：落盘返回路径；坏输入吞异常返 None（非侵入，绝不弄崩主流程）
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from dra import trace as T  # noqa: E402
from dra.models import (  # noqa: E402
    EvidenceCard,
    PlanNode,
    Report,
    ReportPlan,
    ReportPlanSection,
    ReportSection,
    ResearchPlan,
    ResearchState,
    ResearchTask,
    SubAgentReport,
)


def _state() -> ResearchState:
    card = EvidenceCard(
        claim="RAG 通过检索外部知识减少幻觉",
        support_quote="RAG retrieves external documents to ground generation and reduce hallucination.",
        source_url="https://arxiv.org/abs/2005.11401",
        published_at="2020-05-22",
    )
    sub = SubAgentReport(
        research_task_id="A", objective="RAG 的优点", tool_calls=2,
        summary="找到 RAG 降幻觉的证据", evidence=[card], conflicts=[],
    )
    return ResearchState(
        query="对比 RAG 与微调",
        status="done",
        evidence=[card],
        research_plan=ResearchPlan(
            clarified_query="对比 RAG 与微调",
            plan_nodes=[PlanNode(
                id="benefit", objective="RAG 的优点", acceptance_criteria="有可引用证据",
            )],
            initial_tasks=[ResearchTask(
                id="task-benefit", node_id="benefit",
                objective="RAG 的优点", search_query="RAG benefits",
            )],
        ),
        report_plan=ReportPlan(sections=[
            ReportPlanSection(heading="成本差异", covers="对比成本", limitations=["微调GPU成本"]),
        ]),
        sub_reports=[sub],
        report=Report(title="RAG vs 微调 报告", sections=[
            ReportSection(heading="执行摘要", markdown="RAG 更灵活。"),
        ]),
    )


# ---------------------------------------------------------------------------
# build_trace
# ---------------------------------------------------------------------------

def test_build_trace_from_state():
    tr = T.build_trace(_state())
    assert tr["query"] == "对比 RAG 与微调"
    assert tr["status"] == "done"
    assert tr["n_evidence"] == 1
    assert tr["n_domains"] == 1                      # arxiv.org
    assert len(tr["initial_tasks"]) == 1
    assert len(tr["sub_reports"]) == 1
    assert tr["report_plan"]["sections"][0]["heading"] == "成本差异"


def test_build_trace_from_dict():
    """接受 model_dump() 后的 dict（落盘 state.json 复算场景）。"""
    tr = T.build_trace(_state().model_dump())
    assert tr["n_evidence"] == 1
    assert tr["sub_reports"][0]["objective"] == "RAG 的优点"


def test_build_trace_none_and_empty_no_crash():
    assert T.build_trace(None)["n_evidence"] == 0
    assert T.build_trace({})["query"] == ""


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------

def test_render_html_contains_key_elements():
    html = T.render_html(T.build_trace(_state()), timing_summary="search 10.0s")
    assert "<!doctype html>" in html.lower()
    assert "对比 RAG 与微调" in html               # query
    assert "RAG 的优点" in html                     # 研究任务/子代理 objective
    assert "RAG 通过检索外部知识减少幻觉" in html   # 证据 claim
    assert "reduce hallucination" in html           # 逐字 quote（grounding 可视化）
    assert "成本差异" in html                        # 报告蓝图
    assert "post-research 报告蓝图" in html
    assert "局限/后续研究提示" in html
    assert "实验性补派" not in html
    assert "RAG vs 微调 报告" in html                # 报告标题
    assert "search 10.0s" in html                   # timing 摘要嵌入
    assert 'class="status done"' in html            # 状态徽章


def test_render_html_escapes_user_content():
    """claim 里的 HTML 必须被转义，防破版/注入。"""
    st = _state()
    st.evidence[0].claim = "危险 <b>注入</b> & <script>x</script>"
    st.sub_reports[0].evidence[0] = st.evidence[0]
    html = T.render_html(T.build_trace(st))
    assert "&lt;b&gt;" in html
    assert "<script>x</script>" not in html         # 原始 script 不应出现


def test_render_html_minimal_state_no_crash():
    """最小 state（无报告规划/无子代理/无报告）也能渲染、不抛。"""
    html = T.render_html(T.build_trace(ResearchState(query="q")))
    assert "q" in html
    assert "无子代理记录" in html
    assert "实验性补派" not in html


# ---------------------------------------------------------------------------
# save_trace：落盘 + 非侵入
# ---------------------------------------------------------------------------

def test_save_trace_writes_file(tmp_path):
    p = T.save_trace(_state(), tmp_path, slug="demo", timing_summary="x")
    assert p is not None and p.exists()
    assert p.suffix == ".html" and "demo" in p.name
    assert "对比 RAG 与微调" in p.read_text(encoding="utf-8")


def test_save_trace_swallows_errors_returns_none(tmp_path):
    """坏输入不能让主流程崩——save_trace 吞异常返 None。"""
    class Boom:
        def model_dump(self):
            raise RuntimeError("boom")
    # _to_dict 对 Boom.model_dump 抛错会兜成 {}，仍能渲染空 trace → 落盘成功。
    # 用一个不可写目录路径触发真异常路径，验证返回 None 而非抛。
    p = T.save_trace(_state(), "/proc/nonexistent_dir_xyz/sub", slug="x")
    assert p is None


def test_trace_html_wraps_long_unbreakable_strings():
    """证据 URL / 长无空格串折行不溢出（同 web._PAGE 修复，2026-07-06 用户实测）：
    overflow-wrap:anywhere 挂 body 全局兜底。"""
    from dra.trace import _CSS

    body_css = next(ln for ln in _CSS.splitlines() if "body{" in ln)
    assert "overflow-wrap:anywhere" in body_css
