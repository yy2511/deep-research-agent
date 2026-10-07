"""官方扩展模式：离线检查传参、配置继承、授权失败与 MCP 协议握手。"""
import json
from types import SimpleNamespace as N

import pytest

from dra import browser
from dra.mcp_session import MCPSession
from dra.mcp_client import MCPError
from test_delivery_browser import context
from test_review_regressions import controlled_browser, accepted


@pytest.mark.parametrize('mode,channel,profile', [
    ('isolated', 'chrome', None), ('extension', 'chrome', None), ('extension', 'msedge', 'Profile 2'),
])
def test_launch_uses_official_extension_flags_without_isolated_context(tmp_path, monkeypatch, mode, channel, profile):
    ctx = context(tmp_path, browser={'enabled': True, 'mode': mode, 'extension_browser': channel, 'profile_dir_name': profile})
    params, clients = [], []
    monkeypatch.setattr(browser, 'availability', lambda: {'available': True})
    monkeypatch.setattr(browser, 'ThreadingHTTPServer', lambda *a: N(server_port=8000,
        serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None))
    monkeypatch.setenv('PLAYWRIGHT_MCP_EXTENSION_TOKEN', 'fixture-bypass-not-authorized')
    monkeypatch.setenv('PROVIDER_API_KEY', 'fixture-secret')
    monkeypatch.setattr(browser, 'stdio_client', lambda arg, **kw: params.append(arg))
    monkeypatch.setattr(browser, 'MCPSession', lambda *a, **kw: (clients.append(kw), N(close=lambda: None))[1])
    session = browser.BrowserSession(ctx)
    try:
        session.start()
        session.start()
        assert len(params) == 1
        config = json.loads((session.directory / 'config.json').read_text())
        assert config['outputDir'].startswith(str(session.workspace.path))
        assert config['imageResponses'] == 'allow' and config['timeouts'] == {'idle': 0}
        assert 'PLAYWRIGHT_MCP_EXTENSION_TOKEN' not in params[0].env
        assert 'PROVIDER_API_KEY' not in params[0].env
        assert clients[0]['client_name'] == 'DRA delivery / 主 Agent'
        if mode == 'extension':
            assert 'browser' not in config  # 官方先检查 isolated；残留会让扩展无效。
            assert params[0].args[3:5] == ['--extension', '--browser']
            assert params[0].args[5] == channel
            assert params[0].args[6:] == (['--profile-dir-name', profile] if profile else [])
        else:
            assert config['browser']['isolated'] and config['browser']['launchOptions']['headless']
            assert '--extension' not in params[0].args
    finally:
        session.close()


def test_subagent_inherits_extension_config_and_has_separate_session_identity(tmp_path):
    from dra.simple_research import Budget
    ctx = context(tmp_path, browser={'enabled': True, 'mode': 'extension', 'profile_dir_name': 'Default'})
    child = ctx.fork_for_subagent(ctx.config, budget=Budget(ctx.config, None), delegation_id='sub1')
    assert child.config.browser == ctx.config.browser
    assert browser.BrowserSession(child).directory != browser.BrowserSession(ctx).directory


@pytest.mark.parametrize('tool', ['browser_navigate', 'browser_evaluate'])
def test_extension_error_is_returned_without_a_second_connection_attempt(tmp_path, monkeypatch, tool):
    ctx = context(tmp_path, browser={'enabled': True, 'mode': 'extension'})
    session, _, calls = controlled_browser(ctx, monkeypatch)
    def rejected(name, args, **kw):
        calls.append(name)
        return {'isError': True, 'content': [{'type': 'text', 'text': 'User denied the extension connection'}]}
    session.session.request = rejected
    if tool == 'browser_evaluate':
        with pytest.raises(MCPError, match='连接/授权'):
            session.invoke({'tool': tool, 'arguments': {'function': '() => document.title'}})
        assert calls == ['browser_evaluate']
        return
    response = session.invoke({'tool': tool, 'arguments': {'url': 'https://fixture.invalid/article'}})
    assert not response['ok'] and not response['evidence_eligible']
    assert calls == ['browser_navigate']
    assert 'User denied' in response['content'][0]
    assert response['mcp']['browser_mode'] == 'extension'
    assert not ctx.store.state['sources']


@pytest.mark.parametrize('tool,args', [('browser_close', {}), ('browser_tabs', {'action': 'close'})])
def test_closing_browser_does_not_reopen_it_for_page_identity(tmp_path, monkeypatch, tool, args):
    ctx = context(tmp_path, browser={'enabled': True, 'mode': 'extension'})
    session, _, calls = controlled_browser(ctx, monkeypatch)
    session.tools[tool] = {'name': tool, 'inputSchema': {'type': 'object'}}
    result = session.invoke({'tool': tool, 'arguments': args})
    assert result['ok'] and not result['evidence_eligible']
    assert calls == [(tool, args)]


def test_extension_catalog_instructions_and_snapshot_keep_evidence_contract(tmp_path, monkeypatch):
    ctx = context(tmp_path, browser={'enabled': True, 'mode': 'extension'}, vision={'enabled': False})
    session, _, _ = controlled_browser(ctx, monkeypatch)
    catalog = session.invoke({})
    assert catalog['mode'] == 'extension' and '授权' in catalog['connection']
    assert catalog['connection'] in browser.instructions(ctx.config.browser)
    assert '没有用户的个人登录状态' in browser.instructions(browser.BrowserConfig())
    response = session.invoke({'tool': 'browser_snapshot'})
    assert response['sources'] and accepted(ctx, 'Published rainfall was 42 mm.')
    assert response['mcp']['browser_mode'] == 'extension'


def test_mcp_client_name_reaches_official_initialization(tmp_path):
    received = []
    class Client:
        def __init__(self, *a, **kw):received.append(kw['client_info'])
        async def __aenter__(self):return self
        async def __aexit__(self, *a):pass
        async def list_tools(self, **kw):return N(tools=[], next_cursor=None)
    session = MCPSession('mock', root=tmp_path, client_factory=Client, client_name='DRA run-1 / sub2')
    try:
        assert session.request() == []
        assert received[0].name == 'DRA run-1 / sub2'
    finally:
        session.close()


@pytest.mark.browser
def test_official_extension_server_lists_tools_without_opening_personal_browser(tmp_path):
    ctx = context(tmp_path, browser={'enabled': True, 'mode': 'extension', 'profile_dir_name': 'DRA-test-not-a-user-profile'})
    session = browser.BrowserSession(ctx)
    try:
        # tools/list 不连接 Chrome：实际网页操作才要求扩展授权。
        result = session.invoke({})
        assert result['ok'] and result['mode'] == 'extension'
        assert 'browser_snapshot' in {tool['name'] for tool in result['tools']}
        assert 'browser_run_code_unsafe' not in {tool['name'] for tool in result['tools']}
        assert not list((ctx.store.dir / 'mcp_results').glob('*.json'))
    finally:
        session.close()
