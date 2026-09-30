"""真实配置测试入口；仅显式 --run-live 时执行。"""
import pytest

@pytest.mark.live
def test_config_test_endpoint_real_call_live():
    from fastapi.testclient import TestClient

    import dra.web as W
    r = TestClient(W.app).post("/api/config/test", json={
        "model": "deepseek-v4-flash", "provider": "opencode", "reasoning": False, "effort": None,
    })
    print(f"\n[live·config test] {r.json()}")
    assert r.json()["ok"] is True
