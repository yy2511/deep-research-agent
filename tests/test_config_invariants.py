"""co-knob 不变量：耦合旋钮单独改一个 → 构造期 fail loud（lunon 式 import 时检查）。"""
import pytest

from dra.orchestrator import OrchestratorConfig
from dra.subagent import SubAgentConfig


def test_orchestrator_timeout_covers_subagent():
    with pytest.raises(ValueError, match="total_timeout_s"):
        OrchestratorConfig(total_timeout_s=600,
                           subagent=SubAgentConfig(wall_timeout_s=900))


def test_orchestrator_timeout_reserves_writer_window():
    with pytest.raises(ValueError, match="writer_reserve_s"):
        OrchestratorConfig(total_timeout_s=360, writer_reserve_s=360)
    with pytest.raises(ValueError, match="writer_reserve_s"):
        OrchestratorConfig(
            total_timeout_s=1_200,
            writer_reserve_s=360,
            subagent=SubAgentConfig(wall_timeout_s=900),
        )


def test_defaults_are_legal():
    OrchestratorConfig()   # 默认组合必须永远合法
    SubAgentConfig()


def test_condense_ctx_propagates(monkeypatch):
    """condense 线程池 worker 里能读到 contextvars（trace 归属修复）。"""
    from dra import timing
    import dra.nodes as nodes
    from dra.models import RetrievedDoc

    seen = []

    def fake_summarize(query, doc, *, model, provider, reasoning, effort=None):
        seen.append(timing.get_ctx().get("step"))
        return "s"

    monkeypatch.setattr(nodes, "summarize_doc", fake_summarize)
    docs = [RetrievedDoc(title=f"t{i}", snippet="s", raw_content="x" * 7000)
            for i in range(3)]
    with timing.step("condense"):
        nodes.condense_docs("q", docs)
    assert seen == ["condense"] * 3


def test_ready_set_budgets_must_be_positive():
    with pytest.raises(ValueError, match="max_research_rounds"):
        OrchestratorConfig(max_research_rounds=0)
    with pytest.raises(ValueError, match="max_tasks_per_round"):
        OrchestratorConfig(max_tasks_per_round=0)
