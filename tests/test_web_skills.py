from fastapi.testclient import TestClient
import pytest
from dra import web, skills
from test_skills import TEXT

@pytest.fixture
def client(tmp_path, monkeypatch):
    repo = skills.SkillRepository(tmp_path, builtin_dir=tmp_path / 'none')
    monkeypatch.setattr(web, 'skill_repository', lambda: repo)
    with TestClient(web.app) as c:
        yield c, repo


def test_edit_enable_prepare_and_planning_snapshot(client, monkeypatch):
    c, repo = client
    assert c.put('/api/skills/packages/compare', json={'text': TEXT, 'references': {}}).status_code == 200
    assert c.put('/api/skills/enabled', json={'enabled':['compare']}).status_code == 200
    prepared = c.post('/api/skills/prepare', json={'selected':['compare']})
    assert prepared.status_code == 200
    identity = prepared.json()['skill_selection']
    repo.save('compare', TEXT.replace('Compare costs', 'NEW costs'), {})
    captured = []
    def brief(q, **kwargs):
        captured.append(kwargs['source_context'])
        return {'kind':'clarify','gaps':[]}, {}
    monkeypatch.setattr(web, '_brief_payload', brief)
    res = c.post('/api/brief', json={'query':'Which?', 'skill_selection':identity})
    assert res.status_code == 200
    assert 'Compare costs' in captured[0] and 'NEW costs' not in captured[0]
    assert c.post('/api/skills/prepare', json={'selected':['missing']}).status_code == 400
    assert c.post('/api/brief', json={'query':'Which?', 'skill_selection':'bad'}).status_code == 400


def test_research_receives_prepared_bundle(client, monkeypatch):
    c, repo = client
    repo.save('compare', TEXT, {})
    repo.set_enabled(['compare'])
    bundle = repo.prepare(['compare'])
    seen = []
    def start(query, **kwargs):
        seen.append(kwargs['config'].skill_bundle)
        return 'test-skills'
    monkeypatch.setattr(web.REGISTRY, 'start_simple_research', start)
    response = c.post('/api/research', json={'query':'Which?', 'mode':'simple','skill_selection':bundle['id']})
    assert response.status_code == 200
    assert seen == [bundle]


def test_api_rejects_bad_skill_and_retains_previous(client):
    c, repo = client
    repo.save('compare', TEXT, {})
    res = c.put('/api/skills/packages/compare', json={'text':TEXT, 'references':{'../secret':'no'}})
    assert res.status_code == 400
    assert repo.list()['skills'][0]['text'] == TEXT


def test_http_run_followup_and_history_keep_same_version(client, tmp_path, monkeypatch):
    from dra import research, runtime_config
    from dra.runstore import RunStore
    from test_web_research_e2e import _wait_done
    from test_simple_research import _turn
    import time
    c, repo = client
    store = RunStore(tmp_path / 'runs')
    registry = web.RunRegistry(store)
    monkeypatch.setattr(web, 'STORE', store)
    monkeypatch.setattr(web, 'REGISTRY', registry)
    monkeypatch.setattr(runtime_config, 'current', runtime_config._code_defaults)
    monkeypatch.setenv('DRA_EXECUTION_ENABLED', '0')
    repo.save('compare', TEXT, {'references/check.md':'Check units.'})
    repo.set_enabled(['compare'])
    identity = repo.prepare(['compare'])['id']
    original = research.run_minimal_research
    def model(messages, **kwargs):
        assert 'Compare costs' in str(messages)
        return _turn(('finish_research', {'draft':'尚未取得可引用的外部证据，本轮只说明研究方法。', 'coverage':[]}))
    def run(*args, **kwargs):
        return original(*args, model_call=model, **kwargs)
    monkeypatch.setattr(research, 'run_minimal_research', run)
    res = c.post('/api/research', json={'query':'Compare', 'mode':'simple', 'skill_selection':identity})
    assert res.status_code == 200, res.text
    parent = res.json()['run_id']
    assert _wait_done(c, parent)['meta']['status'] in {'done', 'partial'}
    deadline = time.monotonic() + 2
    while registry.runs[parent].busy and time.monotonic() < deadline:
        time.sleep(.01)
    before = c.get(f'/api/runs/{parent}/skills').json()
    assert before['loaded'][0]['agent_id'] == 'lead'
    repo.save('compare', TEXT.replace('Compare costs', 'NEW costs'), {})
    repo.set_enabled([])
    result = c.post(f'/api/research/{parent}/followup', json={'request_id':'skills-next','text':'继续比较'})
    assert result.status_code == 202, result.text
    child = result.json()['run_id']
    assert _wait_done(c, child)['meta']['status'] in {'done', 'partial'}
    assert c.get(f'/api/runs/{child}/skills').json()['catalog'] == before['catalog']
    assert c.get(f'/api/runs/{parent}/skills').json() == before
    assert c.get(f'/api/runs/{child}/simple').json()['sources'] == []
