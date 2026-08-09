"""runtime_config 往返一致性 + fail-soft + OrchestratorConfig 映射正确性。"""
from dra import runtime_config as rc
from dra.runtime_config import NodeModelConfig, PlannerModelConfig, RuntimeModelSettings


def _settings(**overrides) -> RuntimeModelSettings:
    base = dict(
        planner=PlannerModelConfig(model="glm-5.2", provider="opencode"),
        subagent=NodeModelConfig(model="deepseek-v4-flash", provider="opencode"),
        writer=NodeModelConfig(model="deepseek-v4-pro", provider="opencode"),
    )
    base.update(overrides)
    return RuntimeModelSettings(**base)


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "runtime_config.json")
    monkeypatch.setattr(rc, "_cache", None)
    settings = _settings(planner=PlannerModelConfig(model="gpt-5.5", provider="codex521"))
    rc.save(settings)
    monkeypatch.setattr(rc, "_cache", None)
    assert rc.load() == settings


def test_v1_file_is_migrated_once_without_losing_models(tmp_path, monkeypatch):
    path = tmp_path / "runtime_config.json"
    payload = _settings().model_dump(exclude={"schema_version", "enable_cross_worker_audit"})
    payload["enable_global_audit"] = True  # 唯一允许出现旧键的显式迁移测试
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    monkeypatch.setattr(rc, "_CONFIG_PATH", path)
    monkeypatch.setattr(rc, "_cache", None)

    migrated = rc.load()

    assert migrated.schema_version == 2
    assert migrated.enable_cross_worker_audit is True
    assert migrated.planner.model == payload["planner"]["model"]
    rewritten = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == 2
    assert "enable_global_audit" not in rewritten


def test_load_falls_back_to_code_defaults_when_file_missing(tmp_path, monkeypatch):
    path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(rc, "_CONFIG_PATH", path)
    monkeypatch.setattr(rc, "_cache", None)
    settings = rc.load()
    assert settings.subagent.model
    assert path.exists()


def test_load_falls_back_when_file_corrupted(tmp_path, monkeypatch):
    bad = tmp_path / "runtime_config.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(rc, "_CONFIG_PATH", bad)
    monkeypatch.setattr(rc, "_cache", None)
    settings = rc.load()
    assert settings.subagent.model


def test_current_reads_cache_without_touching_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "unused.json")
    settings = _settings()
    monkeypatch.setattr(rc, "_cache", settings)
    assert rc.current() is settings


def test_to_orchestrator_config_maps_fields():
    settings = _settings(
        planner=PlannerModelConfig(model="gpt-5.5", provider="codex521", effort="high"),
    )
    cfg = rc.to_orchestrator_config(settings)
    assert cfg.planner_model == "gpt-5.5"
    assert cfg.planner_provider == "codex521"
    assert cfg.planner_effort == "high"
    assert cfg.subagent.model == "deepseek-v4-flash"
    assert cfg.writer_model == "deepseek-v4-pro"
    # Web 只覆盖模型字段，控制流必须继承 OrchestratorConfig 的当前默认值。
    assert "enable_draft" not in type(cfg).model_fields
    assert "enable_second_opinion" not in type(cfg).model_fields
    assert "max_replan" not in type(cfg).model_fields
    assert "enable_gap_replan" not in type(cfg).model_fields


def test_to_orchestrator_config_rejects_effort_without_reasoning():
    settings = _settings(
        writer=NodeModelConfig(model="x", provider="opencode", reasoning=False, effort="high"),
    )
    try:
        rc.to_orchestrator_config(settings)
        assert False, "应该抛 ValidationError"
    except Exception as e:
        assert "reasoning" in str(e)


def test_to_orchestrator_config_passes_through_overrides():
    cfg = rc.to_orchestrator_config(_settings(), max_initial_tasks=3)
    assert cfg.max_initial_tasks == 3
