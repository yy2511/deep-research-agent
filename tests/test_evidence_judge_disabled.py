"""Disabled JEV requests still enforce original quotes and valid citations."""
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from dra import evidence_judge, simple_research as sr
from test_budget_wrapup import _turn
from test_evidence_judge import context


@pytest.mark.parametrize('role', ['lead', 'subagent'])
@pytest.mark.parametrize('wrapup', [False, True])
@pytest.mark.parametrize('profile', ['product', 'concise_qa_v1', 'concise_qa_v2'])
def test_disabled_request_describes_actual_mode(tmp_path, role, wrapup, profile):
    config = sr.SimpleConfig(prompt_profile=profile, max_llm_calls=1 if wrapup else 8)
    store = sr.RunStore(tmp_path, run_id='disabled', query='Question')
    budget = sr.Budget(config, None)
    ctx = sr.ToolContext(store, config, budget=budget)
    if role == 'subagent':
        ctx = ctx.fork_for_subagent(config, budget=budget, delegation_id='sub1')
    seen = []
    def model(messages, **kwargs):
        seen.append((deepcopy(messages), kwargs))
        return _turn(('finish_research', {'draft': '', 'answers': [], 'coverage': [], 'gaps': ['No evidence']}))
    sr.run_lead_loop(store, config, [], budget=budget, context=ctx, model_call=model, role=role,
        system_prompt=sr._SUBAGENT_SYSTEM if role == 'subagent' else None,
        schemas=sr.SUBAGENT_TOOL_SCHEMAS if role == 'subagent' else None)
    system = seen[0][0][0]['content']
    assert 'JEV 核验已关闭' in system and '无需等待 judgment' in system
    for stale in ('Jev 是系统自动调用', '交给 Jev', 'relation=supported', '再看新卡的 Jev', '该卡核验未通过'):
        assert stale not in system
    if not wrapup:
        save = next(t['function'] for t in seen[0][1]['tools'] if t['function']['name'] == 'save_evidence')
        assert '不调用 JEV' in save['description'] and '任一段匹配失败整卡不保存' in save['description']


@pytest.mark.parametrize('role', ['lead', 'subagent'])
@pytest.mark.parametrize('arm', ['original', 'claude', 'v2'])
@pytest.mark.parametrize('wrapup', [False, True])
def test_frozen_prompt_hook_cannot_restore_jev_instructions(tmp_path, monkeypatch, arm, wrapup, role):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'eval/deepsearchqa'))
    comparison = importlib.import_module('prompt_comparison')
    path = root / f'eval/deepsearchqa/experiments/hard50-20261004/prompts/{arm}.json'
    before = path.read_bytes()
    bundle = json.loads(before)
    seen = []
    def model(messages, **kwargs):
        seen.append((messages, kwargs))
        return _turn(('finish_research', {'draft': '', 'answers': [], 'coverage': [], 'gaps': ['No evidence']}))
    with comparison.installed_bundle(bundle):
        config = sr.SimpleConfig(prompt_profile=bundle['profile'], max_llm_calls=1 if wrapup else 8)
        store = sr.RunStore(tmp_path, run_id='frozen-disabled', query='Question')
        budget = sr.Budget(config, None)
        ctx = sr.ToolContext(store, config, budget=budget)
        if role == 'subagent':
            ctx = ctx.fork_for_subagent(config, budget=budget, delegation_id='sub1')
        sr.run_lead_loop(store, config, [], context=ctx, budget=budget, model_call=model, role=role,
            system_prompt=sr._SUBAGENT_SYSTEM if role == 'subagent' else None,
            schemas=sr.SUBAGENT_TOOL_SCHEMAS if role == 'subagent' else None)
    system = seen[0][0][0]['content']
    assert 'JEV 核验已关闭' in system
    assert '交给 Jev' not in system and 'relation=supported' not in system and '再看新卡的 Jev' not in system
    if not wrapup:
        save = next(t['function'] for t in seen[0][1]['tools'] if t['function']['name'] == 'save_evidence')
        assert '交给 Jev' not in save['description'] and '不调用 JEV' in save['description']
    assert path.read_bytes() == before


def test_disabled_save_and_wrapup_preserve_quote_and_citation_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_judge, 'judge_cards', lambda *a, **kw: pytest.fail('JEV called'))
    ctx, ref = context(tmp_path, enabled=False)
    receipt, ok = ctx.save_evidence({'cards': [
        {'claim': '室内使用', 'source_ref': ref, 'quote': '产品仅在室内使用。'},
        {'claim': '室外使用', 'source_ref': ref, 'quote': '产品可以在室外使用。'}]})
    payload = json.loads(receipt)
    assert ok and [r['accepted'] for r in payload['results']] == [True, False]
    assert 'JEV 核验已关闭' in payload['note'] and '未完成核验不能视为通过' not in payload['note']
    assert ctx.store.cards()[0].judgment is None
    assert sr.citation_gate('室内使用。[c1]', ctx.store.cards())[0]
    assert not sr.citation_gate('室内使用。[c999]', ctx.store.cards())[0]
    seen = []
    def model(messages, **kwargs):
        seen.append(messages)
        return _turn(('finish_research', {'draft': '室内使用。[c1]', 'coverage': [], 'gaps': []}))
    result = sr._wrapup_turn(ctx.store, ctx.config, [], ctx, model_call=model,
                             budget=sr.Budget(ctx.config, None))
    assert result[0] == '室内使用。[c1]'
    assert 'Jev：未核验' not in seen[0][1]['content']
    assert '产品仅在室内使用。' in seen[0][1]['content']


def test_existing_judgments_remain_readable_without_requesting_new_jev(tmp_path):
    ctx, ref = context(tmp_path, enabled=False)
    ctx.save_evidence({'cards': [{'claim': '室内使用', 'source_ref': ref, 'quote': '产品仅在室内使用。'}]})
    row = ctx.store.state['cards'][0]
    row['judgment'] = evidence_judge.Judgment(status='complete', relation='insufficient').model_dump()
    before = deepcopy(row)
    messages = sr._build_messages([], ctx, [], [], seconds_left=None)
    text = json.dumps(messages, ensure_ascii=False)
    assert '已有 JEV 核验记录' in text and 'insufficient' in text
    assert '须修正后重存' not in text and '无需等待新判定' in text
    assert row == before
