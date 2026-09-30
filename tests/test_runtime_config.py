"""runtime_config 当前版本、损坏保留与 BriefConfig 映射。"""
import json
import pytest

from dra import runtime_config as rc
from dra.runtime_config import NodeModelConfig, PlannerModelConfig, RuntimeModelSettings


def _settings(**overrides) -> RuntimeModelSettings:
    base = dict(
        planner=PlannerModelConfig(model="glm-5.2", provider="opencode"),
        subagent=NodeModelConfig(model="deepseek-v4-flash", provider="opencode"),
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


@pytest.mark.parametrize("version", [None, 1, 2, 3])
def test_old_file_is_rejected_without_rewriting(tmp_path, monkeypatch, version):
    path = tmp_path / "runtime_config.json"
    payload = _settings().model_dump()
    if version is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = version
    original = json.dumps(payload)
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(rc, "_CONFIG_PATH", path)
    monkeypatch.setattr(rc, "_cache", None)

    with pytest.raises(ValueError, match="原配置已保留"):
        rc.load()
    assert path.read_text(encoding="utf-8") == original


def test_load_falls_back_to_code_defaults_when_file_missing(tmp_path, monkeypatch):
    path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(rc, "_CONFIG_PATH", path)
    monkeypatch.setattr(rc, "_cache", None)
    settings = rc.load()
    assert settings.subagent.model
    assert path.exists()


def test_load_preserves_corrupt_file_and_fails_explicitly(tmp_path, monkeypatch):
    bad = tmp_path / "runtime_config.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(rc, "_CONFIG_PATH", bad)
    monkeypatch.setattr(rc, "_cache", None)
    import pytest
    with pytest.raises(ValueError, match="原配置已保留"):
        rc.load()
    assert bad.read_text() == "{not valid json"


def test_current_reads_cache_without_touching_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "unused.json")
    settings = _settings()
    monkeypatch.setattr(rc, "_cache", settings)
    assert rc.current() is settings


def test_to_brief_config_maps_fields():
    settings = _settings(
        planner=PlannerModelConfig(model="gpt-5.5", provider="codex521", effort="high"),
    )
    cfg = rc.to_brief_config(settings)
    assert cfg.planner_model == "gpt-5.5"
    assert cfg.planner_provider == "codex521"
    assert cfg.planner_effort == "high"
    assert rc.to_subagent_config(settings).model == "deepseek-v4-flash"


def test_to_subagent_config_rejects_effort_without_reasoning():
    settings = _settings(
        subagent=NodeModelConfig(model="x", provider="opencode", reasoning=False, effort="high"),
    )
    try:
        rc.to_subagent_config(settings)
        assert False, "应该抛 ValidationError"
    except Exception as e:
        assert "reasoning" in str(e)


def test_to_brief_config_passes_through_overrides():
    cfg = rc.to_brief_config(_settings(), max_initial_tasks=3)
    assert cfg.max_initial_tasks == 3
