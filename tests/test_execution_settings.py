"""网页执行开关、只读重试策略与执行状态：同一解析入口；离线，不连接 Docker、不调用模型。"""
import json

import pytest
from fastapi.testclient import TestClient

from dra import execution, runtime_config as rc, simple_research as sr, web


@pytest.fixture
def env_execution(tmp_path, monkeypatch):
    """不读取本机 .env；用环境变量模拟“本机默认”。"""
    monkeypatch.setattr(execution, "_ENV_PATH", tmp_path / "missing.env")
    monkeypatch.setenv("DRA_EXECUTION_IMAGE", "fixture-image:1")
    monkeypatch.setenv("DRA_EXECUTION_NETWORK_MODE", "bridge")

    def set_default(enabled: bool):
        monkeypatch.setenv("DRA_EXECUTION_ENABLED", "true" if enabled else "false")
    return set_default


@pytest.fixture
def no_docker(monkeypatch):
    """状态接口只经过 availability；这里替换掉，记录收到的配置。"""
    seen = []

    def fake(config):
        seen.append(config)
        if not config.enabled:
            return {"enabled": False, "available": False, "reason": "disabled"}
        return {"enabled": True, "available": False, "reason": "image_missing"}
    monkeypatch.setattr(execution, "availability", fake)
    return seen


def _write_config(path, **fields):
    payload = rc._code_defaults().model_dump(mode="json")
    payload.pop("execution_enabled")
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_old_config_without_field_keeps_environment_permission(tmp_path, env_execution):
    _write_config(rc._CONFIG_PATH)
    settings = rc.load()
    assert settings.execution_enabled is None
    for default in (True, False):
        env_execution(default)
        assert rc.execution_config(settings) == execution.ExecutionConfig.from_env()
        assert rc.to_simple_config(settings).execution.enabled is default


@pytest.mark.parametrize("choice", [True, False])
def test_explicit_switch_only_overrides_enabled(env_execution, choice):
    env_execution(not choice)
    settings = rc._code_defaults().model_copy(update={"execution_enabled": choice})
    resolved = rc.execution_config(settings)
    env = execution.ExecutionConfig.from_env()
    assert resolved.enabled is choice
    # 镜像、网络、时限、数量、资源边界都沿用本机配置。
    assert resolved.model_dump(exclude={"enabled"}) == env.model_dump(exclude={"enabled"})
    assert (resolved.command_timeout_s, resolved.max_commands, resolved.memory_mb) == (
        env.command_timeout_s, env.max_commands, env.memory_mb)
    assert rc.to_simple_config(settings).execution == resolved


def test_config_reports_readonly_summaries_and_rejects_them_on_save(env_execution):
    env_execution(True)
    client = TestClient(web.app)
    body = client.get("/api/config").json()
    assert body["execution_enabled"] is None
    assert body["execution_effective"] == {"enabled": True, "network_mode": "bridge",
                                           "source": "environment", "default_enabled": True, "backend":"docker", "default_backend":"docker", "backend_source":"environment"}
    assert body["retry_policy"]["research"] == {"max_retries": sr._CALL_MAX_RETRIES,
                                                "max_attempts": sr._CALL_MAX_RETRIES + 1}
    assert body["retry_policy"]["summary"]["max_retries"] == body["summary_max_retries"]
    writable = {k: body[k] for k in rc.RuntimeModelSettings.model_fields}
    for readonly in ("retry_policy", "execution_effective"):
        assert client.put("/api/config", json={**writable, readonly: body[readonly]}).status_code == 400


def test_saving_switch_and_old_clients_do_not_flip_permission(env_execution):
    env_execution(True)
    client = TestClient(web.app)
    body = {k: v for k, v in client.get("/api/config").json().items() if k in rc.RuntimeModelSettings.model_fields}
    saved = client.put("/api/config", json={**body, "execution_enabled": False})
    assert saved.status_code == 200, saved.text
    assert saved.json()["execution_effective"]["enabled"] is False
    assert saved.json()["execution_effective"]["source"] == "settings"
    # 不认识这个字段的旧客户端只改别的设置：保留已选择的“关闭”。
    old_client = {k: v for k, v in body.items() if k != "execution_enabled"}
    after = client.put("/api/config", json={**old_client, "wall_timeout_s": 900}).json()
    assert after["execution_enabled"] is False and after["wall_timeout_s"] == 900
    assert json.loads(rc._CONFIG_PATH.read_text())["execution_enabled"] is False
    # 显式 null：改回跟随本机默认。
    reset = client.put("/api/config", json={**body, "execution_enabled": None}).json()
    assert reset["execution_enabled"] is None and reset["execution_effective"]["enabled"] is True


def test_status_uses_same_resolution_as_research_start(env_execution, no_docker, monkeypatch):
    env_execution(True)
    client = TestClient(web.app)
    status = client.get("/api/execution/status").json()
    assert status == {"enabled": True, "available": False, "reason": "image_missing", "network_mode": "bridge",
                      "source": "environment", "default_enabled": True, "backend":"docker", "default_backend":"docker", "backend_source":"environment", "image": "fixture-image:1"}

    monkeypatch.setattr(rc, "_cache", rc.current().model_copy(update={"execution_enabled": False}))
    status = client.get("/api/execution/status").json()
    assert status["enabled"] is False and status["reason"] == "disabled" and status["source"] == "settings"
    assert no_docker[-1].enabled is False

    started = []

    def start(query, **kwargs):
        started.append(kwargs["config"])
        return "run-execution-off"
    monkeypatch.setattr(web.REGISTRY, "start_simple_research", start)
    response = client.post("/api/research", json={"query": "q", "mode": "simple"})
    assert response.status_code == 200, response.text
    assert started[0].execution == no_docker[-1]

    monkeypatch.setattr(rc, "_cache", rc.current().model_copy(update={"execution_enabled": True}))
    env_execution(False)
    assert client.get("/api/execution/status").json()["enabled"] is True
    client.post("/api/research", json={"query": "q", "mode": "simple"})
    assert started[1].execution.enabled is True and started[1].execution == no_docker[-1]


def test_run_skills_reports_whether_catalog_was_recorded(tmp_path, monkeypatch):
    from dra.runstore import RunStore
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web, "STORE", store)
    client = TestClient(web.app)
    old = store.create_run("q", None, {"mode": "simple"})
    assert client.get(f"/api/runs/{old}/skills").json() == {
        "catalog": {"skills": {}, "selected": []}, "catalog_recorded": False, "loaded": []}
    fresh = store.create_run("q", None, {"mode": "simple"})
    bundle = {"id": "x", "skills": {"compare": {"title": "方案对比"}}, "selected": []}
    (store._dir(fresh) / "skills.json").write_text(json.dumps(bundle))
    body = client.get(f"/api/runs/{fresh}/skills").json()
    assert body["catalog_recorded"] is True and body["catalog"] == bundle and body["loaded"] == []


def test_lifespan_cleans_pool_created_after_runtime_switch(tmp_path, env_execution, monkeypatch):
    """启动关闭 → 运行中在网页开启并产生执行池 → 又关闭 → 退出：池仍要被清理。"""
    from dra import execution_pool
    from dra.runstore import RunStore
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", web.RunRegistry(store))
    monkeypatch.setattr(execution_pool, "_POOLS", {})
    reaped, closed = [], []

    class FakePool:
        closed = False

        def __init__(self, run_root, config):
            self.config = config

        def shutdown(self):
            closed.append(self.config.enabled)
    monkeypatch.setattr(execution_pool, "ExecutionPool", FakePool)
    monkeypatch.setattr(execution, "reap_orphans", lambda root, config, *a: reaped.append(config) or [])
    monkeypatch.setattr(execution, "docker_client", lambda *a, **k: pytest.fail("不应访问 Docker"))
    env_execution(False)
    with TestClient(web.app):
        monkeypatch.setattr(rc, "_cache", rc.current().model_copy(update={"execution_enabled": True}))
        execution_pool.get_pool(store.root, rc.execution_config(rc.current()))
        monkeypatch.setattr(rc, "_cache", rc.current().model_copy(update={"execution_enabled": False}))
    assert closed == [True] and execution_pool._POOLS == {}
    # 本机默认关闭：沿用原规则，不做孤儿容器扫描。
    assert reaped == []


def test_lifespan_without_pool_or_execution_never_touches_docker(tmp_path, env_execution, monkeypatch):
    from dra import execution_pool
    from dra.runstore import RunStore
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", web.RunRegistry(store))
    monkeypatch.setattr(execution_pool, "_POOLS", {})
    monkeypatch.setattr(execution, "reap_orphans", lambda *a: pytest.fail("不应扫描容器"))
    monkeypatch.setattr(execution, "docker_client", lambda *a, **k: pytest.fail("不应访问 Docker"))
    env_execution(False)
    with TestClient(web.app):
        pass


def test_lifespan_does_not_hide_broken_settings_file(tmp_path, env_execution, monkeypatch):
    """启动清理只按本机默认；损坏的网页设置不在这里被改成环境配置，研究入口照常报错。"""
    from dra import execution_pool
    from dra.runstore import RunStore
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", web.RunRegistry(store))
    monkeypatch.setattr(execution_pool, "_POOLS", {})
    env_execution(False)
    rc._CONFIG_PATH.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(rc, "_cache", None)
    with TestClient(web.app, raise_server_exceptions=False) as client:
        assert client.get("/api/execution/status").status_code == 500


def test_lifespan_keeps_environment_cleanup_when_default_enabled(tmp_path, env_execution, monkeypatch):
    from dra import execution_pool
    from dra.runstore import RunStore
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", web.RunRegistry(store))
    monkeypatch.setattr(execution_pool, "_POOLS", {})
    reaped, closed = [], []

    class FakePool:
        closed = False

        def __init__(self, run_root, config):
            pass

        def shutdown(self):
            closed.append(True)
    monkeypatch.setattr(execution_pool, "ExecutionPool", FakePool)
    monkeypatch.setattr(execution, "reap_orphans", lambda root, config, *a: reaped.append(config.enabled) or [])
    env_execution(True)
    with TestClient(web.app):
        assert reaped == [True] and len(execution_pool._POOLS) == 1
    assert reaped == [True, True] and closed == [True]


def test_backend_saved_selection_old_client_and_start_share_resolution(env_execution,no_docker,monkeypatch):
    env_execution(True)
    client=TestClient(web.app)
    body={k:v for k,v in client.get('/api/config').json().items() if k in rc.RuntimeModelSettings.model_fields}
    response=client.put('/api/config',json={**body,'execution_backend':'native'})
    assert response.status_code==200
    assert response.json()['execution_effective']['backend']=='native'
    old={k:v for k,v in body.items() if k!='execution_backend'}
    response=client.put('/api/config',json=old)
    assert response.json()['execution_backend']=='native'
    status=client.get('/api/execution/status').json()
    assert status['backend']=='native' and status['image'] is None
    seen=[]
    monkeypatch.setattr(web.REGISTRY,'start_simple_research',lambda query,**kwargs:seen.append(kwargs['config']) or 'native-run')
    assert client.post('/api/research',json={'query':'q','mode':'simple'}).status_code==200
    assert seen[0].execution==no_docker[-1]
    response=client.put('/api/config',json={**body,'execution_backend':None})
    assert response.json()['execution_effective']['backend']=='docker'
