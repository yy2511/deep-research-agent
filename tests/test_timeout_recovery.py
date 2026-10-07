"""Timeout ownership, reserved delivery, and capacity-aware failed-condense fallback."""
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from dra import llm, simple_research as sr
from dra.context_compaction import ContextLimit, compact_context
from test_context_compaction import setup_context, summary
from test_budget_wrapup import _doc, _turn, _text_turn
from test_simple_condense import LONG_BODY, TAIL


class Clock:
    def __init__(self):
        self.now = 1000.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def test_summary_retry_gets_fresh_deadline_and_keeps_identical_input(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(sr.time, 'sleep', clock.sleep)
    ctx, _, _, messages, budget = setup_context(tmp_path)
    budget.deadline = 2200
    requests, deadlines = [], []
    original_set = llm.set_request_deadline
    def bind(deadline):
        deadlines.append(deadline)
        return original_set(deadline)
    monkeypatch.setattr(llm, 'set_request_deadline', bind)
    def model(messages, **kwargs):
        requests.append(deepcopy(messages))
        assert kwargs['request_timeout_s'] == 120 and kwargs['max_retries'] == 0
        if len(requests) == 1:
            clock.now += 120
            raise llm.LLMDeadlineExceeded('first attempt reached local deadline')
        return summary()
    compact_context(ctx, messages, [], budget, call_model=model)
    assert requests[0] == requests[1]
    assert deadlines[:2] == [1120, 1241]
    assert ctx.compaction_checkpoints
    calls = ctx.store.state['calls']
    assert calls[0]['attempts'] == 2 and calls[0]['usage_unknown']
    document = json.loads((tmp_path/'context'/'lead'/f'{ctx.compaction_checkpoints[0]}.json').read_text())
    assert [a['status'] for a in document['calls'][0]['attempts']] == ['failed', 'returned']


@pytest.mark.parametrize('kind', ['both_timeout', 'http401', 'research_deadline'])
def test_failed_summary_keeps_history_and_stops_retry_when_required(tmp_path, monkeypatch, kind):
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(sr.time, 'sleep', clock.sleep)
    ctx, _, _, messages, budget = setup_context(tmp_path)
    before = deepcopy(ctx.message_view.chunks)
    budget.deadline = 1250 if kind == 'research_deadline' else 2200
    attempts = []
    def model(*args, **kwargs):
        attempts.append(1)
        if kind == 'http401':
            error = APIStatusError('unauthorized', response=httpx.Response(401, request=httpx.Request('POST', 'https://example.com')), body={})
            raise RuntimeError('wrapped upstream error') from error
        clock.now += 130
        raise llm.LLMRequestTimeout('timeout')
    with pytest.raises(ContextLimit):
        compact_context(ctx, messages, [], budget, call_model=model)
    assert len(attempts) == (2 if kind == 'both_timeout' else 1)
    assert ctx.message_view.chunks == before and not ctx.compaction_checkpoints


@pytest.mark.parametrize('finish', ['boundary', 'exception', 'http_timeout'])
def test_research_deadline_reserves_delivery_and_does_not_search_again(tmp_path, monkeypatch, finish):
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    cfg = sr.SimpleConfig(wall_timeout_s=1200, wrapup_reserve_s=120, delegation_enabled=False)
    calls, searched, bound_deadlines = [], [], []
    original_set = llm.set_request_deadline
    def bind(deadline):
        bound_deadlines.append(deadline)
        return original_set(deadline)
    monkeypatch.setattr(llm, 'set_request_deadline', bind)
    def model(messages, **kwargs):
        names = [t['function']['name'] for t in kwargs['tools']]
        calls.append(names)
        if names == ['finish_research']:
            assert clock.now == 2080
            return _text_turn('现有材料不足，尚未确认最终结论。')
        clock.now = 2080
        if finish == 'exception':
            raise llm.LLMDeadlineExceeded('research deadline')
        if finish == 'http_timeout':
            raise llm.LLMRequestTimeout('HTTP timed out at research deadline')
        return _turn(('search_sources', {'query':'should not execute'}))
    result = sr.run_simple_research('q', config=cfg, run_dir=tmp_path,
        model_call=model, search_fn=lambda q: searched.append(q) or [_doc()])
    assert result.stop_reason == 'research_time+wrapup'
    assert result.draft and not searched and len(calls) == 2
    assert 2080 in bound_deadlines and bound_deadlines[-1] == 2200


def test_total_deadline_never_starts_delivery(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    calls = []
    def model(*args, **kwargs):
        calls.append(1)
        clock.now = 2200
        raise llm.LLMDeadlineExceeded('total deadline')
    result = sr.run_simple_research('q', run_dir=tmp_path, model_call=model,
        config=sr.SimpleConfig(delegation_enabled=False))
    assert result.stop_reason == 'deadline' and len(calls) == 1 and not result.draft


@pytest.mark.parametrize('reason, expected', [('research_time', 'timed_out'),
                                             ('research_time+wrapup', 'completed')])
def test_child_research_cutoff_distinguishes_delivery_from_timeout(reason, expected):
    from dra.research_delegation import DelegationManager
    manager = DelegationManager(lambda task, cancel: {'status': reason}, lambda event: None)
    task = manager.submit([('child question', '')], 'dispatch')[0]
    manager.close(cancel=False)
    assert manager.inspect(task['id'])[0]['status'] == expected


def test_failed_page_condense_is_cached_and_unseen_tail_cannot_be_cited(tmp_path):
    calls = []
    raw = LONG_BODY * 20 + '\nUnique tail condition: only version 7.\n'
    def model(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('page condense failed')
    cfg = sr.SimpleConfig(compaction_enabled=True, condense_over_chars=1000, context_window_tokens=5000,
        max_output_tokens=512, estimate_chars_per_token=1)
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id='fallback', query='q'), cfg, condense_fn=model)
    ctx.store.write_source(_doc(raw=raw))
    receipt, ok = ctx.read_source({'source_ref':'s1'})
    data = json.loads(receipt);ref=data['source_ref']
    assert ok and data['view'] == 'read_required' and not data['excerpts']
    assert data['total_chars'] == len(raw) and data['condense_completed_ranges'] == []
    assert 'find' in data['read_back'] and ref not in ctx.read_exposures
    assert not ctx.save_evidence({'cards':[{'claim':'condition', 'source_ref':ref, 'quote':'Unique tail condition: only version 7.'}]})[1]
    assert json.loads(ctx.read_source({'source_ref':ref})[0])['view'] == 'read_required'
    assert len(calls) == 1
    assert ctx.read_source({'source_ref':ref, 'find':'Unique tail condition'})[1]
    assert ctx.save_evidence({'cards':[{'claim':'condition', 'source_ref':ref, 'quote':'Unique tail condition: only version 7.'}]})[1]
    # Recovered room can return the entire cached original without another condense.
    ctx.config.context_window_tokens = 100000
    whole = json.loads(ctx.read_source({'source_ref':ref})[0])
    assert whole['view'] == 'original' and whole['excerpts'][0]['quote'] == raw
    assert len(calls) == 1


def test_compaction_off_returns_full_original_after_page_condense_failure(tmp_path):
    calls = []
    raw = LONG_BODY * 20 + '\nUnique tail condition: only version 7.\n'
    def model(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('page condense failed')
    cfg = sr.SimpleConfig(compaction_enabled=False, condense_over_chars=1000,
        context_window_tokens=5000, max_output_tokens=512, estimate_chars_per_token=1)
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id='fallback', query='q'), cfg, condense_fn=model)
    ctx.store.write_source(_doc(raw=raw))
    receipt, ok = ctx.read_source({'source_ref':'s1'})
    data = json.loads(receipt)
    assert ok and data['view'] == 'original' and data['excerpts'][0]['quote'] == raw
    assert ctx.save_evidence({'cards':[{'claim':'condition', 'source_ref':'s1',
        'quote':'Unique tail condition: only version 7.'}]})[1]
    assert json.loads(ctx.read_source({'source_ref':'s1'})[0])['excerpts'][0]['quote'] == raw
    assert len(calls) == 1


def test_partial_page_condense_keeps_completed_ranges_and_tail_entry():
    from dra.condense import condense_text
    count = 0
    def model(messages, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise TimeoutError('second chunk')
        return {'summary':'first block', 'excerpts':['AAAA']}
    result = condense_text('q', 'AAAA\nBBBB\nCCCC\n', call_json=model, max_input_chars=5)
    assert result['condense_completed_ranges'] == ['chars=0-5']
    assert result['condense_input_ranges'] == ['chars=0-5', 'chars=5-10']
    assert result['excerpts'] == ['AAAA'] and result['error'] == 'TimeoutError'


def test_search_retry_uses_remaining_research_time(monkeypatch):
    from dra import tools
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(sr.time, 'sleep', clock.sleep)
    timeouts = []
    def search(**kwargs):
        timeouts.append(kwargs['timeout'])
        clock.now += kwargs['timeout']
        raise TimeoutError('search timeout')
    monkeypatch.setattr(tools, '_get_client', lambda: SimpleNamespace(search=search))
    with pytest.raises(TimeoutError, match='研究时间已到'):
        tools.web_search('q', deadline=1002)
    assert timeouts == [2]
