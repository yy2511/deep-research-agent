from types import SimpleNamespace as NS
from dra import llm, events
from test_llm_stream_tools import FakeClient, FakeStream, _chunk
from test_cache_observability import capture


def test_reasoning_callback_keeps_plain_fields_separate_and_protocol_unchanged(monkeypatch):
    client = FakeClient(FakeStream([
        _chunk(reasoning_content='检索'), _chunk(reasoning_content='验证'),
        _chunk(reasoning={'encrypted': 'opaque'}), _chunk(content='公开说明'),
        _chunk(finish_reason='stop'),
    ]))
    monkeypatch.setattr(llm, '_get_client', lambda *_: client)
    thought, content = [], []
    turn = llm.call_tools([], tools=[], on_chunk=content.append,
                          on_reasoning=lambda field, text: thought.append((field, text)))
    assert thought == [('reasoning_content', '检索'), ('reasoning_content', '验证')]
    assert content == ['公开说明']
    assert turn.assistant_message['reasoning_content'] == '检索验证'
    assert turn.assistant_message['reasoning'] == {'encrypted': 'opaque'}


def test_actual_parameters_whitelist_and_structured_full_input(monkeypatch):
    response = NS(usage=None, choices=[NS(message=NS(content='answer', tool_calls=[]))])
    client = NS(base_url='https://user:secret@example.com/v1', chat=NS(completions=NS(create=lambda **_: response)))
    monkeypatch.setattr(llm, '_get_client', lambda *_: client)
    monkeypatch.setattr(llm, '_provider_headers', lambda *_: {'extra_headers': {'Authorization': 'TOP_SECRET'}})
    token = llm.set_trace_full_io(True)
    try:
        with capture() as rows:
            llm.call_tools([{'role':'user','content':'question'}], tools=[], temperature=.7, max_tokens=123)
    finally:
        llm.reset_trace_full_io(token)
    metric = next(r for r in rows if r['type'] == 'llm_usage')
    assert metric['request_parameters']['temperature'] == .7
    assert metric['request_parameters']['max_tokens'] == 123
    assert 'reasoning_effort' not in metric['request_parameters']
    assert metric['request_messages'] == [{'role':'user','content':'question'}]
    assert metric['request_tools'] == []
    assert 'TOP_SECRET' not in str(rows) and 'user:secret' not in str(rows)


def test_reasoning_event_has_independent_identity_and_interruption():
    from dra.simple_research import _LeadMessage
    with capture() as rows:
        msg = _LeadMessage('run', 2, kind='reasoning', scope='sub1:reasoning_content', role='delegate')
        msg.add('可读思考')
        msg.finish(complete=False)
    assert [r['type'] for r in rows] == ['agent_reasoning', 'agent_reasoning']
    assert rows[-1]['complete'] is False
    assert rows[-1]['interrupted'] is True
    assert rows[-1]['call_id'] == 'budget-1'
