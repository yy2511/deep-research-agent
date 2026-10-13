"""Timeout ownership, near-deadline reminder, and capacity-aware failed-condense fallback."""
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from dra import llm, simple_research as sr
from dra.context_compaction import ContextLimit, compact_context
from test_context_compaction import setup_context, summary
from test_budget_wrapup import RAW, _doc, _turn, _text_turn
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


@pytest.mark.parametrize('kind', ['both_timeout', 'http401', 'deadline'])
def test_failed_summary_keeps_history_and_stops_retry_when_required(tmp_path, monkeypatch, kind):
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(sr.time, 'sleep', clock.sleep)
    ctx, _, _, messages, budget = setup_context(tmp_path)
    before = deepcopy(ctx.message_view.chunks)
    budget.deadline = 1130 if kind == 'deadline' else 2200
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


QUOTE = "The sqlite3_rsync utility works fine if both ORIGIN and REPLICA are local."


def test_near_deadline_reminds_but_keeps_tools_for_saving_and_finishing(tmp_path, monkeypatch):
    """研究阶段最后一段只提醒存证：检索、阅读、存证照常执行；研究请求以研究截止为限，不延长总截止。"""
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    cfg = sr.SimpleConfig(wall_timeout_s=1200, wrapup_reserve_s=120, delegation_enabled=False)
    seen, searched, bound_deadlines = [], [], []
    original_set = llm.set_request_deadline
    def bind(deadline):
        bound_deadlines.append(deadline)
        return original_set(deadline)
    monkeypatch.setattr(llm, 'set_request_deadline', bind)
    turns = [_turn(('search_sources', {'query': 'sqlite3_rsync local'})),
             _turn(('read_source', {'source_ref': 's1'})),
             _turn(('save_evidence', {'cards': [{'claim': '两端都在本机也可同步', 'quote': QUOTE}]})),
             _turn(('finish_research', {'draft': '两端都在本机也能同步 [c1]。',
                    'coverage': [{'requirement_id': 'goal', 'status': 'answered'}]}))]
    def model(messages, **kwargs):
        seen.append((json.dumps(messages, ensure_ascii=False), [t['function']['name'] for t in kwargs['tools']]))
        clock.now = 1960 + len(seen)  # 研究阶段（截止 2080）的最后 120 秒内
        return turns[len(seen) - 1]
    result = sr.run_simple_research('q', config=cfg, run_dir=tmp_path,
        model_call=model, search_fn=lambda q: searched.append(q) or [_doc(RAW)])
    assert result.stop_reason == 'finished' and '[c1]' in result.draft
    assert searched == ['sqlite3_rsync local'] and len(result.cards) == 1
    assert len(seen) == 4 and all('save_evidence' in names and 'search_sources' in names for _, names in seen)
    assert '【临近截止】研究阶段即将结束' not in seen[0][0] and '【临近截止】研究阶段即将结束' in seen[1][0]
    assert '工具仍可使用' in seen[1][0]
    assert bound_deadlines[0] == 2080 and 2200 not in bound_deadlines[:1]


def test_research_cutoff_switches_to_one_delivery_turn_within_total_limit(tmp_path, monkeypatch):
    """研究阶段到点：不再检索，用已存证据交稿一次；交稿请求以总截止为限，总时限不延长。"""
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    cfg = sr.SimpleConfig(wall_timeout_s=1200, wrapup_reserve_s=300, delegation_enabled=False)
    seen, searched, bound_deadlines = [], [], []
    original_set = llm.set_request_deadline
    def bind(deadline):
        bound_deadlines.append(deadline)
        return original_set(deadline)
    monkeypatch.setattr(llm, 'set_request_deadline', bind)
    research = [_turn(('search_sources', {'query': 'sqlite3_rsync local'})),
                _turn(('read_source', {'source_ref': 's1'})),
                _turn(('save_evidence', {'cards': [{'claim': '两端都在本机也可同步', 'quote': QUOTE}]})),
                _turn(('search_sources', {'query': 'more'}))]
    def model(messages, **kwargs):
        names = [t['function']['name'] for t in kwargs['tools']]
        seen.append(names)
        if names == ['finish_research']:
            assert clock.now < 2200
            return _turn(('finish_research', {'draft': '两端都在本机也能同步 [c1]。',
                          'coverage': [{'requirement_id': 'goal', 'status': 'answered'}]}))
        clock.now = 1000 + 280 * len(seen)  # 第 4 轮结束时越过研究截止 1900
        return research[len(seen) - 1]
    result = sr.run_simple_research('q', config=cfg, run_dir=tmp_path,
        model_call=model, search_fn=lambda q: searched.append(q) or [_doc(RAW)])
    assert result.stop_reason == 'research_time+wrapup' and '[c1]' in result.draft
    assert searched == ['sqlite3_rsync local']  # 越过研究截止后那次检索不执行
    assert seen[-1] == ['finish_research'] and len(seen) == 5
    assert bound_deadlines[0] == 1900 and 2200 in bound_deadlines


def test_finish_and_save_in_the_turn_that_crosses_research_cutoff_still_count(tmp_path, monkeypatch):
    """跨过研究截止返回的那一轮：存证与交稿仍有效，外部工具不再执行。"""
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    cfg = sr.SimpleConfig(wall_timeout_s=1200, wrapup_reserve_s=300, delegation_enabled=False)
    searched = []
    turns = [_turn(('search_sources', {'query': 'sqlite3_rsync local'})),
             _turn(('read_source', {'source_ref': 's1'})),
             _turn(('save_evidence', {'cards': [{'claim': '两端都在本机也可同步', 'quote': QUOTE}]}),
                   ('search_sources', {'query': 'late'}),
                   ('finish_research', {'draft': '两端都在本机也能同步 [c1]。',
                    'coverage': [{'requirement_id': 'goal', 'status': 'answered'}]}))]
    calls = []
    def model(messages, **kwargs):
        calls.append(1)
        clock.now = 1950 if len(calls) == 3 else 1000 + len(calls)
        return turns[len(calls) - 1]
    result = sr.run_simple_research('q', config=cfg, run_dir=tmp_path,
        model_call=model, search_fn=lambda q: searched.append(q) or [_doc(RAW)])
    assert result.stop_reason == 'finished' and len(result.cards) == 1 and '[c1]' in result.draft
    assert searched == ['sqlite3_rsync local']


def test_delivery_reserve_never_takes_more_than_half_of_total_time():
    clock_now = sr.time.monotonic()
    budget = sr.Budget(sr.SimpleConfig(wall_timeout_s=200, wrapup_reserve_s=300), clock_now + 200)
    assert 99 <= budget.wrapup_reserve_s <= 100
    assert budget.deadline == budget.research_deadline < budget.final_deadline
    budget.end_research()
    assert budget.deadline == budget.final_deadline
    assert sr.Budget(sr.SimpleConfig(wrapup_reserve_s=300), None).deadline is None


def test_time_limit_prompt_follows_switch(tmp_path):
    """系统提示随总时限开关变化：不限时不催稿，限时说明提醒与未完成规则。"""
    prompts = {}
    for limit in (None, 600):
        def model(messages, **kwargs):
            prompts[limit] = messages[0]['content']
            return _turn(('finish_research', {'draft': '没有可确认的结论。', 'gaps': ['未检索']}))
        sr.run_simple_research('q', config=sr.SimpleConfig(wall_timeout_s=limit, delegation_enabled=False),
                               run_dir=tmp_path / str(limit), model_call=model, search_fn=lambda q: [])
    assert '本轮不设总时限' in prompts[None] and '【剩余墙钟】' not in prompts[None]
    assert '本轮有总截止时间' in prompts[600] and '【临近截止】提醒' in prompts[600]
    assert '计入无进展' not in prompts[None] + prompts[600]


@pytest.mark.parametrize('finish', ['exception', 'http_timeout'])
def test_total_deadline_never_starts_delivery(tmp_path, monkeypatch, finish):
    """到总截止时间仍未交稿：不再发起收尾调用，如实记为到点未完成。"""
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    calls = []
    def model(*args, **kwargs):
        calls.append(1)
        clock.now = 2200
        if finish == 'exception':
            raise llm.LLMDeadlineExceeded('total deadline')
        raise llm.LLMRequestTimeout('HTTP timed out at total deadline')
    result = sr.run_simple_research('q', run_dir=tmp_path, model_call=model,
        config=sr.SimpleConfig(wall_timeout_s=1200, delegation_enabled=False))
    assert result.stop_reason == 'deadline' and len(calls) == 1 and not result.draft


def test_tool_call_at_total_deadline_is_not_executed(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sr.time, 'monotonic', clock.monotonic)
    searched, calls = [], []
    def model(*args, **kwargs):
        calls.append(1)
        clock.now = 2200
        return _turn(('search_sources', {'query': 'should not execute'}))
    result = sr.run_simple_research('q', run_dir=tmp_path, model_call=model,
        config=sr.SimpleConfig(wall_timeout_s=1200, delegation_enabled=False),
        search_fn=lambda q: searched.append(q) or [_doc()])
    assert result.stop_reason == 'deadline' and not searched and len(calls) == 1 and not result.draft


@pytest.mark.parametrize('reason, expected', [('deadline', 'timed_out'),
                                             ('call_budget+wrapup', 'completed')])
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
