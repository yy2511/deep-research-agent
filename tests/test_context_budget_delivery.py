from copy import deepcopy
import json

import pytest
from dra import simple_research as sr
from dra.context_budget import assess, capacity
from dra.context_receipts import deliver, read_page, _path
from dra.models import RetrievedDoc


def ctx_at(tmp_path, **settings):
    cfg = sr.SimpleConfig(**settings)
    return sr.ToolContext(sr.RunStore(tmp_path, run_id='budget', query='find target'), cfg)


def test_real_output_reservation_and_stage_override(tmp_path):
    ctx = ctx_at(tmp_path, context_window_tokens=64000, max_output_tokens=32768,
                 summary_max_output_tokens=8192, wrapup_max_output_tokens=16384)
    assert capacity(ctx.config)['input_budget_tokens'] == 31232
    assert capacity(ctx.config, 'compact')['input_budget_tokens'] == 55808
    assert capacity(ctx.config, 'wrapup')['input_budget_tokens'] == 47616
    first = assess(ctx, [{'role':'user', 'content':'a' * 1000}])
    assert first['calibration'] == 'uncalibrated' and first['error_margin_tokens'] == 0
    ctx.note_usage_anchor(800, first['request_chars'])
    next_check = assess(ctx, [{'role':'user', 'content':'a' * 1000}])
    assert next_check['checked_input_tokens'] > next_check['estimated_input_tokens'] == 800
    assert assess(ctx, [], phase='wrapup')['calibration'] == 'uncalibrated'


def test_route_fields_take_minimum_never_catalog_or_model_alias(monkeypatch):
    from dra import provider_registry as pr
    data = {'connections': {}, 'discovery': {'relay': {'checked_at': 123,
        'models': {'model-alias': {'context_window': 48000, 'max_input_tokens': 20000,
                                  'max_output_tokens': 32768}}}},
        'overrides': {'relay': {'model-alias': {'context_window': 100000}}}}
    monkeypatch.setattr(pr, '_load', lambda: data)
    monkeypatch.setattr(pr, '_catalog', lambda: pytest.fail('catalog cannot prove route capacity'))
    caps = pr.capacity_snapshot('relay', 'model-alias')
    assert caps['context_window'] == 48000 and caps['checked_at'] == 123
    cfg = sr.SimpleConfig(adapter={'provider':'relay', 'model':'model-alias'}, context_window_mode='auto',
                          compaction_enabled=True, max_output_tokens=32768)
    assert capacity(cfg)['input_budget_tokens'] == 15232  # min(P, C-O), no second O subtraction
    assert pr.capacity_snapshot('other', 'model-alias')['context_window'] is None
    with pytest.raises(ValueError, match='容量未知'):
        sr.SimpleConfig(adapter={'provider':'other', 'model':'model-alias'},
                        context_window_mode='auto', compaction_enabled=True)


def test_paged_source_preserves_all_text_and_unread_permissions(tmp_path):
    ctx = ctx_at(tmp_path, compaction_enabled=True, context_window_tokens=5000,
                 max_output_tokens=512, estimate_chars_per_token=1, condense_over_chars=None)
    raw = ('Visible paragraph.\n\n' * 600) + 'HIDDEN-TARGET end condition.'
    ctx.store.write_source(RetrievedDoc(snippet='', title='long', source_url='https://example.com', raw_content=raw))
    before = deepcopy(ctx.read_exposures)
    result, ok = ctx.read_source({'source_ref':'s1'})
    assert ok
    full = json.loads(result)
    ctx.tool_page_request = ([], [], 'read')
    first = json.loads(deliver(ctx, 'read_source', result, before))
    assert first['next_cursor'] is not None
    assert assess(ctx, [{'role':'tool', 'tool_call_id':'read', 'content':json.dumps(first, ensure_ascii=False)}])['fits']
    _, saved = ctx.save_evidence({'cards':[{'claim':'target', 'source_ref':'s1', 'quote':'HIDDEN-TARGET end condition.'}]})
    assert not saved
    chunks = [first['text']]
    page = first
    while page['next_cursor'] is not None:
        body, ok = read_page(ctx, {'result_id':first['result_id'], 'cursor':page['next_cursor']})
        assert ok
        page = json.loads(body)
        assert page['shown_range'][1] > page['shown_range'][0]
        chunks.append(page['text'])
    assert json.loads(''.join(chunks)) == full
    assert json.loads(_path(ctx, first['result_id']).read_text())['original'] == result
    _, saved = ctx.save_evidence({'cards':[{'claim':'target', 'source_ref':'s1', 'quote':'HIDDEN-TARGET end condition.'}]})
    assert saved
    child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id='child')
    assert not read_page(child, {'result_id':first['result_id']})[1]


def test_wrapup_deduplicates_ranges_without_deleting_qualifiers(tmp_path):
    from dra.context_delivery import delivery_evidence
    ctx = ctx_at(tmp_path)
    ctx.store.write_source(RetrievedDoc(snippet='', title='fact', source_url='https://example.com',
        raw_content='Only for version 2.\n\nThe feature is supported.\n\nExcept on older systems.'))
    ctx.read_source({'source_ref':'s1'})
    for claim in ('support', 'compatibility'):
        assert ctx.save_evidence({'cards':[{'claim':claim, 'quote':'The feature is supported.'}]})[1]
    before = deepcopy(ctx.store.cards())
    rows = delivery_evidence(ctx.store.cards())
    assert rows[1][0]['quote_ref'] == rows[0][0]['evidence_id']
    assert 'Except on older systems.' in json.dumps(rows)
    assert ctx.store.cards() == before


def test_context_error_still_delivers_from_existing_cards(tmp_path):
    from test_budget_wrapup import _turn
    ctx = ctx_at(tmp_path, max_llm_calls=5, max_tool_calls=10, wall_timeout_s=60, wrapup_reserve_s=0,
                 max_output_tokens=512, wrapup_max_output_tokens=1024, context_window_tokens=64000,
                 delegation_enabled=False)
    ctx.store.write_source(RetrievedDoc(snippet='', title='fact', source_url='https://example.com', raw_content='The feature is supported.'))
    ctx.read_source({'source_ref':'s1'})
    ctx.save_evidence({'cards':[{'claim':'support', 'quote':'The feature is supported.'}]})
    seen = []
    def model(messages, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            raise RuntimeError('context_length_exceeded')
        return _turn(('finish_research', {'draft':'The feature is supported [c1].', 'coverage':[], 'gaps':[]}))
    result = sr.run_lead_loop(ctx.store, ctx.config, [], context=ctx, model_call=model, budget=sr.Budget(ctx.config, None))
    assert result['draft'] and result['stop_reason'] == 'context_limit+wrapup'
    assert [r['max_tokens'] for r in seen] == [512, 1024]


def test_oversized_wrapup_is_not_sent(tmp_path):
    ctx = ctx_at(tmp_path, compaction_enabled=True, context_window_tokens=2000, max_output_tokens=512)
    ctx.effective_query = 'current user requirement ' * 1000
    budget = sr.Budget(ctx.config, None)
    draft, _, _, note = sr._wrapup_turn(ctx.store, ctx.config, [], ctx,
        model_call=lambda *a, **k: pytest.fail('oversized request sent'), budget=budget)
    assert not draft and '收尾输入超过容量' in note


@pytest.mark.parametrize('role', ['lead', 'subagent'])
def test_compaction_off_sends_research_without_local_capacity_check(tmp_path, monkeypatch, role):
    from test_budget_wrapup import _turn
    ctx = ctx_at(tmp_path, compaction_enabled=False, context_window_tokens=2000,
        max_output_tokens=512, delegation_enabled=False)
    mission = '完整任务内容 ' * 2000
    ctx.effective_query = mission
    if role == 'subagent':
        ctx = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id='sub1')
    monkeypatch.setattr('dra.context_budget.assess',
        lambda *a, **k: pytest.fail('disabled compaction checked request capacity'))
    seen = []
    def model(messages, **kwargs):
        seen.append(deepcopy(messages))
        return _turn(('finish_research', {'draft':'已按完整任务交稿。', 'coverage':[], 'gaps':[]}))
    result = sr.run_lead_loop(ctx.store, ctx.config, [], context=ctx, model_call=model,
        budget=sr.Budget(ctx.config, None), role=role,
        scope='sub1' if role == 'subagent' else '', mission_text=mission)
    assert result['draft'] and result['stop_reason'] == 'finished'
    assert len(seen) == 1 and mission in seen[0][1]['content']
    assert all(call['status'] != 'not_sent' for call in ctx.store.state['calls'])


@pytest.mark.parametrize('role', ['lead', 'subagent'])
def test_compaction_off_sends_full_wrapup_without_local_capacity_check(tmp_path, monkeypatch, role):
    from test_budget_wrapup import _turn
    ctx = ctx_at(tmp_path, compaction_enabled=False, context_window_tokens=2000, max_output_tokens=512)
    mission = '完整交付材料 ' * 2000
    ctx.effective_query = mission
    monkeypatch.setattr('dra.context_budget.assess',
        lambda *a, **k: pytest.fail('disabled compaction checked wrapup capacity'))
    seen = []
    def model(messages, **kwargs):
        seen.append(deepcopy(messages))
        return _turn(('finish_research', {'draft':'基于现有材料交稿。', 'coverage':[], 'gaps':[]}))
    draft, _, _, _ = sr._wrapup_turn(ctx.store, ctx.config, [], ctx,
        model_call=model, budget=sr.Budget(ctx.config, None), role=role, mission_text=mission)
    assert draft and len(seen) == 1 and mission in seen[0][1]['content']
    assert all(call['status'] != 'not_sent' for call in ctx.store.state['calls'])


def test_all_rejections_persist_and_new_save_resolves_only_matching_item(tmp_path):
    ctx = ctx_at(tmp_path)
    ctx.store.write_source(RetrievedDoc(snippet='', title='fact', source_url='https://example.com', raw_content='A verified fact.'))
    ctx.read_source({'source_ref':'s1'})
    cards = [{'claim':f'claim {i}', 'source_ref':'s1', 'excerpt_no':99+i} for i in range(6)]
    _, ok = ctx.save_evidence({'cards': cards})
    assert not ok and len(ctx.rejection_items) == 6
    assert ctx.store.state['unresolved_evidence_feedback']['lead'] == ctx.rejection_items
    ctx.save_evidence({'cards':[{'claim':'claim 2', 'source_ref':'s1', 'quote':'A verified fact.'}]})
    assert len(ctx.rejection_items) == 5
    assert all(row['submitted']['claim'] != 'claim 2' for row in ctx.rejection_items.values())


def test_condense_chunks_use_actual_stage_budget_and_cover_tail(tmp_path):
    from dra.condense import condense_text
    ctx = ctx_at(tmp_path, context_window_tokens=6000, max_output_tokens=4096, summary_max_output_tokens=512)
    text = ('a continuous original passage.\n' * 1100) + 'FINAL LIMITATION'
    seen = []
    def compress(messages, **kwargs):
        assert assess(ctx, messages, phase='condense')['fits']
        body = messages[1]['content'].split('【网页正文】\n', 1)[1]
        seen.append(body)
        return {'summary':'summary', 'excerpts':[body]}
    data = condense_text('query', text, call_json=compress,
        fits_request=lambda req:assess(ctx, req, phase='condense')['fits'])
    assert ''.join(seen) == text and not data['condense_input_truncated']
    assert data['excerpts'][-1].endswith('FINAL LIMITATION')
