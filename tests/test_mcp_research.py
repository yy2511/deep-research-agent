"""外部 MCP → 真实研究循环 → 快照/证据/事件，模型使用离线脚本。"""
import json

import pytest

from dra import events, simple_research as sr
from test_mcp_client import mcp_server, config
from test_budget_wrapup import Script, _turn


def test_mcp_visible_results_are_directly_quotable_with_actor_isolation(tmp_path, mcp_server):
    url, _, _ = mcp_server
    cfg = sr.SimpleConfig(mcp_servers=[config(url)], condense_over_chars=None)
    store = sr.RunStore(tmp_path, run_id='mcp-read', query='Orchid')
    ctx = sr.ToolContext(store, cfg)
    ctx.mcp.prepare()
    name = next(k for k, v in ctx.mcp.tools.items() if v['tool']['name'] == 'read_document')
    result, ok = ctx.execute(name, {'topic': 'Orchid'})
    assert ok, result
    source_ref = json.loads(result)['sources'][0]['source_ref']
    quote = 'Orchid keeps audit logs for 43 days.'
    assert quote in result
    before, _ = ctx.save_evidence({'cards': [{'claim': 'Logs retained 43 days', 'quote': quote}]})
    assert json.loads(before)['results'][0]['accepted'] is True
    snapshot = store.source(source_ref)
    assert snapshot.mcp['server_id'] == 'notes'
    assert snapshot.mcp['tool'] == 'read_document'
    assert snapshot.url == ''
    assert store.read_source_text(source_ref).startswith('# Orchid\r\n')
    child = ctx.fork_for_subagent(cfg, budget=sr.Budget(cfg, None))
    assert child.mcp is ctx.mcp
    assert child.read_exposures == {}
    unseen, _ = child.save_evidence({'cards': [{'claim': 'Logs retained 43 days', 'quote': quote}]})
    assert json.loads(unseen)['results'][0]['accepted'] is False
    assert child.read_source({'source_ref': source_ref})[1]


def test_mcp_search_results_are_directly_quotable(tmp_path, mcp_server):
    url, _, _ = mcp_server
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id='mcp-search', query='Orchid'),
                         sr.SimpleConfig(mcp_servers=[config(url)]))
    ctx.mcp.prepare()
    name = next(k for k, v in ctx.mcp.tools.items() if v['tool']['name'] == 'search_documents')
    result, ok = ctx.execute(name, {'query': 'Orchid'})
    quote = 'Search snippet only: Orchid keeps audit logs for 43 days.'
    assert ok and quote in result
    saved, _ = ctx.save_evidence({'cards': [{'claim': '工具返回的保留期限是 43 天', 'quote': quote}]})
    assert json.loads(saved)['results'][0]['accepted'] is True
    snapshot = ctx.store.source(ctx.store.cards()[0].source_ref)
    assert snapshot.mcp['tool'] == 'search_documents'


def test_full_loop_events_evidence_and_finish(tmp_path, mcp_server):
    from dra.mcp_client import model_tool_name
    url, calls, _ = mcp_server
    name = model_tool_name('notes', 'read_document')
    script = Script([
        _turn((name, {'topic': 'Orchid'})),
        _turn(('save_evidence', {'cards': [{'claim': '保留 43 天', 'quote': 'Orchid keeps audit logs for 43 days.'}]})),
        _turn(('finish_research', {'draft': '资料记载保留 43 天 [c1]。',
                                 'coverage': [{'requirement_id': 'goal', 'status': 'answered'}]})),
    ])
    rows = []
    sub = events.subscribe(rows.append)
    try:
        result = sr.run_simple_research('Orchid', sr.requirements_from_query('Orchid'),
            config=sr.SimpleConfig(mcp_servers=[config(url)]), run_dir=tmp_path, run_id='mcp-loop', model_call=script)
    finally:
        events.unsubscribe(sub)
    assert result.stop_reason == 'finished', result
    assert result.research_coverage == {'goal': 'answered'}
    assert name in script.tool_schemas[0]
    assert calls == [('read_document', 'Orchid')]
    done = next(r for r in rows if r.get('tool') == name and r.get('phase') == 'done')
    assert done['display']['mcp']['server_id'] == 'notes'
    assert done['display']['elapsed_ms'] >= 0
    assert result.cards[0].deep_link == ''
    assert '/sources/s1?hl=' in result.markdown


def test_mcp_respects_tool_reserve_and_library_only(tmp_path, mcp_server):
    from dra.mcp_client import model_tool_name
    from pydantic import ValidationError
    url, calls, _ = mcp_server
    with pytest.raises(ValidationError):
        sr.SimpleConfig(source_mode='library', mcp_servers=[config(url)])
    cfg = sr.SimpleConfig(mcp_servers=[config(url)], max_tool_calls=3, tool_reserve_calls=2)
    name = model_tool_name('notes', 'read_document')
    script = Script([_turn((name, {'topic': 'first'}), (name, {'topic': 'blocked'})),
                     _turn(('finish_research', {'draft': '本轮取得材料，未完成证据整理。'}))])
    sr.run_simple_research('Orchid', config=cfg, run_dir=tmp_path, run_id='reserve', model_call=script)
    assert calls == [('read_document', 'first')]


def test_service_discovery_failure_is_visible_but_does_not_break_research(tmp_path, monkeypatch):
    from dra import mcp_client as mc
    def unavailable(*a, **kw):
        raise mc.MCPError('MCP HTTP 503：暂不可用')
    monkeypatch.setattr(mc, 'discover', unavailable)
    script = Script([_turn(('finish_research', {'draft': '外部资料未取得，本轮无法确认。'}))])
    result = sr.run_simple_research('Orchid', config=sr.SimpleConfig(mcp_servers=[config('https://example.org/mcp')]),
        run_dir=tmp_path, model_call=script)
    assert result.stop_reason == 'finished'
    assert any('503' in w for w in result.warnings)
    assert not result.cards
    assert not any(name.startswith('mcp_') for name in script.tool_schemas[0])


def test_child_uses_http_mcp_while_parent_continues_and_preserves_provenance(tmp_path, mcp_server):
    import threading
    from dra.mcp_client import model_tool_name
    read_name = model_tool_name('notes', 'read_document')
    search_name = model_tool_name('notes', 'search_documents')
    parent_work = threading.Event()
    child_turn = 0
    parent_turn = 0
    inspected = False
    quote = 'Orchid keeps audit logs for 43 days.'

    def model(messages, **kw):
        nonlocal child_turn, parent_turn, inspected
        names = {t['function']['name'] for t in kw['tools']}
        assert {read_name, search_name} <= names
        if '你是研究子 Agent' in messages[0]['content']:
            child_turn += 1
            if child_turn == 1:
                assert parent_work.wait(3), '派发不能阻塞主 Agent'
                return _turn((read_name, {'topic': 'Orchid'}))
            if child_turn == 2:
                return _turn(('save_evidence', {'cards': [{'claim': '日志保留 43 天', 'quote': quote}]}))
            return _turn(('finish_research', {'draft': 'MCP_CHILD_COMPLETE [c1]', 'gaps': []}))
        parent_turn += 1
        if parent_turn == 1:
            return _turn(('delegate_research', {'tasks': [{'goal': '核对外部资料日志期限', 'context': ''}]}))
        if parent_turn == 2:
            return _turn((search_name, {'query': 'Orchid'}))
        parent_work.set()
        if 'MCP_CHILD_COMPLETE' not in str(messages):
            return _turn(('wait_research', {'timeout_s': 2}))
        if not inspected:
            inspected = True
            return _turn(('inspect_research', {'view': 'cards', 'card_id': 'c1'}))
        return _turn(('finish_research', {'draft': '子任务资料记载日志保留 43 天 [c1]。',
            'coverage': [{'requirement_id': 'goal', 'status': 'answered'}]}))

    result = sr.run_simple_research('Orchid', sr.requirements_from_query('Orchid'), run_dir=tmp_path,
        config=sr.SimpleConfig(mcp_servers=[config(mcp_server[0])], max_llm_calls=15, wall_timeout_s=15), model_call=model)
    assert result.stop_reason == 'finished', result
    assert child_turn == 3 and result.cards[0].quote == quote
    receipts = [json.loads(p.read_text()) for p in (tmp_path / 'mcp_results').glob('*.json')]
    assert {r['mcp']['actor'] for r in receipts} == {'lead', 'sub1'}
    assert all(r['mcp']['tool_call_id'] and r['mcp']['model_call_id'] for r in receipts)
    assert mcp_server[1] == [('search_documents', 'Orchid'), ('read_document', 'Orchid')]


def test_error_and_cancelled_payload_never_become_sources(tmp_path, mcp_server, monkeypatch):
    from dra import mcp_client as mc
    cfg = config(mcp_server[0])
    cfg.tools = [mc.MCPToolConfig(name='error_read', result_mode='source')]
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id='mcp-failure', query='Orchid'), sr.SimpleConfig(mcp_servers=[cfg]))
    ctx.mcp.prepare()
    result, ok = ctx.execute(mc.model_tool_name('notes', 'error_read'), {})
    assert not ok and '未登记' in result
    assert not ctx.store.state['sources']
    assert len(list((tmp_path / 'mcp_results').glob('*.json'))) == 1
    monkeypatch.setattr(sr.llm, 'cancellation_requested', lambda: True)
    result, ok = ctx.execute(mc.model_tool_name('notes', 'error_read'), {})
    assert not ok and '取消' in result
    assert len(list((tmp_path / 'mcp_results').glob('*.json'))) == 1
