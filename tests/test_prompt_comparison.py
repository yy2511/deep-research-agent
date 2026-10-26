"""实验隔离：固定困难题、冻结提示词覆盖真实调用、保留交稿权限。"""
from copy import deepcopy
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import json

import pytest
from dra import research_prompts, simple_research as sr
from test_budget_wrapup import _turn


@pytest.fixture
def comparison(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "eval/deepsearchqa"))
    return importlib.import_module("prompt_comparison")


def test_fixed_nonperfect_pool_keeps_empty_answers_and_nested_subset(comparison):
    rows = [{"id": i, "response": "" if i % 2 else "Answer"} for i in range(200)]
    grades = {str(i): {"f1": 1 if i % 5 == 0 else 0.5} for i in range(200)}
    first = comparison.select_questions(rows, grades, 20261004)
    second = comparison.select_questions(list(reversed(rows)), grades, 20261004)
    assert first == second
    assert len(first) == 160 and all(i % 5 != 0 for i in first)
    assert any(i % 2 for i in first[:50])
    assert first[:100][:50] == first[:50]


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "eval/deepsearchqa/runs/concise-v2-20261003").exists(),
                    reason="需要本机旧批次 concise-v2-20261003 及其冻结代码快照（运行记录不入库）")
def test_new_comparison_disables_inherited_jev_without_rewriting_old_batch(comparison, tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'manifest.json').write_text(json.dumps({'records': [{'id': 68, 'response': ''}],
                                                    'judge': {'model': 'fixture', 'provider': 'fixture'}}))
    (source / 'results.json').write_text(json.dumps({'grades': {'68': {'f1': 0}}}))
    old = comparison.HERE / 'runs/concise-v2-20261003/manifest.json'
    old_bytes = old.read_bytes()
    monkeypatch.setattr(comparison, 'load_questions', lambda: [{'id': 68, 'problem': 'Question',
                                                               'problem_category': 'fixture'}])
    monkeypatch.setattr(comparison.provider_registry, 'status', lambda _: SimpleNamespace(
        model_dump=lambda: {'name': 'fixture', 'base_url': 'https://example.org'}))
    target = tmp_path / 'new'
    comparison.prepare(target, source, size=1, seed=1, provider='fixture', model='fixture', workers=1)
    config = comparison.read(target / 'manifest.json')['research_config']
    assert config['evidence_judge']['enabled'] is False
    for arm in comparison.ARMS:
        assert (target / 'prompts' / f'{arm}.json').read_bytes() == (
            comparison.FROZEN_BASELINE / 'prompts' / f'{arm}.json').read_bytes()
    assert old.read_bytes() == old_bytes


def test_frozen_runtime_uses_project_web_credentials(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, DRA_COMPARISON_PROJECT=str(tmp_path),
               DRA_DATA_DIR=str(tmp_path),
               PYTHONPATH=os.pathsep.join([str(root / "src"), str(root / "eval/deepsearchqa")]))
    subprocess.run([sys.executable, "-c", "\n".join([
        "import prompt_comparison",
        "from dra import llm, paths, tools",
        "assert tools._ENV_PATH == paths.PROJECT_ROOT / '.env'",
        "assert llm._ENV_PATH == tools._ENV_PATH",
    ])], env=env, check=True, capture_output=True, text=True)


@pytest.mark.parametrize("wrapup", [False, True])
@pytest.mark.parametrize("profile", ["product", "concise_qa_v2"])
def test_frozen_bundle_reaches_model_and_restores_live_product(comparison, tmp_path, profile, wrapup):
    bundle = comparison.prompt_bundle(sr, profile, modern=True)
    marker = "FROZEN STUDY PROMPT"
    bundle["systems"]["lead"]["wrapup" if wrapup else "normal"] = marker
    seen = []

    def model(messages, **kwargs):
        seen.append((messages, kwargs))
        return _turn(("finish_research", {"draft": "", "answers": [], "coverage": [], "gaps": ["No evidence"]}))

    original_system, original_tools = research_prompts.system_prompt, research_prompts.tool_schemas
    schemas_before = deepcopy(sr.LEAD_TOOL_SCHEMAS)
    with comparison.installed_bundle(bundle):
        sr.run_simple_research("Question only", sr.requirements_from_query("Question only"),
            config=sr.SimpleConfig(prompt_profile=profile, max_llm_calls=1 if wrapup else 3),
            run_dir=tmp_path, model_call=model, search_fn=lambda _: [])
    assert seen[0][0][0]["content"].startswith(marker)
    finish = next(s["function"] for s in seen[0][1]["tools"] if s["function"]["name"] == "finish_research")
    assert ("answers" in finish["parameters"]["properties"]) == (profile == "concise_qa_v2")
    if wrapup:
        assert len(seen[0][1]["tools"]) == 1
    assert research_prompts.system_prompt is original_system
    assert research_prompts.tool_schemas is original_tools
    assert schemas_before == sr.LEAD_TOOL_SCHEMAS


def test_child_and_wrapup_use_their_own_frozen_text(comparison):
    bundle = comparison.prompt_bundle(sr, "product", modern=True)
    with comparison.installed_bundle(bundle):
        assert research_prompts.system_prompt("product", "subagent", "ignored", evidence="ignored",
                    steering="ignored", wrapup=True) == bundle["systems"]["subagent"]["wrapup"]
