"""分层约束（docs/开发规范.md §1）：业务层不依赖接口层与 FastAPI，研究引擎不依赖 dra.api / dra.web。

业务层以后要被 Worker 进程直接调用，那里没有 HTTP 请求；一旦业务层 import 了 FastAPI 或接口文件，
这条路就断了。只写在文档里的规则容易被一次顺手的 import 打破，所以用测试固定下来。
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "dra"
SERVICES = sorted(
    "dra.api.services." + path.stem
    for path in (SRC / "api" / "services").glob("*.py")
    if path.stem != "__init__"
)
# 接口层：业务层不能直接或间接加载这些模块。
INTERFACE_MODULES = ("dra.web", "dra.api.app", "dra.api.common", "dra.api.deps")


def test_services_load_without_fastapi_or_interface_layer(tmp_path):
    script = (
        "import importlib, json, sys\n"
        f"for name in {SERVICES!r}: importlib.import_module(name)\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'fastapi' or m.startswith('fastapi.')"
        f" or m.startswith('dra.api.routers') or m in {INTERFACE_MODULES!r})))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "DRA_DATA_DIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert SERVICES, "没有找到业务层模块"
    assert json.loads(result.stdout) == []


def _imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_research_engine_does_not_import_api_or_web():
    offenders = {}
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC)
        # web.py、admin.py 是入口（Web 服务、管理命令），可以调用接口层和业务层。
        if relative.parts[0] == "api" or relative in (Path("web.py"), Path("admin.py")):
            continue
        bad = sorted(m for m in _imported_modules(path)
                     if m in ("dra.web", "dra.api") or m.startswith("dra.api."))
        if bad:
            offenders[str(relative)] = bad
    assert offenders == {}


def test_routers_do_not_touch_database_sessions():
    """ADR 0006 规则 6：接口层不直接接触数据库会话，只调用业务层。"""
    offenders = {}
    for path in (SRC / "api" / "routers").glob("*.py"):
        bad = sorted(m for m in _imported_modules(path)
                     if m == "sqlalchemy" or m.startswith("sqlalchemy.")
                     or m in ("dra.db_tables", "dra.db") or m.startswith(("dra.db.", "dra.db_tables.")))
        if bad:
            offenders[path.name] = bad
    assert offenders == {}
