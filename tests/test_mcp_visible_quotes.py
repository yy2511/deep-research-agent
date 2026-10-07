"""工具返回可见范围的引用合同；协议外层另由真实 HTTP MCP 测试覆盖。"""
import json

import pytest

from dra import mcp_client as mc, simple_research as sr


def context(tmp_path, monkeypatch, payload, **config):
    server = mc.MCPServerConfig(id='fixture', name='人工夹具', url='https://example.org/mcp',
                                tools=[{'name': 'external_tool'}])
    monkeypatch.setattr(mc, 'discover', lambda *a, **kw: [
        {'name': 'external_tool', 'inputSchema': {'type': 'object'}}])
    monkeypatch.setattr(mc, 'call_tool', lambda *a, **kw: payload)
    cfg = sr.SimpleConfig(mcp_servers=[server], **config)
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id='visible-quotes', query='test'), cfg)
    ctx.mcp.prepare()
    return ctx, mc.model_tool_name('fixture', 'external_tool')


def accepted(ctx, quote):
    result, _ = ctx.save_evidence({'cards': [{'claim': '工具返回的记录', 'quote': quote}]})
    return json.loads(result)['results'][0]['accepted']


def test_long_result_exposes_only_visible_prefix_then_allows_explicit_reread(tmp_path, monkeypatch):
    head, tail = 'Visible prefix is recorded exactly.', 'Unseen tail belongs to another version.'
    raw = head + '\r\n' + 'padding data.\n' * 1000 + tail
    ctx, name = context(tmp_path, monkeypatch, {'content': [{'type': 'text', 'text': raw}]})
    result, ok = ctx.execute(name, {})
    data = json.loads(result)
    assert ok and head in result and tail not in result
    source = data['sources'][0]
    ref = source['source_ref']
    assert source['has_more'] and 0 < source['next_cursor'] < len(raw)
    assert ctx.store.read_source_text(ref) == raw
    assert accepted(ctx, head)
    assert not accepted(ctx, tail)
    ctx.read_source({'source_ref': ref, 'range': f'{raw.index(tail)}-'})
    assert accepted(ctx, tail)
    card = ctx.store.cards()[-1]
    lo, hi = sr._chars_of(card.location)
    assert raw[lo:hi] == card.quote == tail


def test_later_unshown_blocks_cannot_be_cited_or_joined(tmp_path, monkeypatch):
    first = 'First block is a distinct record.\n' * 400
    second = 'Second block has a different original source.'
    ctx, name = context(tmp_path, monkeypatch, {'content': [
        {'type': 'text', 'text': first}, {'type': 'text', 'text': second}]})
    result, ok = ctx.execute(name, {})
    assert ok
    sources = json.loads(result)['sources']
    assert second not in result and len(sources) == 2
    assert not accepted(ctx, second)
    assert not ctx.read_exposures.get(sources[1]['source_ref'])
    ctx.read_source({'source_ref': sources[1]['source_ref'], 'range': '0-'})
    assert accepted(ctx, second)
    assert not accepted(ctx, 'First block is a distinct record.\n' + second)


@pytest.mark.parametrize('with_text', [False, True])
def test_structured_fields_are_visible_and_quotable_even_with_text_summary(tmp_path, monkeypatch, with_text):
    payload = {'structuredContent': {'retention_days': 43, 'region': 'east'}}
    if with_text:
        payload['content'] = [{'type': 'text', 'text': 'One database row was returned.'}]
    ctx, name = context(tmp_path, monkeypatch, payload)
    result, ok = ctx.execute(name, {})
    sources = json.loads(result)['sources']
    assert ok and len(sources) == (2 if with_text else 1)
    assert any(s['format'] == 'structured_json' for s in sources)
    assert accepted(ctx, '"retention_days": 43')


def test_child_result_is_not_automatically_read_by_parent_or_sibling(tmp_path, monkeypatch):
    quote = 'The child alone received this original return value.'
    parent, name = context(tmp_path, monkeypatch, {'content': [{'type': 'text', 'text': quote}]})
    child = parent.fork_for_subagent(parent.config, budget=sr.Budget(parent.config, None))
    sibling = parent.fork_for_subagent(parent.config, budget=sr.Budget(parent.config, None))
    assert child.execute(name, {})[1]
    assert not accepted(parent, quote) and not accepted(sibling, quote)
    assert accepted(child, quote)


def test_mcp_archive_only_hides_saved_block_in_model_messages(tmp_path, monkeypatch):
    saved = 'The first returned fact is saved into an evidence card.'
    unsaved = 'The second returned fact must remain visible for later use.'
    ctx, name = context(tmp_path, monkeypatch, {'content': [
        {'type': 'text', 'text': saved}, {'type': 'text', 'text': unsaved}]})
    receipt, ok = ctx.execute(name, {})
    assert ok and saved in receipt and unsaved in receipt
    assert accepted(ctx, saved)
    exchange = [{'role': 'assistant', 'tool_calls': [{'id': 't1', 'function': {'name': name}}]},
                {'role': 'tool', 'tool_call_id': 't1', 'content': receipt}]
    archived = sr._archived_exchange(ctx, exchange)
    sources = json.loads(archived[1]['content'])['sources']
    assert sources[0]['archived'] and not sources[0].get('excerpts')
    assert unsaved in json.dumps(sources[1])
    assert exchange[1]['content'] == receipt and saved in receipt
    raw_receipt = next((tmp_path / 'mcp_results').glob('*.json'))
    assert json.loads(raw_receipt.read_text())['result']['content'][0]['text'] == saved


def test_display_summary_does_not_duplicate_large_excerpts(tmp_path, monkeypatch):
    text = 'Original text for the model only. ' * 100
    ctx, name = context(tmp_path, monkeypatch, {'content': [{'type': 'text', 'text': text}]})
    result, _ = ctx.execute(name, {})
    display = sr._display_payload(name, {}, result, 0)
    assert display['total'] == 1 and display['sources'][0]['source_ref']
    assert 'excerpts' not in display['sources'][0]
    assert text not in json.dumps(display)


@pytest.mark.parametrize('wrapped', [True, False])
def test_sdk_duplicate_json_representation_is_not_a_second_source(wrapped):
    value = 'Exact same original result.' if wrapped else {'count': 43}
    text = value if wrapped else json.dumps(value)
    parts, _ = mc.source_parts({'content': [{'type': 'text', 'text': text}],
                               'structuredContent': {'result': value} if wrapped else value})
    assert len(parts) == 1 and parts[0]['text'] == text


def test_mcp_return_remains_in_history_without_shared_read_previews(tmp_path, monkeypatch):
    visible = 'Visible facts remain available during wrapup.'
    hidden = 'This second part has not been shown.'
    ctx, name = context(tmp_path, monkeypatch, {'content': [
        {'type': 'text', 'text': visible}, {'type': 'text', 'text': hidden}]}, read_window_chars=len(visible))
    result, ok = ctx.execute(name, {})
    ctx.receipts.append({'tool': name, 'payload': result, 'ok': ok})
    exchange = [{'role': 'assistant', 'tool_calls': [{'id': 't1', 'function': {'name': name}}]},
                {'role': 'tool', 'tool_call_id': 't1', 'content': result}]
    messages = sr._build_messages([], ctx, [], [exchange], seconds_left=None)
    content = json.dumps(messages, ensure_ascii=False)
    assert visible in content and hidden not in content
    assert '【最近已读、可直接存证的片段】' not in content
    child = ctx.fork_for_subagent(ctx.config, budget=sr.Budget(ctx.config, None))
    child_messages = sr._build_messages([], child, [], [], seconds_left=None)
    assert visible not in json.dumps(child_messages, ensure_ascii=False)
    assert not accepted(child, visible)
