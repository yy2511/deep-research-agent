"""管理边界、乐观锁、会话撤销及负责人单次移交证明的离线回归。"""
from datetime import timedelta
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dra.api.app import app
from dra.api.deps import SESSION_COOKIE
from dra.api.services import auth, user_management as management
from dra.api.services.errors import Conflict, Forbidden, InvalidRequest, Unauthorized
from dra.db_tables import ManagementAudit, OwnerTransferProof, PlatformGovernance, User, UserQuotaPolicy, utcnow

pytestmark = pytest.mark.real_auth
PASSWORD = "correct horse battery"


@pytest.fixture
def accounts(database):
    rows = {}
    for role in ("owner", "admin", "user", "guest"):
        row = auth.create_user(database, f"{role}@example.com", PASSWORD, role="admin" if role == "owner" else "user" if role == "guest" else role)
        rows[role] = row
    with database.session() as session:
        session.get(User, rows["guest"].id).role = "guest"
        session.delete(session.get(UserQuotaPolicy, rows["guest"].id))
    management.bootstrap_owner(database, rows["owner"].id)
    tokens = {role: auth.login(database, f"{role}@example.com", PASSWORD).token for role in rows}
    clients = {role: TestClient(app, cookies={SESSION_COOKIE: token}) for role, token in tokens.items()}
    return rows, tokens, clients


def test_management_routes_deny_non_managers_and_anonymous(accounts):
    rows, _, clients = accounts
    paths = ("/api/admin/users", f"/api/admin/users/{rows['user'].id}",
             "/api/admin/governance", "/api/admin/audit-events", "/api/admin/owner-transfer/status")
    for client, code in ((clients["user"], 403), (clients["guest"], 403), (TestClient(app), 401)):
        for path in paths:
            assert client.get(path).status_code == code
        assert client.patch(f"/api/admin/users/{rows['user'].id}/status", json={"status": "disabled", "expected_version": 0}).status_code == code


def test_list_has_stable_pagination_search_and_no_sensitive_fields(accounts):
    rows, _, clients = accounts
    client = clients["admin"]
    first = client.get("/api/admin/users?limit=2")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    second = client.get(f"/api/admin/users?limit=2&cursor={first.json()['next_cursor']}").json()
    ids = [r["id"] for r in first.json()["items"] + second["items"]]
    assert ids == sorted(row.id for row in rows.values())
    assert second["next_cursor"] is None
    found = client.get("/api/admin/users", params={"q": "user@example.com", "role": "user"}).json()["items"]
    assert [r["id"] for r in found] == [rows["user"].id]
    assert client.get("/api/admin/users", params={"q": "%"}).json()["items"] == []
    assert not ({"password_hash", "token", "query", "report"} & found[0].keys())


@pytest.mark.parametrize("target", ["owner", "admin"])
def test_admin_cannot_change_self_or_other_managers(database, accounts, target):
    rows, _, clients = accounts
    target_id = rows[target].id
    for suffix, method, body in (("status", "patch", {"status": "disabled", "expected_version": 0}),
                                 ("role", "patch", {"role": "guest", "expected_version": 0}),
                                 ("quota", "put", {"mode": "unlimited", "expected_management_version": 0})):
        assert getattr(clients["admin"], method)(f"/api/admin/users/{target_id}/{suffix}", json=body).status_code == 403
    with database.session() as session:
        assert session.get(User, target_id).status == "active"


def test_admin_must_explicitly_grant_finite_quota_and_cannot_promote_admin(accounts):
    rows, _, clients = accounts
    url = f"/api/admin/users/{rows['guest'].id}/role"
    client = clients["admin"]
    assert client.patch(url, json={"role": "user", "expected_version": 0}).status_code == 400
    assert client.patch(url, json={"role": "admin", "expected_version": 0}).status_code == 403
    assert client.patch(url, json={"role": "user", "expected_version": 0, "quota": {"mode": "unlimited"}}).status_code == 403
    upgraded = client.patch(url, json={"role": "user", "expected_version": 0,
        "quota": {"mode": "limited", "daily_runs_limit": 2, "daily_briefs_limit": 3}})
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["quota"]["daily_runs_limit"] == 2
    assert clients["guest"].get("/api/me").status_code == 401
    assert client.patch(url, json={"role": "guest", "expected_version": 0}).status_code == 409


def test_quota_update_preserves_session_and_rejects_stale_or_unknown_fields(accounts):
    rows, _, clients = accounts
    url = f"/api/admin/users/{rows['user'].id}/quota"
    body = {"mode": "limited", "daily_runs_limit": 0, "daily_briefs_limit": 3,
            "expected_version": 0, "expected_management_version": 0}
    response = clients["admin"].put(url, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["quota"]["version"] == 1
    assert clients["user"].get("/api/me").status_code == 200
    assert clients["admin"].put(url, json=body).status_code == 409
    assert clients["owner"].put(url, json={**body, "role": "owner"}).status_code == 422
    assert clients["admin"].put(url, json={**body, "daily_runs_limit": True}).status_code == 422
    assert clients["admin"].put(f"/api/admin/users/{rows['guest'].id}/quota", json=body).status_code == 400


def transfer_proof(db, rows, tokens):
    return management.prepare_transfer(db, rows["owner"].id, tokens["owner"], target_id=rows["admin"].id,
        expected_governance_version=1, expected_actor_version=1, expected_target_version=0,
        provider="password", password=PASSWORD)["proof"]


def test_transfer_is_atomic_revokes_both_sessions_and_cannot_replay(database, accounts):
    rows, tokens, clients = accounts
    proof = transfer_proof(database, rows, tokens)
    assert management.transfer_status(database, rows["owner"].id, tokens["owner"], proof)["ready"]
    assert management.transfer_owner(database, rows["owner"].id, tokens["owner"], proof) == {"transferred": True}
    with database.session() as s:
        assert s.get(PlatformGovernance, 1).owner_user_id == rows["admin"].id
        assert s.get(User, rows["owner"].id).role == "admin"
        assert s.get(User, rows["admin"].id).role == "owner"
        assert len(list(s.scalars(select(User).where(User.role == "owner")))) == 1
        event = s.scalar(select(ManagementAudit).where(ManagementAudit.action == "owner_transfer"))
        assert event is not None and "password" not in event.after_json and proof not in event.after_json
    for role in ("owner", "admin"):
        assert clients[role].get("/api/me").status_code == 401
    with pytest.raises(Unauthorized):
        management.transfer_owner(database, rows["owner"].id, tokens["owner"], proof)


@pytest.mark.parametrize("invalidation", ["expired", "revoked", "target_changed", "governance_changed", "other_session"])
def test_transfer_rejects_stale_session_target_version_or_expiry(database, accounts, invalidation):
    rows, tokens, _ = accounts
    proof = transfer_proof(database, rows, tokens)
    token = tokens["owner"]
    with database.session() as s:
        if invalidation == "expired":
            s.get(OwnerTransferProof, auth.hash_secret(proof)).expires_at = utcnow() - timedelta(seconds=1)
        elif invalidation == "target_changed":
            s.get(User, rows["admin"].id).management_version += 1
        elif invalidation == "governance_changed":
            s.get(PlatformGovernance, 1).version += 1
    if invalidation == "revoked":
        auth.logout(database, token)
    if invalidation == "other_session":
        token = auth.login(database, "owner@example.com", PASSWORD).token
    with pytest.raises((Conflict, Unauthorized)):
        management.transfer_owner(database, rows["owner"].id, token, proof)
    with database.session() as s:
        assert s.get(PlatformGovernance, 1).owner_user_id == rows["owner"].id


def test_bootstrap_race_has_one_owner(database):
    users = [auth.create_user(database, f"admin{i}@example.com", PASSWORD, role="admin") for i in range(2)]
    barrier, results = threading.Barrier(2), []
    def attempt(user):
        barrier.wait()
        try:
            management.bootstrap_owner(database, user.id)
            results.append("ok")
        except Conflict:
            results.append("conflict")
    threads = [threading.Thread(target=attempt, args=(user,)) for user in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == ["conflict", "ok"]
    with database.session() as s:
        assert len(list(s.scalars(select(User).where(User.role == "owner")))) == 1


def test_cli_cannot_bypass_owner_protection(database, accounts):
    with pytest.raises(Forbidden):
        auth.set_role(database, "guest", email="owner@example.com")
    with pytest.raises(Forbidden):
        auth.set_user_status(database, "owner@example.com", "disabled")
    with pytest.raises(InvalidRequest):
        auth.set_role(database, "owner", email="admin@example.com")


def test_bigint_limits_roundtrip_without_javascript_precision_loss(accounts):
    rows, _, clients = accounts
    url = f"/api/admin/users/{rows['user'].id}/quota"
    body = {"mode": "limited", "daily_runs_limit": "9223372036854775807", "daily_briefs_limit": "9007199254740993",
            "expected_version": 0, "expected_management_version": 0}
    result = clients["owner"].put(url, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["quota"]["daily_runs_limit"] == body["daily_runs_limit"]
    assert result.json()["quota"]["daily_briefs_limit"] == body["daily_briefs_limit"]
    assert result.json()["usage"]["runs_left"] == body["daily_runs_limit"]
    assert clients["user"].get("/api/me").json()["quota"]["briefs_left"] == body["daily_briefs_limit"]
    for value in ("9223372036854775808", "-1", "1.5", "1e3", True):
        assert clients["owner"].put(url, json={**body, "daily_runs_limit": value, "expected_version": 1}).status_code == 422


def test_github_transfer_must_match_bound_identity_and_be_verified(database, accounts):
    from dra.db_tables import UserIdentity
    rows, tokens, _ = accounts
    with database.session() as session:
        session.add(UserIdentity(user_id=rows["owner"].id, provider="github", subject="owner-subject", login="owner-login"))
    result = management.prepare_transfer(database, rows["owner"].id, tokens["owner"], target_id=rows["admin"].id,
        expected_governance_version=1, expected_actor_version=1, expected_target_version=0, provider="github")
    proof = result["proof"]
    with pytest.raises(Unauthorized):
        management.transfer_owner(database, rows["owner"].id, tokens["owner"], proof)
    with pytest.raises(Unauthorized):
        management.verify_github_transfer(database, proof, tokens["owner"], auth.ExternalProfile(provider="github", subject="attacker", login="attacker"))
    profile = auth.ExternalProfile(provider="github", subject="owner-subject", login="changed-name")
    management.verify_github_transfer(database, proof, tokens["owner"], profile)
    with pytest.raises(Unauthorized):
        management.verify_github_transfer(database, proof, tokens["owner"], profile)
    assert management.transfer_owner(database, rows["owner"].id, tokens["owner"], proof)["transferred"]
