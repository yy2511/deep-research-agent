"""Owner-only timezone scheduling and the next-old-midnight boundary."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dra import runtime_config
from dra.api.app import app
from dra.api.deps import SESSION_COOKIE
from dra.api.services import auth, quota_timezone
from dra.api.services.errors import Conflict, Forbidden
from dra.db_tables import ManagementAudit, PlatformGovernance, User

pytestmark = pytest.mark.real_auth


def identities(database):
    admin = auth.create_user(database, "admin@example.com", "correct horse battery", role="admin")
    owner = auth.create_user(database, "owner@example.com", "correct horse battery", role="admin")
    with database.session() as session:
        row = session.get(User, owner.id)
        row.role = "owner"
        session.get(PlatformGovernance, 1).owner_user_id = owner.id
    return owner.id, admin.id


def test_timezone_only_owner_can_schedule_and_admin_can_read(database):
    owner, admin = identities(database)
    state = quota_timezone.status(database, admin)
    with pytest.raises(Forbidden):
        quota_timezone.schedule(database, admin, "Asia/Shanghai", state["version"])
    changed = quota_timezone.schedule(database, owner, "Asia/Tokyo", state["version"])
    assert changed["pending_timezone"] == "Asia/Tokyo"
    assert quota_timezone.status(database, admin)["pending_timezone"] == "Asia/Tokyo"
    with database.session() as session:
        assert session.scalar(select(ManagementAudit).where(ManagementAudit.action == "quota_timezone")) is not None


def test_timezone_activates_at_old_day_boundary_and_rejects_stale_version(database, monkeypatch):
    owner, _ = identities(database)
    with database.session() as session:
        session.get(PlatformGovernance, 1).quota_timezone = "UTC"
    now = datetime(2026, 11, 12, 14, 30, tzinfo=UTC)
    monkeypatch.setattr(quota_timezone, "utcnow", lambda: now)
    state = quota_timezone.schedule(database, owner, "Asia/Shanghai", 0)
    boundary = datetime(2026, 11, 13, tzinfo=UTC)
    assert state["timezone"] == "UTC"
    assert state["effective_at"] == boundary
    with pytest.raises(Conflict):
        quota_timezone.schedule(database, owner, "Asia/Tokyo", 0)
    with database.session() as session:
        assert quota_timezone.effective_timezone_in_session(session, now=boundary-timedelta(seconds=1)) == "UTC"
    with database.session() as session:
        assert quota_timezone.effective_timezone_in_session(session, now=boundary) == "Asia/Shanghai"
    assert quota_timezone.status(database, owner)["pending_timezone"] is None


def test_schedule_can_be_cancelled_without_resetting_timezone(database):
    owner, _ = identities(database)
    state = quota_timezone.status(database, owner)
    pending = quota_timezone.schedule(database, owner, "America/New_York", state["version"])
    cancelled = quota_timezone.schedule(database, owner, state["timezone"], pending["version"])
    assert cancelled["pending_timezone"] is None
    assert cancelled["effective_at"] is None
    assert cancelled["timezone"] == state["timezone"]


def test_config_route_cannot_bypass_timezone_owner_rule(database):
    _, admin = identities(database)
    token = auth.login(database, "admin@example.com", "correct horse battery").token
    client = TestClient(app, cookies={SESSION_COOKIE: token})
    current = runtime_config.current().model_dump(mode="json")
    before = runtime_config.current().trial.timezone
    current["trial"]["timezone"] = "America/New_York" if before != "America/New_York" else "UTC"
    assert client.put("/api/config", json=current).status_code == 403
    assert runtime_config.current().trial.timezone == before
    status = client.get("/api/admin/quota-timezone")
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert client.put("/api/admin/quota-timezone", json={"timezone": "UTC", "expected_version": 0}).status_code == 403
