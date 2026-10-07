"""Profile selection leaves product defaults intact and reaches both normal and wrapup calls."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from dra import research_prompts as prompts, simple_research as sr
from test_budget_wrapup import _turn


def test_default_product_systems_match_selected_claude_winner():
    baseline = Path(__file__).resolve().parents[1] / 'eval/deepsearchqa/experiments/hard50-20261004/prompts/claude.json'
    systems = json.loads(baseline.read_text())['systems']
    assert sr.SimpleConfig().prompt_profile == 'product'
    assert sr._lead_system(delegation=True, execution=False) == systems['lead']['normal']
    assert sr._SUBAGENT_SYSTEM == systems['subagent']['normal']
    for role in ('lead', 'subagent'):
        assert sr._wrapup_system(role=role, delegation=True) == systems[role]['wrapup']


@pytest.mark.parametrize('wrapup', [False, True])
@pytest.mark.parametrize('profile', ['product', 'concise_qa_v1', 'concise_qa_v2'])
@pytest.mark.parametrize('delegation', [False, True])
def test_profile_reaches_real_model_call_without_mutating_defaults(tmp_path, profile, wrapup, delegation):
    before = deepcopy(sr.LEAD_TOOL_SCHEMAS)
    calls = []
    def model(messages, **kwargs):
        calls.append((messages, kwargs))
        return _turn(('finish_research', {'draft': '', 'answers': [], 'coverage': [], 'gaps': ['No evidence.']}))
    # 空正文会被打回；非收尾组给 3 次调用，打回后由调用额度收尾结束。
    config = sr.SimpleConfig(prompt_profile=profile, max_llm_calls=1 if wrapup else 3,
                             delegation_enabled=delegation, evidence_judge={'enabled': True})
    sr.run_simple_research('Question', sr.requirements_from_query('Question'), config=config,
                           run_dir=tmp_path, model_call=model, search_fn=lambda _: [])
    assert calls
    system = calls[0][0][0]['content']
    finish = next(s['function'] for s in calls[0][1]['tools'] if s['function']['name'] == 'finish_research')
    request = '\n'.join(m.get('content') or '' for m in calls[0][0])
    if not delegation or wrapup:
        assert 'wait_research' not in system and 'delegate_research' not in system
    if profile == 'product':
        assert system.startswith(sr._live_contract_prompt(
            sr._wrapup_system(role='lead', delegation=delegation) if wrapup else
            sr._lead_system(delegation=delegation)))
        assert '因果' in finish['description']
    else:
        assert ('answers 数组' if profile == 'concise_qa_v2' else '<final_answer>') in system
        assert '因果、比较与总结等分析' not in system + finish['description']
        assert sr._live_contract_prompt(sr._WRAPUP_EVIDENCE_INSTRUCTIONS if wrapup else sr._EVIDENCE_USE_INSTRUCTIONS) in system
    if wrapup:
        # 收尾只剩一次交稿：不能再要求补读、重存或调用其他工具。
        assert '重新 save_evidence' not in system and 'update_research_plan 的 scope_update' not in system
        assert '先检索取证' not in request
        assert '先判断是否需要分工' not in system and '建立简短任务清单' not in system
        assert 'read_source' not in system and 'save_evidence' not in system and 'inspect_research' not in system
        assert [s['function']['name'] for s in calls[0][1]['tools']] == ['finish_research']
        if profile == 'concise_qa_v2':
            assert 'draft' not in finish['parameters']['properties']
            assert 'answers' in finish['parameters']['required']
        else:
            assert '[cN]' in finish['parameters']['properties']['draft']['description']
    assert sr.LEAD_TOOL_SCHEMAS == before
    assert sr.SimpleConfig().prompt_profile == 'product'


@pytest.mark.parametrize('profile', ['product', 'concise_qa_v1', 'concise_qa_v2'])
@pytest.mark.parametrize('wrapup', [False, True])
def test_child_requests_only_include_child_stage_instructions(tmp_path, profile, wrapup):
    config = sr.SimpleConfig(prompt_profile=profile, max_llm_calls=1 if wrapup else 8)
    store = sr.RunStore(tmp_path, run_id='child-prompt', query='parent question')
    ctx = sr.ToolContext(store, config)
    budget = sr.Budget(config, deadline=None)
    child = ctx.fork_for_subagent(config, budget=budget, delegation_id='sub1')
    calls = []

    def model(messages, **kwargs):
        calls.append((messages, kwargs))
        return _turn(('finish_research', {'draft': '已确认的发现', 'gaps': []}))

    sr.run_lead_loop(store, config, [sr.Requirement(id='sub', text='child goal', kind='goal')],
                     budget=budget, context=child, model_call=model, role='subagent', scope='sub1',
                     system_prompt=sr._SUBAGENT_SYSTEM, mission_text='child goal',
                     schemas=sr.SUBAGENT_TOOL_SCHEMAS)
    messages, kwargs = calls[0]
    system = messages[0]['content']
    request = '\n'.join(m.get('content') or '' for m in messages)
    assert '【子任务目标】child goal' in request and 'parent question' not in request
    for unavailable in ('wait_research', 'delegate_research', 'update_research_plan'):
        assert unavailable not in system
        assert unavailable not in {s['function']['name'] for s in kwargs['tools']}
    assert '建立简短任务清单' not in system
    assert '<final_answer>' not in system and 'answers 数组' not in system
    if wrapup:
        assert '只能调用一次 finish_research' in system
        assert '先检索取证' not in request
        assert 'read_source' not in system and 'save_evidence' not in system
        assert [s['function']['name'] for s in kwargs['tools']] == ['finish_research']
    else:
        assert 'read_source' in system and 'save_evidence' in system


def test_structured_answer_has_no_freeform_draft_escape():
    assert prompts.structured_draft({'answers': [{'answer': 'Alpha — 2020', 'citations': ['c1', 'c3']}],
                                      'draft': 'Unwanted background'}) == '<final_answer>\n- Alpha — 2020 [c1][c3]\n</final_answer>'
    for bad in ({'draft': 'A'}, {'answers': [{'answer': 'A', 'citations': []}]},
                {'answers': [{'answer': 'A', 'citations': ['made up']}]}):
        with pytest.raises(ValueError):
            prompts.structured_draft(bad)


def test_structured_answer_still_requires_real_citations(tmp_path):
    from test_simple_research import _full_sequence, _run
    from dra.evidence_judge import JudgeConfig
    for citation, rejected in [('c1', False), ('c999', True)]:
        result, _ = _run(_full_sequence(),
                         config=sr.SimpleConfig(prompt_profile='concise_qa_v2', max_llm_calls=8,
                                                evidence_judge=JudgeConfig(enabled=False)),
                         run_dir=tmp_path / citation,
                         finish=('finish_research', {'answers': [{'answer': '支持 SSH 同步', 'citations': [citation]}],
                                                     'coverage': [{'requirement_id': 'goal', 'status': 'answered'}]}))
        assert result.draft_rejected is rejected
        if not rejected:
            assert result.draft == '<final_answer>\n- 支持 SSH 同步 [c1]\n</final_answer>'
