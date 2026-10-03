"""Profile selection leaves product defaults intact and reaches both normal and wrapup calls."""
from copy import deepcopy

import pytest
from dra import research_prompts as prompts, simple_research as sr
from test_budget_wrapup import _turn


@pytest.mark.parametrize('wrapup', [False, True])
@pytest.mark.parametrize('profile', ['product', 'concise_qa_v1', 'concise_qa_v2'])
def test_profile_reaches_real_model_call_without_mutating_defaults(tmp_path, profile, wrapup):
    before = deepcopy(sr.LEAD_TOOL_SCHEMAS)
    calls = []
    def model(messages, **kwargs):
        calls.append((messages, kwargs))
        return _turn(('finish_research', {'draft': '', 'answers': [], 'coverage': [], 'gaps': ['No evidence.']}))
    config = sr.SimpleConfig(prompt_profile=profile, max_llm_calls=1 if wrapup else None)
    sr.run_simple_research('Question', sr.requirements_from_query('Question'), config=config,
                           run_dir=tmp_path, model_call=model, search_fn=lambda _: [])
    assert calls
    system = calls[0][0][0]['content']
    finish = next(s['function'] for s in calls[0][1]['tools'] if s['function']['name'] == 'finish_research')
    if profile == 'product':
        assert system.startswith(sr._WRAPUP_SYSTEM if wrapup else sr._LEAD_SYSTEM)
        assert '因果' in finish['description']
    else:
        assert ('answers 数组' if profile == 'concise_qa_v2' else '<final_answer>') in system
        assert '因果、比较与总结等分析' not in system + finish['description']
        assert sr._EVIDENCE_USE_INSTRUCTIONS in system
        if profile == 'concise_qa_v2':
            assert 'draft' not in finish['parameters']['properties']
            assert 'answers' in finish['parameters']['required']
        else:
            assert '[cN]' in finish['parameters']['properties']['draft']['description']
    assert sr.LEAD_TOOL_SCHEMAS == before
    assert sr.SimpleConfig().prompt_profile == 'product'


def test_children_inherit_profile_but_submit_discoveries():
    config = sr.SimpleConfig(prompt_profile='concise_qa_v1').model_copy(update={'max_llm_calls': 8})
    prompt = prompts.system_prompt(config.prompt_profile, 'subagent', sr._SUBAGENT_SYSTEM,
                                   evidence=sr._EVIDENCE_USE_INSTRUCTIONS, steering='', wrapup=True)
    assert '只完成分配的子任务' in prompt and '<final_answer>' not in prompt
    assert '只能调用一次 finish_research' in prompt


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
