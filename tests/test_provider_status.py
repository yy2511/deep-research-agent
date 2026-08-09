"""provider 可用性探测：key 缺失/网络失败不拖垮整体，缓存不重复探测。"""
from unittest.mock import MagicMock, patch

import pytest

from dra import llm


def test_key_not_configured_short_circuits(monkeypatch, tmp_path):
    # check_provider 内部会 load_dotenv(override=True) 兜底"进程里还没人加载过 .env"
    # 的场景（见该函数 docstring）；这里把 _ENV_PATH 指到不存在的文件，让它读不到
    # 真实 .env、也就不会把下面这行 delenv 模拟的"没配 key"状态覆盖回真实 key。
    monkeypatch.setattr(llm, "_ENV_PATH", tmp_path / "does_not_exist.env")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    status = llm.check_provider("deepseek")
    assert status.key_configured is False
    assert status.reachable is False


def test_reachable_provider_returns_model_list(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    fake_client = MagicMock()
    fake_client.models.list.return_value = MagicMock(
        data=[MagicMock(id="deepseek-v4-flash"), MagicMock(id="deepseek-v4-pro")]
    )
    with patch.object(llm, "_get_client", return_value=fake_client):
        status = llm.check_provider("deepseek")
    assert status.key_configured is True
    assert status.reachable is True
    assert status.models == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_unreachable_provider_reports_error_not_raise(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    with patch.object(llm, "_get_client", side_effect=RuntimeError("网络超时")):
        status = llm.check_provider("deepseek")
    assert status.key_configured is True
    assert status.reachable is False
    assert "网络超时" in status.error


def test_list_provider_status_partial_failure_does_not_raise(monkeypatch):
    """check_provider 自己吞异常返回 reachable=False；list 层不应该再看到异常冒出来。"""
    monkeypatch.setattr(llm, "_provider_cache", None)

    def fake_check(name):
        if name == "codex_local":
            return llm.ProviderStatus(name=name, key_configured=True, reachable=False, error="down")
        return llm.ProviderStatus(name=name, key_configured=True, reachable=True, models=["m"])

    with patch.object(llm, "check_provider", side_effect=fake_check):
        statuses = llm.list_provider_status()
    assert len(statuses) == len(llm._PROVIDERS)
    down = next(s for s in statuses if s.name == "codex_local")
    assert down.reachable is False


def test_list_provider_status_uses_cache_unless_force_refresh(monkeypatch):
    monkeypatch.setattr(llm, "_provider_cache", None)
    calls = []

    def fake_check(name):
        calls.append(name)
        return llm.ProviderStatus(name=name, key_configured=True, reachable=True, models=[])

    with patch.object(llm, "check_provider", side_effect=fake_check):
        llm.list_provider_status()
        llm.list_provider_status()
        assert len(calls) == len(llm._PROVIDERS)
        llm.list_provider_status(force_refresh=True)
        assert len(calls) == 2 * len(llm._PROVIDERS)


def test_force_refresh_clears_client_cache(monkeypatch):
    """force_refresh 清 _clients：改了已存在的 key 后，重探要用新 key 重建 client
    （否则旧 key 的缓存 client 一直被 _get_client 复用，refresh 也白搭）。"""
    monkeypatch.setattr(llm, "_provider_cache", None)
    monkeypatch.setattr(llm, "_clients", {"deepseek": MagicMock()})  # 模拟已缓存旧 key 的 client
    with patch.object(llm, "check_provider",
                      return_value=llm.ProviderStatus(name="x", key_configured=True, reachable=True, models=[])):
        llm.list_provider_status(force_refresh=True)
    assert llm._clients == {}   # 已被清空 → 下次 _get_client 会用 .env 现值重建


@pytest.mark.live
def test_check_provider_reaches_all_configured_providers_live():
    """5 个 provider 的 models.list() 真实连通性——量级核对 spec §4 实测表格
    （deepseek=2/codex_local=4/codex521=16/opencode=20/openai=509，数字可能随
    厂商上新变化，不锁死绝对值，只断言"配了 key 就该连得通、有模型"）。"""
    for name in llm._PROVIDERS:
        status = llm.check_provider(name)
        print(f"\n[live·provider] {name}: key_configured={status.key_configured} "
              f"reachable={status.reachable} n_models={len(status.models)}")
        if status.key_configured:
            assert status.reachable, f"{name} 配了 key 但连不通：{status.error}"
            assert len(status.models) > 0


@pytest.mark.live
def test_config_test_endpoint_real_call_live():
    from fastapi.testclient import TestClient

    import dra.web as W
    r = TestClient(W.app).post("/api/config/test", json={
        "model": "deepseek-v4-flash", "provider": "opencode", "reasoning": False, "effort": None,
    })
    print(f"\n[live·config test] {r.json()}")
    assert r.json()["ok"] is True
