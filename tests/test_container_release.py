from __future__ import annotations

import json
import stat
import sys
import urllib.error
from importlib import import_module
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
container_release = import_module("deploy.container_release")

ReleaseError = container_release.ReleaseError
Settings = container_release.Settings
_atomic_write = container_release._atomic_write
_read_json = container_release._read_json
_release_env_path = container_release._release_env_path
_release_record_path = container_release._release_record_path
_write_json = container_release._write_json
release_env_text = container_release.release_env_text
validate_coordinates = container_release.validate_coordinates


REVISION = "1" * 40
DIGEST = "sha256:" + "2" * 64
TAG = f"ghcr.io/yy2511/deep-research-agent:sha-{REVISION}"


def test_validate_coordinates_requires_full_immutable_tag_and_digest() -> None:
    revision, image_ref = validate_coordinates(TAG, DIGEST)

    assert revision == REVISION
    assert image_ref == f"{TAG}@{DIGEST}"

    for invalid_tag in (
        "ghcr.io/yy2511/deep-research-agent:main",
        "ghcr.io/yy2511/deep-research-agent:sha-deadbeef",
        f"docker.io/yy2511/deep-research-agent:sha-{REVISION}",
    ):
        with pytest.raises(ReleaseError, match="image must be"):
            validate_coordinates(invalid_tag, DIGEST)

    with pytest.raises(ReleaseError, match="digest must be"):
        validate_coordinates(TAG, "sha256:short")


def test_release_env_separates_app_secrets_and_pins_runtime_identity(
    tmp_path: Path,
) -> None:
    settings = Settings(app_root=tmp_path, runtime_uid=1000, runtime_gid=1000)

    content = release_env_text(settings, f"{TAG}@{DIGEST}")

    assert f"DRA_APP_ENV_FILE={tmp_path}/shared/.env\n" in content
    assert f"DRA_HOST_DATA_DIR={tmp_path}/shared\n" in content
    assert f"DRA_IMAGE={TAG}@{DIGEST}\n" in content
    assert "DRA_RUNTIME_UID=1000\n" in content
    assert "DRA_RUNTIME_GID=1000\n" in content
    assert "DRA_RUNTIME_HOME=/tmp\n" in content
    assert "API_KEY" not in content


def test_atomic_state_files_are_private_and_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "container" / "active.json"
    value = {"kind": "container", "revision": REVISION}

    _write_json(state_path, value)

    assert _read_json(state_path) == value
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    _atomic_write(state_path, json.dumps({"kind": "systemd"}) + "\n")
    assert _read_json(state_path) == {"kind": "systemd"}


def test_release_paths_are_revision_scoped(tmp_path: Path) -> None:
    settings = Settings(app_root=tmp_path)

    assert _release_env_path(settings, REVISION) == (
        tmp_path / "container" / "releases" / f"{REVISION}.env"
    )
    assert _release_record_path(settings, REVISION) == (
        tmp_path / "container" / "releases" / f"{REVISION}.json"
    )


def _write_verified_release(settings: Settings) -> None:
    _write_json(
        _release_record_path(settings, REVISION),
        {
            "digest": DIGEST,
            "image_ref": f"{TAG}@{DIGEST}",
            "revision": REVISION,
            "staged_at": "2026-07-30T00:00:00+00:00",
            "tag": TAG,
            "verified_at": "2026-07-30T00:01:00+00:00",
        },
    )


def test_activate_from_systemd_records_reversible_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(app_root=tmp_path)
    _write_verified_release(settings)
    events: list[object] = []
    monkeypatch.setattr(
        container_release, "_current_state", lambda _settings: {"kind": "systemd"}
    )
    monkeypatch.setattr(
        container_release, "_stop_systemd", lambda: events.append("stop-systemd")
    )
    monkeypatch.setattr(
        container_release,
        "_start_release",
        lambda _settings, revision: events.append(("start-release", revision)),
    )

    container_release.activate(settings, REVISION)

    assert events == ["stop-systemd", ("start-release", REVISION)]
    assert _read_json(settings.previous_state_file) == {"kind": "systemd"}
    assert _read_json(settings.active_state_file) == {
        "kind": "container",
        "revision": REVISION,
    }
    assert not settings.pending_state_file.exists()


def test_failed_activation_restores_systemd_and_clears_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(app_root=tmp_path)
    _write_verified_release(settings)
    restored: list[dict[str, str]] = []
    monkeypatch.setattr(
        container_release, "_current_state", lambda _settings: {"kind": "systemd"}
    )
    monkeypatch.setattr(container_release, "_stop_systemd", lambda: None)
    monkeypatch.setattr(container_release, "_compose_running", lambda _settings: False)
    monkeypatch.setattr(
        container_release,
        "_start_release",
        lambda _settings, _revision: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        container_release,
        "_restore",
        lambda _settings, state: restored.append(state),
    )

    with pytest.raises(RuntimeError, match="boom"):
        container_release.activate(settings, REVISION)

    assert restored == [{"kind": "systemd"}]
    assert not settings.pending_state_file.exists()
    assert not settings.active_state_file.exists()
    assert not settings.previous_state_file.exists()


def test_rollback_from_container_to_systemd_swaps_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(app_root=tmp_path)
    current = {"kind": "container", "revision": REVISION}
    _write_json(settings.active_state_file, current)
    _write_json(settings.previous_state_file, {"kind": "systemd"})
    events: list[object] = []
    monkeypatch.setattr(
        container_release, "_current_state", lambda _settings: current.copy()
    )
    monkeypatch.setattr(
        container_release,
        "_stop_release",
        lambda _settings, revision: events.append(("stop-release", revision)),
    )
    monkeypatch.setattr(
        container_release, "_start_systemd", lambda: events.append("start-systemd")
    )

    container_release.rollback(settings)

    assert events == [("stop-release", REVISION), "start-systemd"]
    assert _read_json(settings.active_state_file) == {"kind": "systemd"}
    assert _read_json(settings.previous_state_file) == current
    assert not settings.pending_state_file.exists()


def test_http_wait_tolerates_startup_connection_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[object] = [
        urllib.error.URLError(ConnectionRefusedError(61, "refused")),
        200,
    ]

    def fake_status(_url: str) -> int:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return int(outcome)

    monkeypatch.setattr(container_release, "_url_status", fake_status)
    monkeypatch.setattr(container_release.time, "sleep", lambda _seconds: None)

    container_release._wait_for_http_200("http://127.0.0.1:8765/", timeout=30)

    assert outcomes == []


def test_systemd_switch_changes_boot_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    waits: list[tuple[str, float]] = []
    monkeypatch.setattr(
        container_release,
        "_run",
        lambda command, **_kwargs: commands.append(list(command)),
    )
    monkeypatch.setattr(
        container_release,
        "_wait_for_http_200",
        lambda url, *, timeout: waits.append((url, timeout)),
    )

    container_release._stop_systemd()
    container_release._start_systemd()

    assert commands == [
        ["sudo", "-n", "systemctl", "disable", "--now", "dra-web"],
        ["sudo", "-n", "systemctl", "enable", "--now", "dra-web"],
    ]
    assert waits == [("http://127.0.0.1:8765/", 30)]
