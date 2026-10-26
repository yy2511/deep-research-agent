#!/usr/bin/env python3
"""Stage, activate, and roll back immutable DRA container releases.

This script is installed on the production host and intentionally uses only the
Python standard library.  It never reads or prints application secrets; Compose
receives their existing file path through DRA_APP_ENV_FILE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


IMAGE_PATTERN = re.compile(
    r"^ghcr\.io/yy2511/deep-research-agent:sha-([0-9a-f]{40})$"
)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SYSTEMD_STATE = {"kind": "systemd"}


class ReleaseError(RuntimeError):
    """A safe, user-facing release error."""


@dataclass(frozen=True)
class Settings:
    app_root: Path
    project_name: str = "deep-research-agent"
    canary_project: str = "deep-research-agent-canary"
    canary_port: int = 18766
    runtime_uid: int = 1000
    runtime_gid: int = 1000

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            app_root=Path(
                os.environ.get(
                    "DRA_DEPLOY_ROOT", "/home/deploy/apps/deep-research-agent"
                )
            ),
            canary_port=int(os.environ.get("DRA_CANARY_PORT", "18766")),
            runtime_uid=int(os.environ.get("DRA_RUNTIME_UID", "1000")),
            runtime_gid=int(os.environ.get("DRA_RUNTIME_GID", "1000")),
        )

    @property
    def runtime_dir(self) -> Path:
        return self.app_root / "container"

    @property
    def releases_dir(self) -> Path:
        return self.runtime_dir / "releases"

    @property
    def compose_file(self) -> Path:
        return self.runtime_dir / "compose.yaml"

    @property
    def app_env_file(self) -> Path:
        return self.app_root / "shared" / ".env"

    @property
    def data_dir(self) -> Path:
        return self.app_root / "shared"

    @property
    def active_state_file(self) -> Path:
        return self.runtime_dir / "active.json"

    @property
    def previous_state_file(self) -> Path:
        return self.runtime_dir / "previous.json"

    @property
    def pending_state_file(self) -> Path:
        return self.runtime_dir / "pending.json"


def _run(
    command: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=check,
        capture_output=capture_output,
        env=env,
        text=True,
    )


def validate_coordinates(tag: str, digest: str) -> tuple[str, str]:
    image_match = IMAGE_PATTERN.fullmatch(tag)
    if not image_match:
        raise ReleaseError(
            "image must be ghcr.io/yy2511/deep-research-agent:sha-<40 lowercase hex>"
        )
    if not DIGEST_PATTERN.fullmatch(digest):
        raise ReleaseError("digest must be sha256:<64 lowercase hex>")
    return image_match.group(1), f"{tag}@{digest}"


def release_env_text(settings: Settings, image_ref: str) -> str:
    values = {
        "DRA_APP_ENV_FILE": str(settings.app_env_file),
        "DRA_HOST_DATA_DIR": str(settings.data_dir),
        "DRA_IMAGE": image_ref,
        "DRA_MAX_ACTIVE_RUNS": "1",
        "DRA_RUNTIME_GID": str(settings.runtime_gid),
        "DRA_RUNTIME_HOME": "/tmp",
        "DRA_RUNTIME_UID": str(settings.runtime_uid),
        "DRA_WEB_PORT": "8765",
    }
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReleaseError(f"missing state file: {path}") from error
    except json.JSONDecodeError as error:
        raise ReleaseError(f"invalid state file: {path}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"invalid state file: {path}")
    return value


def _release_record_path(settings: Settings, revision: str) -> Path:
    return settings.releases_dir / f"{revision}.json"


def _release_env_path(settings: Settings, revision: str) -> Path:
    return settings.releases_dir / f"{revision}.env"


def _load_release(settings: Settings, revision: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ReleaseError("revision must be a full 40-character lowercase Git SHA")
    record = _read_json(_release_record_path(settings, revision))
    if record.get("revision") != revision:
        raise ReleaseError("release record revision does not match its filename")
    return record


def _validate_layout(settings: Settings) -> None:
    for path, label in (
        (settings.compose_file, "compose file"),
        (settings.app_env_file, "application env file"),
        (settings.data_dir, "data directory"),
    ):
        if not path.exists():
            raise ReleaseError(f"{label} does not exist: {path}")
    if not settings.data_dir.is_dir():
        raise ReleaseError(f"data path is not a directory: {settings.data_dir}")


def _compose_command(
    settings: Settings, env_file: Path, project_name: str | None = None
) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        project_name or settings.project_name,
        "--file",
        str(settings.compose_file),
        "--env-file",
        str(env_file),
    ]


def _inspect_image(image_ref: str, revision: str, digest: str) -> None:
    result = _run(
        ["docker", "image", "inspect", image_ref], capture_output=True
    )
    try:
        image = json.loads(result.stdout)[0]
    except (IndexError, KeyError, json.JSONDecodeError) as error:
        raise ReleaseError("docker returned invalid image metadata") from error

    platform = f"{image.get('Os')}/{image.get('Architecture')}"
    if platform != "linux/amd64":
        raise ReleaseError(f"unexpected image platform: {platform}")
    labels = image.get("Config", {}).get("Labels", {}) or {}
    if labels.get("org.opencontainers.image.revision") != revision:
        raise ReleaseError("image revision label does not match requested Git SHA")
    expected_repo_digest = (
        "ghcr.io/yy2511/deep-research-agent@" + digest
    )
    if expected_repo_digest not in (image.get("RepoDigests") or []):
        raise ReleaseError("local image RepoDigest does not match requested digest")


def _url_status(url: str) -> int:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status


def _wait_for_http_200(url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_result = "no response"
    while True:
        try:
            status = _url_status(url)
            if status == 200:
                return
            last_result = f"HTTP {status}"
        except (OSError, urllib.error.URLError) as error:
            last_result = str(error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReleaseError(
                f"service did not return HTTP 200 within {timeout:g}s: {last_result}"
            )
        time.sleep(min(0.5, remaining))


def _assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise ReleaseError(f"canary port {port} is already in use") from error


def stage(settings: Settings, tag: str, digest: str) -> str:
    _validate_layout(settings)
    revision, image_ref = validate_coordinates(tag, digest)
    settings.releases_dir.mkdir(parents=True, exist_ok=True)

    _run(["docker", "pull", image_ref])
    _inspect_image(image_ref, revision, digest)

    env_file = _release_env_path(settings, revision)
    _atomic_write(env_file, release_env_text(settings, image_ref))
    _run(_compose_command(settings, env_file) + ["config", "--quiet"])

    record = {
        "digest": digest,
        "image_ref": image_ref,
        "revision": revision,
        "staged_at": datetime.now(UTC).isoformat(),
        "tag": tag,
        "verified_at": None,
    }
    _write_json(_release_record_path(settings, revision), record)
    print(f"staged {revision} ({digest})")
    return revision


def verify(settings: Settings, revision: str) -> None:
    _validate_layout(settings)
    record = _load_release(settings, revision)
    env_file = _release_env_path(settings, revision)
    _assert_port_available(settings.canary_port)

    compose = _compose_command(settings, env_file, settings.canary_project)
    command_env = os.environ.copy()
    command_env["DRA_WEB_PORT"] = str(settings.canary_port)
    attempted = False
    try:
        attempted = True
        _run(
            compose
            + [
                "up",
                "--detach",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "60",
            ],
            env=command_env,
        )
        if _url_status(f"http://127.0.0.1:{settings.canary_port}/healthz") != 200:
            raise ReleaseError("canary health endpoint did not return 200")
        if _url_status(f"http://127.0.0.1:{settings.canary_port}/") != 200:
            raise ReleaseError("canary home page did not return 200")
    finally:
        if attempted:
            _run(compose + ["down", "--timeout", "10"], env=command_env)

    record["verified_at"] = datetime.now(UTC).isoformat()
    _write_json(_release_record_path(settings, revision), record)
    print(f"verified {revision} on 127.0.0.1:{settings.canary_port}")


def _systemd_active() -> bool:
    return (
        _run(
            ["systemctl", "is-active", "--quiet", "dra-web"], check=False
        ).returncode
        == 0
    )


def _compose_running(settings: Settings) -> bool:
    result = _run(
        [
            "docker",
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={settings.project_name}",
        ],
        capture_output=True,
    )
    return bool(result.stdout.strip())


def _current_state(settings: Settings) -> dict[str, Any]:
    systemd = _systemd_active()
    compose = _compose_running(settings)
    if systemd and compose:
        raise ReleaseError("systemd and Compose are both running; refusing to guess")
    if systemd:
        return SYSTEMD_STATE.copy()
    if compose:
        state = _read_json(settings.active_state_file)
        if state.get("kind") != "container":
            raise ReleaseError("Compose is running without valid active state")
        return state
    raise ReleaseError("neither systemd nor the production Compose project is running")


def _release_state(revision: str) -> dict[str, str]:
    return {"kind": "container", "revision": revision}


def _start_release(settings: Settings, revision: str) -> None:
    record = _load_release(settings, revision)
    if not record.get("verified_at"):
        raise ReleaseError(f"release {revision} has not passed canary verification")
    env_file = _release_env_path(settings, revision)
    compose = _compose_command(settings, env_file)
    _run(
        compose
        + [
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "60",
        ]
    )
    _wait_for_http_200("http://127.0.0.1:8765/healthz", timeout=10)


def _stop_release(settings: Settings, revision: str) -> None:
    env_file = _release_env_path(settings, revision)
    _run(_compose_command(settings, env_file) + ["down", "--timeout", "30"])


def _start_systemd() -> None:
    _run(["sudo", "-n", "systemctl", "enable", "--now", "dra-web"])
    _wait_for_http_200("http://127.0.0.1:8765/", timeout=30)


def _stop_systemd() -> None:
    _run(["sudo", "-n", "systemctl", "disable", "--now", "dra-web"])


def _restore(settings: Settings, state: dict[str, Any]) -> None:
    if state.get("kind") == "systemd":
        _start_systemd()
        return
    if state.get("kind") == "container" and isinstance(
        state.get("revision"), str
    ):
        _start_release(settings, state["revision"])
        return
    raise ReleaseError("cannot restore invalid prior state")


def activate(settings: Settings, revision: str) -> None:
    target_record = _load_release(settings, revision)
    if not target_record.get("verified_at"):
        raise ReleaseError(f"release {revision} has not passed canary verification")

    current = _current_state(settings)
    target = _release_state(revision)
    if current == target:
        print(f"release {revision} is already active")
        return

    _write_json(
        settings.pending_state_file,
        {"from": current, "started_at": datetime.now(UTC).isoformat(), "to": target},
    )
    try:
        if current["kind"] == "systemd":
            _stop_systemd()
        _start_release(settings, revision)
    except BaseException:
        try:
            if _compose_running(settings):
                _stop_release(settings, revision)
            _restore(settings, current)
        finally:
            settings.pending_state_file.unlink(missing_ok=True)
        raise

    _write_json(settings.previous_state_file, current)
    _write_json(settings.active_state_file, target)
    settings.pending_state_file.unlink(missing_ok=True)
    print(f"activated {revision}; previous state is {current['kind']}")


def rollback(settings: Settings) -> None:
    current = _current_state(settings)
    if current.get("kind") != "container":
        raise ReleaseError("rollback is only available while a container release is active")
    previous = _read_json(settings.previous_state_file)
    current_revision = str(current["revision"])

    _write_json(
        settings.pending_state_file,
        {
            "from": current,
            "started_at": datetime.now(UTC).isoformat(),
            "to": previous,
        },
    )
    try:
        if previous.get("kind") == "systemd":
            _stop_release(settings, current_revision)
            _start_systemd()
        elif previous.get("kind") == "container" and isinstance(
            previous.get("revision"), str
        ):
            _start_release(settings, previous["revision"])
        else:
            raise ReleaseError("previous state is invalid")
    except BaseException:
        try:
            if previous.get("kind") == "systemd" and _systemd_active():
                _stop_systemd()
            _start_release(settings, current_revision)
        finally:
            settings.pending_state_file.unlink(missing_ok=True)
        raise

    _write_json(settings.active_state_file, previous)
    _write_json(settings.previous_state_file, current)
    settings.pending_state_file.unlink(missing_ok=True)
    print(f"rolled back from {current_revision} to {previous['kind']}")


def status(settings: Settings) -> None:
    value: dict[str, Any] = {
        "active_record": None,
        "compose_running": _compose_running(settings),
        "pending": None,
        "previous_record": None,
        "systemd_active": _systemd_active(),
    }
    for key, path in (
        ("active_record", settings.active_state_file),
        ("previous_record", settings.previous_state_file),
        ("pending", settings.pending_state_file),
    ):
        if path.exists():
            value[key] = _read_json(path)
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage_parser = subparsers.add_parser("stage", help="pull and record an image")
    stage_parser.add_argument("tag")
    stage_parser.add_argument("digest")

    verify_parser = subparsers.add_parser(
        "verify", help="run the staged release on an isolated port"
    )
    verify_parser.add_argument("revision")

    activate_parser = subparsers.add_parser(
        "activate", help="switch systemd/Compose to a verified release"
    )
    activate_parser.add_argument("revision")

    subparsers.add_parser("rollback", help="restore the previous recorded state")
    subparsers.add_parser("status", help="show systemd, Compose, and release state")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = Settings.from_environment()
    try:
        if arguments.command == "stage":
            stage(settings, arguments.tag, arguments.digest)
        elif arguments.command == "verify":
            verify(settings, arguments.revision)
        elif arguments.command == "activate":
            activate(settings, arguments.revision)
        elif arguments.command == "rollback":
            rollback(settings)
        elif arguments.command == "status":
            status(settings)
        else:  # pragma: no cover - argparse enforces the command set.
            raise ReleaseError(f"unsupported command: {arguments.command}")
    except (ReleaseError, subprocess.CalledProcessError) as error:
        print(f"release error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
