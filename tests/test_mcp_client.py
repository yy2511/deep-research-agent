"""离线真实 HTTP MCP 协议测试；无模型、无外部服务。"""
import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from mcp import types
from mcp.server import MCPServer


@pytest.fixture
def mcp_server():
    server = MCPServer("Research fixture")
    calls, headers = [], []

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    def read_document(topic: str) -> str:
        calls.append(("read_document", topic))
        return '# Orchid\r\n\r\nOrchid keeps audit logs for 43 days.\nVersion: 2026-09.'

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    def search_documents(query: str) -> str:
        calls.append(("search_documents", query))
        return 'Search snippet only: Orchid keeps audit logs for 43 days.'

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    async def slow_read() -> str:
        await asyncio.sleep(2)
        return 'late result'

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    def error_read() -> str:
        raise ValueError('upstream failed')

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    def echo_document(text: str) -> str:
        return text

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def delete_document() -> str:
        calls.append(("delete_document", ""))
        return 'deleted'

    app = server.streamable_http_app(json_response=True, stateless_http=True)

    async def capture(scope, receive, send):
        if scope['type'] == 'http':
            headers.append(dict(scope['headers']))
        await app(scope, receive, send)

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    runner = uvicorn.Server(uvicorn.Config(capture, log_level='critical', lifespan='on'))
    thread = threading.Thread(target=runner.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 4
    while not runner.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert runner.started
    yield f'http://127.0.0.1:{sock.getsockname()[1]}/mcp', calls, headers
    runner.should_exit = True
    thread.join(5)
    sock.close()
    assert not thread.is_alive()


def config(url, **overrides):
    from dra.mcp_client import MCPServerConfig
    return MCPServerConfig.model_validate(dict(
        id='notes', name='研究资料', url=url, enabled=True,
        tools=[{'name': 'read_document', 'result_mode': 'source'},
               {'name': 'search_documents', 'result_mode': 'discovery'}], **overrides))


def test_http_discovery_invocation_and_environment_credentials(mcp_server, monkeypatch):
    from dra.mcp_client import discover, call_tool
    url, calls, headers = mcp_server
    monkeypatch.setenv('DRA_TEST_MCP_KEY', 'Bearer test-secret-value')
    server = config(url, headers_env={'Authorization': 'DRA_TEST_MCP_KEY'})
    tools = discover(server)
    assert {'read_document', 'search_documents'} <= {t['name'] for t in tools}
    assert next(t for t in tools if t['name'] == 'read_document')['inputSchema']['properties']['topic']
    result = call_tool(server, 'read_document', {'topic': 'Orchid'})
    assert '43 days' in result['content'][0]['text']
    assert calls == [('read_document', 'Orchid')]
    assert any(h.get(b'authorization') == b'Bearer test-secret-value' for h in headers)
    assert 'test-secret-value' not in server.model_dump_json()


def test_unallowed_tool_and_missing_credentials_do_not_make_requests(mcp_server):
    from dra.mcp_client import call_tool, discover, MCPError
    url, calls, headers = mcp_server
    with pytest.raises(MCPError, match='未允许'):
        call_tool(config(url), 'delete_document', {})
    with pytest.raises(MCPError, match='环境变量'):
        discover(config(url, headers_env={'Authorization': 'DRA_NONEXISTENT_MCP_KEY'}))
    assert not calls and not headers


def test_config_validation_and_atomic_persistence(tmp_path):
    from dra.mcp_client import MCPSettings, load_settings, save_settings, pin_servers
    from pydantic import ValidationError
    path = tmp_path / 'mcp_servers.json'
    assert load_settings(path).servers == []
    server = config('https://example.org/mcp')
    save_settings(MCPSettings(servers=[server]), path)
    assert pin_servers(['notes'], path)[0] == server
    assert 'secret' not in path.read_text()
    with pytest.raises(ValidationError):
        config('https://user:secret@example.org/mcp')
    with pytest.raises(ValidationError):
        config('http://example.org/mcp')
    with pytest.raises(ValidationError):
        MCPSettings(servers=[server, server])
    with pytest.raises(ValidationError):
        config('https://example.org/mcp?token=secret')
    path.write_text('broken')
    with pytest.raises(ValueError, match='配置'):
        load_settings(path)


def test_timeout_cancellation_and_error_are_bounded(mcp_server):
    from dra.mcp_client import call_tool, MCPError
    url, _, _ = mcp_server
    server = config(url)
    server = type(server).model_validate({**server.model_dump(), 'tools': [
        {'name': 'slow_read', 'result_mode': 'source'}, {'name': 'error_read', 'result_mode': 'source'}]})
    started = time.monotonic()
    with pytest.raises(MCPError, match='超时'):
        call_tool(server, 'slow_read', {}, deadline=time.monotonic() + .15)
    assert time.monotonic() - started < 1.5
    stop = threading.Event()
    timer = threading.Timer(.15, stop.set)
    timer.start()
    try:
        with pytest.raises(MCPError, match='取消'):
            call_tool(server, 'slow_read', {}, cancelled=stop.is_set)
    finally:
        timer.join()
    result = call_tool(server, 'error_read', {})
    assert result['isError'] is True


def test_registry_filters_writes_isolates_names_and_caps_definitions(mcp_server):
    from dra.mcp_client import MCPRegistry
    url, _, _ = mcp_server
    one = config(url)
    one = type(one).model_validate({**one.model_dump(), 'tools': [*one.model_dump()['tools'],
        {'name': 'delete_document', 'result_mode': 'source'}]})
    two = one.model_copy(update={'id': 'other'})
    registry = MCPRegistry([one, two])
    registry.prepare()
    assert len(registry.schemas) == 4
    assert len({s['function']['name'] for s in registry.schemas}) == 4
    assert all(s['function']['name'].startswith('mcp_') for s in registry.schemas)
    assert not any('delete_document' in str(s) for s in registry.schemas)
    assert len(registry.warnings) == 2 and all('写入' in w for w in registry.warnings)


def test_result_normalization_does_not_join_unrelated_blocks_or_treat_binary_as_text():
    from dra.mcp_client import source_parts, MCPError
    result = {'content': [{'type': 'text', 'text': 'one\r\n'},
                          {'type': 'text', 'text': 'two'},
                          {'type': 'image', 'data': 'binary', 'mimeType': 'image/png'}]}
    parts, warnings = source_parts(result)
    assert [p['text'] for p in parts] == ['one\r\n', 'two']
    assert not warnings  # images are handled by the pixel pipeline
    with pytest.raises(MCPError, match='上限'):
        source_parts({'content': [{'type': 'text', 'text': 'x' * 200001}]})


def test_credentials_echo_and_oversize_response_are_rejected(mcp_server, monkeypatch):
    from dra.mcp_client import call_tool, MCPError, MCPToolConfig
    server = config(mcp_server[0], headers_env={'Authorization': 'DRA_TEST_MCP_KEY'})
    server.tools = [MCPToolConfig(name='echo_document', result_mode='source')]
    monkeypatch.setenv('DRA_TEST_MCP_KEY', 'Bearer test-private-credential')
    with pytest.raises(MCPError, match='回显') as err:
        call_tool(server, 'echo_document', {'text': 'service exposes test-private-credential'})
    assert 'test-private-credential' not in str(err.value)
    with pytest.raises(MCPError, match='32000'):
        call_tool(server, 'echo_document', {'text': 'x' * 32001})


def test_paginated_discovery_and_broken_cursor(monkeypatch):
    """替换 SDK 响应边界，验证多页合并及循环游标，不虚称真实分页服务验收。"""
    from types import SimpleNamespace
    from dra import mcp_client as mc
    pages = [dict(tools=[{'name': 'a'}], nextCursor='next'), dict(tools=[{'name': 'b'}])]
    cursors = []
    class Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def list_tools(self, *, cursor):
            cursors.append(cursor)
            page = pages[min(len(cursors) - 1, len(pages) - 1)]
            return SimpleNamespace(model_dump=lambda **kw: page)
    monkeypatch.setattr(mc, 'Client', Client)
    assert [t['name'] for t in mc.discover(config('http://localhost/mcp'))] == ['a', 'b']
    assert cursors == [None, 'next']
    cursors.clear()
    pages[:] = [dict(tools=[{'name': 'a'}], nextCursor='repeat')]
    with pytest.raises(mc.MCPError, match='分页重复'):
        mc.discover(config('http://localhost/mcp'))


def test_schema_failure_and_unselected_service_never_invoke(monkeypatch):
    from dra import mcp_client as mc
    calls = []
    def discover(*a, **kw):
        calls.append('discover')
        return [{'name': 'read_document', 'inputSchema': {'type': 'object', 'properties': {
            'path': {'$ref': 'https://untrusted.invalid/schema.json'}}}}]
    monkeypatch.setattr(mc, 'discover', discover)
    empty = mc.MCPRegistry([])
    empty.prepare()
    assert not calls and not empty.schemas
    registry = mc.MCPRegistry([config('https://example.org/mcp')])
    registry.prepare()
    assert not registry.schemas
    assert any('外部引用' in w for w in registry.warnings)
    with pytest.raises(mc.MCPError, match='未允许'):
        registry.invoke(mc.model_tool_name('notes', 'read_document'), {})
