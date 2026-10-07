"""Use the real research loop and HTTP retry owner, with an offline provider."""
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError
from openai.types.chat import ChatCompletion

from dra import llm, simple_research as sr


@pytest.mark.parametrize('failed_attempts', [3, 10, 11])
def test_research_survives_repeated_timeouts_and_stops_at_retry_limit(
        tmp_path, monkeypatch, failed_attempts):
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) <= failed_attempts:
            raise APITimeoutError(request=httpx.Request('POST', 'https://offline.test'))
        return ChatCompletion(id='offline', created=0, model='offline', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'finish', 'type': 'function', 'function': {
                        'name': 'finish_research', 'arguments': json.dumps({
                            'draft': '现有材料不足，未确认结论。', 'coverage': []})}}]}}])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, '_get_client', lambda _: client)
    monkeypatch.setattr(llm.time, 'sleep', lambda _: None)

    def model(messages, **kwargs):
        assert kwargs['max_retries'] == 10
        return llm.call_tools(messages, **kwargs)

    result = sr.run_simple_research('q', model_call=model, run_dir=tmp_path,
        config=sr.SimpleConfig(delegation_enabled=False))
    assert len(requests) == min(failed_attempts + 1, 11)
    assert len(result.calls) == 1
    if failed_attempts <= 10:
        assert result.stop_reason == 'finished'
        assert result.draft and result.calls[0].attempts == failed_attempts + 1
    else:
        assert result.stop_reason == 'request_timeout'
        assert not result.draft and result.calls[0].error == 'LLMRequestTimeout'
