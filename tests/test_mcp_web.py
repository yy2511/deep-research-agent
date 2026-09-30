import json
import time

from fastapi.testclient import TestClient

from dra import mcp_client, research, web
from test_mcp_client import mcp_server, config
from test_web_research_e2e import isolated_web, _wait_done
from test_budget_wrapup import Script, _turn


def test_http_settings_test_and_invalid_config_redaction(tmp_path, monkeypatch, mcp_server):
    monkeypatch.setattr(mcp_client, '_CONFIG_PATH', tmp_path / 'mcp.json')
    server = config(mcp_server[0]).model_dump()
    with TestClient(web.app) as client:
        assert client.get('/api/mcp/config').json()['servers'] == []
        assert client.post('/api/mcp/test', json=server).json()['ok']
        result = client.put('/api/mcp/config', json={'servers': [server]})
        assert result.status_code == 200
        assert client.get('/api/mcp/config').json()['servers'][0]['tools'] == server['tools']
        bad = {**server, 'url': 'https://user:private-secret@example.org/mcp'}
        response = client.put('/api/mcp/config', json={'servers': [bad]})
        assert response.status_code == 400
        assert 'private-secret' not in response.text
        assert client.get('/api/mcp/config').json() == result.json()
        monkeypatch.setattr(mcp_client, 'discover', lambda *a, **kw: (_ for _ in ()).throw(mcp_client.MCPError('MCP 连接失败')))
        assert client.post('/api/mcp/test', json=server).status_code == 502


def test_http_research_mcp_highlight_followup_and_revocation(isolated_web, tmp_path, monkeypatch, mcp_server):
    store, registry = isolated_web
    monkeypatch.setattr(mcp_client, '_CONFIG_PATH', tmp_path / 'mcp.json')
    server = config(mcp_server[0])
    mcp_client.save_settings(mcp_client.MCPSettings(servers=[server]))
    name = mcp_client.model_tool_name('notes', 'read_document')
    original = research.run_minimal_research

    def run(query, requirements=None, **kw):
        script = Script([
            _turn((name, {'topic': 'Orchid'})),
            _turn(('save_evidence', {'cards': [{'claim': '保留 43 天', 'quote': 'Orchid keeps audit logs for 43 days.'}]})),
            _turn(('finish_research', {'draft': '资料记载保留 43 天 [c1]。', 'coverage':
                [{'requirement_id': r.id, 'status': 'answered'} for r in requirements or []]})),
        ])
        return original(query, requirements, model_call=script, **kw)

    monkeypatch.setattr(research, 'run_minimal_research', run)
    with TestClient(web.app) as client:
        bad = client.post('/api/research', json={'query': 'Orchid', 'mode': 'simple', 'mcp_server_ids': ['missing']})
        assert bad.status_code == 400
        response = client.post('/api/research', json={'query': 'Orchid', 'mode': 'simple', 'mcp_server_ids': ['notes']})
        assert response.status_code == 200, response.text
        run_id = response.json()['run_id']
        done = _wait_done(client, run_id)
        assert done['meta']['status'] == 'done', done
        result = client.get(f'/api/runs/{run_id}/simple').json()
        card, source = result['cards'][0], result['sources'][0]
        assert source['mcp']['server_id'] == 'notes'
        assert done['meta']['config_summary']['mcp_server_ids'] == ['notes']
        assert '<mark' in client.get(f'/api/runs/{run_id}/sources/s1?hl={card["location"][6:]}').text
        receipt_id = source['mcp']['receipt_id']
        raw = client.get(f'/api/runs/{run_id}/mcp-results/{receipt_id}')
        assert raw.status_code == 200 and raw.json()['result']['content'][0]['text'].startswith('# Orchid')
        assert client.get(f'/api/runs/{run_id}/mcp-results/unknown').status_code == 404
        deadline = time.monotonic() + 3
        while registry.runs[run_id].busy and time.monotonic() < deadline:
            time.sleep(.01)
        following = client.post(f'/api/research/{run_id}/followup', json={'request_id': 'mcp-next', 'text': '重新确认保留期限'})
        assert following.status_code == 202, following.text
        second = following.json()['run_id']
        _wait_done(client, second)
        assert client.get(f'/api/runs/{second}/simple').json()['sources'][0]['mcp']['server_id'] == 'notes'
        while registry.runs[second].busy and time.monotonic() < deadline:
            time.sleep(.01)
        mcp_client.save_settings(mcp_client.MCPSettings(servers=[server.model_copy(update={'enabled': False})]))
        assert client.post(f'/api/research/{second}/followup', json={'request_id': 'revoked', 'text': '继续'}).status_code == 409
        assert client.get(f'/api/runs/{run_id}/simple').json() == result


def test_brief_receives_external_source_scope_and_library_mode_rejects(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_client, '_CONFIG_PATH', tmp_path / 'mcp.json')
    server = config('https://example.org/mcp')
    mcp_client.save_settings(mcp_client.MCPSettings(servers=[server]))
    assert '研究资料' in web._brief_sources(web.SourceSelectionReq(mcp_server_ids=['notes']))['source_context']
    with TestClient(web.app) as client:
        result = client.post('/api/brief', json={'query': 'test', 'source_mode': 'library', 'mcp_server_ids': ['notes']})
        assert result.status_code == 400


def test_legacy_tool_modes_are_removed_without_losing_connections(tmp_path, monkeypatch):
    path = tmp_path / 'mcp.json'
    monkeypatch.setattr(mcp_client, '_CONFIG_PATH', path)
    server = config('https://example.org/mcp').model_dump()
    server['headers_env'] = {'Authorization': 'MY_MCP_AUTH'}
    server['tools'] = [{'name': 'read_document', 'result_mode': 'source'},
                       {'name': 'search_documents', 'result_mode': 'discovery'}]
    path.write_text(json.dumps({'schema_version': 1, 'servers': [server]}))
    with TestClient(web.app) as client:
        migrated = client.get('/api/mcp/config').json()
        expected = {**server, 'tools': [{'name': 'read_document'}, {'name': 'search_documents'}]}
        assert migrated['servers'] == [expected]
        response = client.put('/api/mcp/config', json={'schema_version': 1, 'servers': [server]})
        assert response.status_code == 200
        assert json.loads(path.read_text()) == migrated
