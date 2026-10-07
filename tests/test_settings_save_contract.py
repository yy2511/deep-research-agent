"""设置页可写字段经过真实 HTTP 保存并回读；配置写入临时目录，不调用模型。"""
from fastapi.testclient import TestClient
from dra import runtime_config as rc, web
import pytest


def test_evidence_judge_setting_applies_without_exposing_key(tmp_path, monkeypatch):
    from dra import evidence_judge
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "runtime_config.json")
    monkeypatch.setattr(rc, "_cache", None)
    monkeypatch.setattr(evidence_judge, "_api_key", lambda: "secret-for-test")
    client = TestClient(web.app)
    before = client.get("/api/config").json()
    assert before["evidence_judge"] == {"enabled": False}
    assert before["evidence_judge_status"]["key_configured"] is True
    body = {k: before[k] for k in rc.RuntimeModelSettings.model_fields}
    body["evidence_judge"] = {"enabled": True}
    assert client.put("/api/config", json=body).status_code == 200
    monkeypatch.setattr(rc, "_cache", None)
    assert rc.to_simple_config(rc.current()).evidence_judge.enabled
    assert "secret-for-test" not in client.get("/api/config").text
    assert "secret-for-test" not in (tmp_path / "runtime_config.json").read_text()
    assert client.get("/api/config").json()["subagent"] == before["subagent"]


def test_settings_save_updates_current_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "runtime_config.json")
    monkeypatch.setattr(rc, "_cache", None)
    client = TestClient(web.app)
    before = client.get("/api/config").json()
    body = {key: before[key] for key in rc.RuntimeModelSettings.model_fields}
    body["planner"] = {"provider": "opencode", "model": "deepseek-v4.1-flash", "effort": None}
    body["subagent"] = {"provider": "opencode", "model": "deepseek-v4.1-flash", "reasoning": False, "effort": None}
    response = client.put("/api/config", json=body)
    assert response.status_code == 200, response.text
    monkeypatch.setattr(rc, "_cache", None)
    after = client.get("/api/config").json()
    assert after["planner"] == body["planner"]
    assert after["subagent"] == body["subagent"]
    assert after["simple_model"]["model"] == body["subagent"]["model"]
    for key in ("writer", "agentic", "enable_cross_worker_audit"):
        assert key not in after
    assert client.put("/api/config", json={**body, "unknown_option": True}).status_code == 400


def test_old_settings_client_preserves_visual_connection(tmp_path, monkeypatch):
    from dra import provider_registry
    from dra.vision import VisionConfig
    monkeypatch.setattr(rc, '_CONFIG_PATH', tmp_path / 'runtime_config.json')
    monkeypatch.setattr(rc, '_cache', None)
    current = rc.current()
    monkeypatch.setattr(rc, '_cache', current.model_copy(update={'vision': VisionConfig(provider='openai')}))
    with TestClient(web.app) as client:
        before = client.get('/api/config').json()
        body = {k: before[k] for k in rc.RuntimeModelSettings.model_fields if k != 'vision'}
        response = client.put('/api/config', json=body)
        assert response.status_code == 200 and response.json()['vision'] == before['vision']
        body['vision'] = {**before['vision'], 'provider': 'missing-vision-provider'}
        monkeypatch.setattr(provider_registry, 'exists', lambda p: False)
        assert client.put('/api/config', json=body).status_code == 400


def test_browser_setting_roundtrip_and_old_client_preserves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, '_CONFIG_PATH', tmp_path / 'runtime_config.json')
    monkeypatch.setattr(rc, '_cache', None)
    with TestClient(web.app) as client:
        before = client.get('/api/config').json()
        assert before['browser'] == {'enabled': True, 'mode': 'isolated', 'extension_browser': 'chrome', 'profile_dir_name': None}
        body = {k: before[k] for k in rc.RuntimeModelSettings.model_fields}
        body['browser'] = {'enabled': True, 'mode': 'extension', 'extension_browser': 'msedge', 'profile_dir_name': 'Profile 2'}
        assert client.put('/api/config', json=body).status_code == 200
        monkeypatch.setattr(rc, '_cache', None)
        assert rc.to_simple_config(rc.current()).browser.model_dump() == body['browser']
        # 仍在旧页面的客户端只有开关；不能丢掉新设置的扩展模式。
        body['browser'] = {'enabled': False}
        expected = {'enabled': False, 'mode': 'extension', 'extension_browser': 'msedge', 'profile_dir_name': 'Profile 2'}
        assert client.put('/api/config', json=body).json()['browser'] == expected
        del body['browser']
        assert client.put('/api/config', json=body).json()['browser'] == expected
        monkeypatch.setattr(rc, '_cache', None)
        assert not rc.to_simple_config(rc.current()).browser.enabled
        body['browser'] = {'mode': 'isolated', 'profile_dir_name': None}
        assert client.put('/api/config', json=body).json()['browser']['profile_dir_name'] is None


@pytest.mark.parametrize("enabled", [False, True])
def test_call_limit_switch_saved_and_applied_to_new_research(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "runtime_config.json")
    monkeypatch.setattr(rc, "_cache", None)
    captured = []
    def start(query, **kwargs):
        captured.append(kwargs["config"])
        return "offline-run"
    monkeypatch.setattr(web.REGISTRY, "start_simple_research", start)
    with TestClient(web.app) as client:
        before = client.get("/api/config").json()
        assert before["call_limits_enabled"] is False
        body = {k: before[k] for k in rc.RuntimeModelSettings.model_fields}
        body["call_limits_enabled"] = enabled
        assert client.put("/api/config", json=body).status_code == 200
        monkeypatch.setattr(rc, "_cache", None)
        after = client.get("/api/config").json()
        assert after["call_limits_enabled"] is enabled
        assert after["simple_budget"]["max_llm_calls"] == (20 if enabled else None)
        assert after["simple_budget"]["reserve_wrapup_calls"] == (1 if enabled else 0)
        assert after["subagent"] == before["subagent"]
        assert client.post("/api/research", json={"query": "q", "mode": "simple"}).status_code == 200
    assert captured[0].max_llm_calls == captured[0].max_tool_calls == (20 if enabled else None)
    assert captured[0].wall_timeout_s is None


@pytest.mark.parametrize("limit", [None, 900.0])
def test_time_limit_switch_saved_and_applied_to_new_research(tmp_path, monkeypatch, limit):
    """总时限默认关闭；开启后的秒数与关闭后的 None 都原样进入新研究。"""
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "runtime_config.json")
    monkeypatch.setattr(rc, "_cache", None)
    captured = []
    monkeypatch.setattr(web.REGISTRY, "start_simple_research",
                        lambda query, **kwargs: captured.append(kwargs["config"]) or "offline-run")
    with TestClient(web.app) as client:
        before = client.get("/api/config").json()
        assert before["wall_timeout_s"] is None and before["simple_budget"]["wall_timeout_s"] is None
        body = {k: before[k] for k in rc.RuntimeModelSettings.model_fields}
        body["wall_timeout_s"] = limit
        assert client.put("/api/config", json=body).status_code == 200
        monkeypatch.setattr(rc, "_cache", None)
        assert client.get("/api/config").json()["simple_budget"]["wall_timeout_s"] == limit
        assert client.post("/api/research", json={"query": "q", "mode": "simple"}).status_code == 200
    assert captured[0].wall_timeout_s == limit
