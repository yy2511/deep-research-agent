"""评测完整性：不泄漏 gold、不把坏裁判输出当高分、不丢失败题。"""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval/deepsearchqa"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(EVAL_DIR))
    loaded = []
    for name in ("prepare", "run_pilot", "judge"):
        spec = importlib.util.spec_from_file_location(name, EVAL_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


def verdict(details, extras=()):
    return {"Answer Correctness": {"Explanation": "fixture", "Correctness Details": details,
                                   "Excessive Answers": list(extras)}}


def test_new_research_config_disables_jev_even_if_web_enabled(modules):
    from dra import runtime_config
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'fixture', 'model': 'fixture'},
        subagent={'provider': 'fixture', 'model': 'fixture'}, evidence_judge={'enabled': True})
    config = modules[1].research_config(settings, 1200)
    assert not config.evidence_judge.enabled
    assert not config.browser.enabled and settings.browser.enabled
    assert settings.evidence_judge.enabled  # evaluator must not rewrite Web settings
    assert config.wall_timeout_s == 1200



@pytest.mark.parametrize("details,extras,expected", [
    ({"A": True, "B": True}, [], (1, 1, 1, "fully_correct")),
    ({"A": True, "B": False}, [], (1, .5, 2/3, "partially_correct")),
    ({"A": True, "B": True}, ["C"], (2/3, 1, .8, "correct_with_extras")),
    ({"A": False, "B": False}, [], (0, 0, 0, "fully_incorrect")),
])
def test_partial_credit_and_extras(modules, details, extras, expected):
    score = modules[2].score_verdict(verdict(details, extras))
    assert [score[k] for k in ("precision", "recall", "f1")] == pytest.approx(expected[:3])
    assert score["category"] == expected[3]


@pytest.mark.parametrize("details", [{}, {"A": "true"}, {"A": 1}])
def test_invalid_judge_output_is_not_a_pass(modules, details):
    with pytest.raises(ValueError):
        modules[2].score_verdict(verdict(details))


def test_empty_answer_and_judge_failure_keep_denominator(modules):
    judge = modules[2]
    empty = judge.grade_one({}, "", {}, model="unused", provider="unused",
                            call=lambda *a, **k: pytest.fail("Empty answer must not call judge"))
    rows = [{"id": i, "response": "A" if i == 1 else "", "wall_seconds": 1, "searches": 0}
            for i in (1, 2)]
    first = judge.score_verdict(verdict({"A": True}))
    summary = judge.summarize(rows, {"1": first, "2": empty})
    assert summary["sample_size"] == 2
    assert summary["mean_f1"] == .5
    assert summary["fully_correct_rate"] == .5
    broken = judge.summarize(rows, {"1": first, "2": {"status": "judge_error"}})
    assert broken["mean_f1"] is None
    assert broken["judge_errors_or_pending"] == [2]


def test_sampling_does_not_use_gold(modules):
    runner = modules[1]
    rows = [{"id": i, "problem": f"question {i}", "answer": "SECRET"} for i in range(900)]
    chosen = runner.pick(rows)
    changed = [{**r, "answer": "OTHER SECRET"} for r in rows]
    assert [r["id"] for r in chosen] == [r["id"] for r in runner.pick(changed)]
    assert not runner.EXPOSED_IDS.intersection(r["id"] for r in chosen)


def test_targeted_rerun_selects_only_requested_ids_in_order(modules):
    runner = modules[1]
    rows = [{"id": i, "problem": str(i)} for i in range(900)]
    assert runner.pick(rows, ids=[699]) == [rows[699]]
    assert runner.pick(rows, ids=[889, 169]) == [rows[889], rows[169]]
    for invalid in ([], [699, 699], [900], [-1]):
        with pytest.raises(ValueError):
            runner.pick(rows, ids=invalid)


def test_runner_only_passes_question_and_rejects_unpublished_draft(modules, monkeypatch, tmp_path):
    runner = modules[1]
    from dra.simple_research import SimpleConfig
    question = {"id": 1, "problem": "Original question", "problem_category": "Test"}
    seen = []

    def research(query, **kwargs):
        seen.append((query, kwargs))
        return SimpleNamespace(draft="Rejected answer", draft_rejected=True, delivery_status="limited",
                               stop_reason="finished", cards=[], sources=[], calls=[], usage={}, warnings=[])

    monkeypatch.setattr(runner.simple_research, "render_markdown", lambda r: "diagnostic report")
    result = runner.run_one(question, tmp_path, SimpleConfig(), run_fn=research)
    assert seen[0][0] == "Original question"
    assert "answer_type" not in str(seen)
    assert result["response"] == ""
    assert result["status"] == "no_answer"


def test_benchmark_source_is_filtered(modules, monkeypatch):
    runner = modules[1]
    from dra.simple_research import SimpleConfig
    from dra.models import RetrievedDoc
    docs = [RetrievedDoc(title="DeepSearchQA answer", source_url="https://example.org/leak", snippet="gold"),
            RetrievedDoc(title="Original statistics", source_url="https://example.org/data", snippet="facts")]
    monkeypatch.setattr(runner.subagent, "_retrieve", lambda *a, **k: docs)
    guard = runner.WebSearch(SimpleConfig())
    assert [d.title for d in guard.search("question")] == ["Original statistics"]
    assert guard.blocked == ["https://example.org/leak"]


def test_runner_closes_new_download_route_and_keeps_vision_off(modules, monkeypatch, tmp_path):
    runner = modules[1]
    from dra import file_sources
    from dra.simple_research import RunStore, ToolContext, SimpleConfig
    from dra.vision import VisionConfig
    original = file_sources.fetch_original
    seen = []
    def research(query, **kwargs):
        config = kwargs['config']
        seen.append(config.vision.enabled)
        assert not config.browser.enabled
        ctx = ToolContext(RunStore(tmp_path / 'isolated', run_id='guard', query=query), config)
        receipt, ok = ctx.execute('read_source', {'url': 'https://example.org/chart.png', 'original': True})
        assert not ok and '原件下载未启用' in receipt
        assert not ctx.store.state['sources']
        return SimpleNamespace(draft='', draft_rejected=False, delivery_status='limited',
            stop_reason='finished', cards=[], sources=[], calls=[], usage={}, warnings=[])
    monkeypatch.setattr(runner.simple_research, 'render_markdown', lambda *a: '')
    result = runner.run_one({'id': 1, 'problem': 'offline question'}, tmp_path / 'out',
        SimpleConfig(vision=VisionConfig(enabled=True), browser={'enabled': True}), run_fn=research)
    assert seen == [False]
    assert result['blocked_original_downloads'] == ['https://example.org/chart.png']
    assert file_sources.fetch_original is original


def test_prompt_extraction_does_not_execute_notebook(modules):
    source = '''
import textwrap
raise RuntimeError("must not execute")
DEEPSEARCH_QA_PROMPT = textwrap.dedent("  Judge\\n")
GRADER_RATING_OUTPUT_EXAMPLE = "{prompt} / {answer} / {response}"
'''
    notebook = json.dumps({"cells": [{"cell_type": "code", "source": [source]}]})
    assert modules[0].extract_prompt(notebook)["DEEPSEARCH_QA_PROMPT"] == "Judge\n"
