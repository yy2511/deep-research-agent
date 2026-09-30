"""DRA_DATA_DIR：默认兼容、输入防线与独立进程真实落盘。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dra import paths


def test_default_data_dir_keeps_project_root(monkeypatch):
    monkeypatch.delenv("DRA_DATA_DIR", raising=False)
    assert paths.resolve_data_dir() == paths.PROJECT_ROOT


def test_data_dir_accepts_absolute_path_and_expands_home():
    absolute = paths.resolve_data_dir("/tmp/dra-data")
    assert absolute == Path("/tmp/dra-data")
    assert paths.resolve_data_dir("~/dra-data").is_absolute()


@pytest.mark.parametrize("raw", ["relative/data", ".", "/"])
def test_data_dir_rejects_unstable_or_dangerous_paths(raw):
    with pytest.raises(RuntimeError, match="DRA_DATA_DIR"):
        paths.resolve_data_dir(raw)


def test_web_state_uses_configured_data_dir_in_fresh_process(tmp_path):
    data_dir = tmp_path / "persistent-data"
    env = {**os.environ, "DRA_DATA_DIR": str(data_dir)}
    script = """
import json
from dra import runtime_config, web
from dra.paths import DATA_DIR

runtime_config.load()
print(json.dumps({
    "data": str(DATA_DIR),
    "runs": str(web.STORE.root),
    "plans": str(web.PLAN_STORE.root),
    "config": str(runtime_config._CONFIG_PATH),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(result.stdout.strip())

    assert payload == {
        "data": str(data_dir),
        "runs": str(data_dir / "runs"),
        "plans": str(data_dir / "plans"),
        "config": str(data_dir / "runtime_config.json"),
    }
    assert (data_dir / "runs").is_dir()
    assert (data_dir / "plans").is_dir()
    assert (data_dir / "runtime_config.json").is_file()
