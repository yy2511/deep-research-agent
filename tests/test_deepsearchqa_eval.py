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


def test_runner_downloads_originals_with_leak_guard_and_follows_vision_setting(modules, monkeypatch, tmp_path):
    """原件下载开放，但与网页阅读同样过滤基准答案页；评测结束恢复原下载函数。"""
    runner = modules[1]
    from dra import file_sources
    from dra.simple_research import SimpleConfig
    from dra.vision import VisionConfig
    original = file_sources.fetch_original
    calls = []
    def fake_download(url, **kwargs):
        calls.append(url)
        if 'leak' in url:
            return b'x', 'Notes on DeepSearchQA answers', {'final_url': url, 'filename': 'a.csv'}
        return b'a,b', 'a,b', {'final_url': url, 'filename': 'table.ods'}
    monkeypatch.setattr(file_sources, 'fetch_original', fake_download)
    seen = []
    def research(query, **kwargs):
        config = kwargs['config']
        seen.append(config.vision.enabled)
        assert not config.browser.enabled
        assert file_sources.fetch_original('https://orr.gov.uk/table.ods')[2]['filename'] == 'table.ods'
        with pytest.raises(runner.ExecutionError) as error:
            file_sources.fetch_original('https://example.org/leak.csv')
        assert error.value.code == 'benchmark_page_excluded'
        with pytest.raises(runner.ExecutionError):
            file_sources.fetch_original('https://kaggle.com/benchmarks/google/dsqa-full.csv')
        return SimpleNamespace(draft='', draft_rejected=False, delivery_status='limited',
            stop_reason='finished', cards=[], sources=[], calls=[], usage={}, warnings=[])
    monkeypatch.setattr(runner.simple_research, 'render_markdown', lambda *a: '')
    result = runner.run_one({'id': 1, 'problem': 'offline question'}, tmp_path / 'out',
        SimpleConfig(vision=VisionConfig(enabled=True), browser={'enabled': True}), run_fn=research)
    assert seen == [True]
    assert calls == ['https://orr.gov.uk/table.ods', 'https://example.org/leak.csv']  # 第三个在下载前就被拦下
    assert result['blocked_original_downloads'] == ['https://example.org/leak.csv',
                                                     'https://kaggle.com/benchmarks/google/dsqa-full.csv']
    assert file_sources.fetch_original is fake_download
    assert original is not None


def test_research_config_offers_the_web_skill_catalog(modules, monkeypatch, tmp_path):
    """评测与网页“自动”模式一样提供全部已启用技能目录，不预选。"""
    runner = modules[1]
    from dra import runtime_config, skills
    monkeypatch.setattr('dra.paths.DATA_DIR', tmp_path)
    expected = skills.SkillRepository(tmp_path).prepare([])
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'f', 'model': 'f'}, subagent={'provider': 'f', 'model': 'f'})
    bundle = runner.research_config(settings, 600).skill_bundle
    assert bundle['selected'] == [] and set(bundle['skills']) == set(expected['skills']) and bundle['skills']
    assert bundle['id'] == expected['id']
    vision = runtime_config.RuntimeModelSettings(planner={'provider': 'f', 'model': 'f'}, subagent={'provider': 'f', 'model': 'f'},
        vision={'enabled': True, 'mode': 'auto', 'provider': 'g', 'model': 'gemini-2.5-flash'})
    assert runner.research_config(vision, 600).vision.model_dump() == vision.vision.model_dump()


def test_prompt_extraction_does_not_execute_notebook(modules):
    source = '''
import textwrap
raise RuntimeError("must not execute")
DEEPSEARCH_QA_PROMPT = textwrap.dedent("  Judge\\n")
GRADER_RATING_OUTPUT_EXAMPLE = "{prompt} / {answer} / {response}"
'''
    notebook = json.dumps({"cells": [{"cell_type": "code", "source": [source]}]})
    assert modules[0].extract_prompt(notebook)["DEEPSEARCH_QA_PROMPT"] == "Judge\n"


def _brief(needs_confirmation):
    from dra.research_brief import ResearchBrief
    return ResearchBrief(original_query="Original question", objective="Find the operators",
        requirements=[{"id": "req-goal", "text": "Answer the question", "kind": "goal"},
                      {"id": "req-src", "text": "Use the named report", "kind": "constraint"}],
        route=[{"id": "route-1", "text": "Read the report as published"},
               {"id": "route-2", "text": "Flag boundary cases"}],
        assumptions=["Use the On Time measure"], needs_confirmation=needs_confirmation,
        confirmation_reason="fixture")


@pytest.mark.parametrize("needs_confirmation", [False, True])
def test_planning_skips_clarification_and_passes_unconfirmed_plan(modules, monkeypatch, tmp_path, needs_confirmation):
    """先生成计划再研究：不调用澄清判断；计划要求确认也照常开跑，且只能写成未经用户确认。"""
    runner = modules[1]
    from dra.simple_research import SimpleConfig
    monkeypatch.setattr(runner.research_brief, "assess_research_gaps", lambda *a, **k: pytest.fail("no clarification"))
    monkeypatch.setattr(runner.simple_research, "render_markdown", lambda r: "report")
    planned, seen = [], []
    def plan(problem, config):
        planned.append((problem, config.planner_model))
        return _brief(needs_confirmation)
    def research(query, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(draft="Answer", draft_rejected=False, delivery_status="delivered",
                               stop_reason="finished", cards=[], sources=[], calls=[], usage={}, warnings=[])
    brief_config = {"planner_model": "planner-x", "planner_provider": "p", "planner_effort": "low", "max_initial_tasks": 8}
    result = runner.run_one({"id": 1, "problem": "Original question", "problem_category": "T"}, tmp_path,
                            SimpleConfig(), run_fn=research, brief_config=brief_config, plan_fn=plan)
    assert planned == [("Original question", "planner-x")] and result["status"] == "completed"
    kwargs = seen[0]
    assert [r.id for r in kwargs["requirements"]] == ["req-goal", "req-src"]
    assert [i["id"] for i in kwargs["initial_route"]] == ["route-1", "route-2"]
    assert "未经用户确认" in kwargs["brief_context"] and "用户确认的研究目标" not in kwargs["brief_context"]
    assert "Read the report as published" in kwargs["brief_context"] and "Use the On Time measure" in kwargs["brief_context"]
    saved = json.loads((tmp_path / "plan.json").read_text())
    assert saved["planner_needs_confirmation"] is needs_confirmation and saved["planner"] == brief_config
    assert result["preflight_planning"] == runner.PLANNING


def test_planning_failure_is_an_error_not_a_silent_unplanned_run(modules, monkeypatch, tmp_path):
    runner = modules[1]
    from dra.simple_research import SimpleConfig
    def plan(problem, config):
        raise ValueError("规划没有返回可用结果，未开始研究")
    result = runner.run_one({"id": 2, "problem": "q"}, tmp_path, SimpleConfig(),
                            run_fn=lambda *a, **k: pytest.fail("must not research"),
                            brief_config={"planner_model": "m", "planner_provider": "p"}, plan_fn=plan)
    assert result["status"] == "error" and "规划没有返回可用结果" in result["planning_error"]
    assert not (tmp_path / "plan.json").exists()


def test_without_brief_config_research_receives_only_the_question(modules, monkeypatch, tmp_path):
    runner = modules[1]
    from dra.simple_research import SimpleConfig
    monkeypatch.setattr(runner.simple_research, "render_markdown", lambda r: "report")
    seen = []
    def research(query, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(draft="A", draft_rejected=False, delivery_status="delivered",
                               stop_reason="finished", cards=[], sources=[], calls=[], usage={}, warnings=[])
    result = runner.run_one({"id": 3, "problem": "q"}, tmp_path, SimpleConfig(), run_fn=research)
    assert not {"requirements", "brief_context", "initial_route"} & set(seen[0])
    assert result["preflight_planning"] == runner.NO_PLANNING


def test_research_config_runs_code_like_the_web(modules):
    runner = modules[1]
    from dra import runtime_config
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'f', 'model': 'f'}, subagent={'provider': 'f', 'model': 'f'},
                                                   execution_enabled=True, execution_backend='docker')
    config = runner.research_config(settings, 1200)
    assert config.execution == runtime_config.execution_config(settings) and config.execution.enabled
    assert config.wall_timeout_s == 1200 and not config.browser.enabled


def test_bash_leak_audit_flags_commands_and_outputs_without_blocking(modules, tmp_path):
    runner = modules[1]
    (tmp_path / 'execution/logs').mkdir(parents=True)
    (tmp_path / 'execution/logs/exec_1.stdout').write_text('ordinary table output')
    (tmp_path / 'execution/logs/exec_2.stdout').write_text('downloaded DSQA-full.csv rows')
    (tmp_path / 'tool_calls.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'tool': 'bash', 'tool_call_id': 'a', 'phase': 'requested', 'arguments_raw': '{"command": "curl https://orr.gov.uk"}'},
        {'tool': 'bash', 'tool_call_id': 'b', 'phase': 'requested', 'arguments_raw': '{"command": "curl huggingface.co/datasets/google/deepsearchqa"}'},
        {'tool': 'read_source', 'tool_call_id': 'c', 'arguments_raw': 'deepsearchqa'}]))
    hits = runner.bash_leak_audit(tmp_path)
    assert {h.get('tool_call_id') or h.get('file') for h in hits} == {'b', 'exec_2.stdout'}
    assert runner.bash_leak_audit(tmp_path / 'missing') == []
