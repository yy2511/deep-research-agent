"""管理员权限（#32）：只有管理员能改全站设置；普通用户照常读取、选技能、选外部资料。

设计 docs/design/阶段1-数据库与用户.md §4。走真实的登录与 Cookie（real_auth）。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from dra import web
from dra.api.deps import ADMIN_ONLY, SESSION_COOKIE
from dra.api.routers import providers, settings, skills
from dra.api.services import auth

pytestmark = pytest.mark.real_auth

PASSWORD = "correct horse battery"

# 设计 §4 表格：只有管理员能调用的接口。
ADMIN_OPERATIONS = {
    ("PUT", "/api/config"), ("POST", "/api/config/test"),
    ("PUT", "/api/mcp/config"), ("POST", "/api/mcp/test"),
    ("POST", "/api/providers"), ("PUT", "/api/providers/{provider}"), ("DELETE", "/api/providers/{provider}"),
    ("POST", "/api/providers/refresh"), ("POST", "/api/providers/{provider}/refresh"),
    ("PUT", "/api/providers/{provider}/model-metadata"), ("POST", "/api/model-catalog/refresh"),
    ("PUT", "/api/skills/enabled"), ("PUT", "/api/skills/packages/{name}"),
}
# 这几个 router 里，所有登录用户都能调的非 GET 接口：发起研究前准备所选技能。
OPEN_WRITES = {("POST", "/api/skills/prepare")}

# 被拦下的请求不能走到真正改设置、连外部服务的那一步。
_WRITERS = ("dra.runtime_config.save", "dra.mcp_client.save_settings", "dra.mcp_client.discover", "dra.llm.chat",
            "dra.provider_registry.save_connection", "dra.provider_registry.delete_connection",
            "dra.provider_registry.refresh_provider", "dra.provider_registry.save_override",
            "dra.provider_registry.refresh_catalog",
            "dra.api.services.skills.set_enabled", "dra.api.services.skills.save")


@pytest.fixture
def clients(database):
    def login(email, role):
        auth.create_user(database, email, PASSWORD, role=role)
        return TestClient(web.app, cookies={SESSION_COOKIE: auth.login(database, email, PASSWORD).token})

    return {"user": login("user@example.com", "user"), "admin": login("boss@example.com", "admin"),
            "anonymous": TestClient(web.app)}


@pytest.fixture
def writers_must_not_run(monkeypatch):
    calls = []
    for target in _WRITERS:
        monkeypatch.setattr(target, lambda *a, _t=target, **k: calls.append(_t))
    return calls


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_regular_users_cannot_change_global_settings(clients, writers_must_not_run):
    for method, path in sorted(ADMIN_OPERATIONS):
        response = clients["user"].request(method, _concrete(path), json={})
        assert (response.status_code, response.json()) == (403, {"detail": "需要管理员权限"}), (method, path)
    assert writers_must_not_run == []


def test_anonymous_requests_still_get_401_not_403(clients):
    for method, path in sorted(ADMIN_OPERATIONS):
        assert clients["anonymous"].request(method, _concrete(path), json={}).status_code == 401, (method, path)


def test_admins_can_still_change_settings(clients):
    admin = clients["admin"]
    current = admin.get("/api/config").json()
    body = {key: current[key] for key in ("planner", "subagent")}
    saved = admin.put("/api/config", json=body)
    assert saved.status_code == 200, saved.text
    # 只校验正文的接口：管理员通过了权限检查，到的是参数校验（400/422），不是 403。
    assert admin.put("/api/providers/x/model-metadata", json={"bad": 1}).status_code in (400, 422)


def test_regular_users_can_read_settings_and_use_skills(clients, monkeypatch):
    import dra.execution

    monkeypatch.setattr(dra.execution, "availability", lambda config: {"available": False, "reason": "disabled"})
    user = clients["user"]
    for path in ("/api/config", "/api/mcp/config", "/api/providers", "/api/skills", "/api/execution/status"):
        assert user.get(path).status_code == 200, path
    assert user.post("/api/skills/prepare", json={"selected": []}).status_code == 200


def test_every_settings_write_sits_in_an_admin_group():
    """结构检查（第二道保险）：设置、服务商、技能 router 里会改东西的接口，全部在要求管理员的分组里。

    以后在这几个文件里加修改接口却忘了放进 admin_router，这里会失败。只用公开的 APIRouter.routes。
    """
    def operations(router):
        return {(method, route.path) for route in router.routes for method in route.methods}

    admin_groups = (settings.admin_router, providers.admin_router, skills.admin_router)
    for group in admin_groups:
        assert [d.dependency for d in group.dependencies] == [d.dependency for d in ADMIN_ONLY]
    assert set().union(*map(operations, admin_groups)) == ADMIN_OPERATIONS
    open_writes = {(method, path) for router in (settings.router, providers.router, skills.router)
                   for method, path in operations(router) if method not in {"GET", "HEAD"}}
    assert open_writes == OPEN_WRITES
