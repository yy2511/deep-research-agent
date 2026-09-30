"""Web 运行时数据路径：代码/静态资源与可变状态分离。"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_data_dir(raw: str | None = None) -> Path:
    """解析 ``DRA_DATA_DIR``；未设置时保持历史项目根路径。

    生产环境必须给绝对路径，避免 systemd/容器工作目录变化后把同一服务的数据
    写进不同位置。根目录 ``/`` 也拒绝，防止误建 ``/runs``、``/plans``。
    """
    if raw is None:
        raw = os.environ.get("DRA_DATA_DIR")
    if raw is None or not raw.strip():
        return PROJECT_ROOT
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"DRA_DATA_DIR 必须是绝对路径，当前为 {raw!r}")
    if path == Path(path.anchor):
        raise RuntimeError("DRA_DATA_DIR 不得直接指向文件系统根目录")
    return path


DATA_DIR = resolve_data_dir()
RUNS_DIR = DATA_DIR / "runs"
PLANS_DIR = DATA_DIR / "plans"
RUNTIME_CONFIG_PATH = DATA_DIR / "runtime_config.json"
