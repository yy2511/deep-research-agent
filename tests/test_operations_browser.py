"""本机真实页面 + SQLite API 验收（合成数据，不调用模型/外部网站）。

先 npm --prefix web run build，再显式运行：
  DRA_DATA_DIR="$(mktemp -d)" uv run pytest -q tests/test_operations_browser.py --run-browser
可选 DRA_BROWSER_EXECUTABLE 指定本机 Chromium；截图默认留在 pytest 临时目录，
DRA_OPERATIONS_SCREENSHOTS 可指定输出目录。不会读取生产库或修改真实账号。
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import uvicorn

pytestmark = [pytest.mark.browser, pytest.mark.real_auth]
ROOT = Path(__file__).resolve().parents[1]


def test_local_operations_dashboard(database, tmp_path, monkeypatch):
    if not os.environ.get("DRA_DATA_DIR"):
        pytest.fail("请设置 DRA_DATA_DIR 为新建临时目录，避免读取本机配置")
    assert database.engine.dialect.name == "sqlite", "浏览器验收只使用临时 SQLite"
    if not (ROOT / "web/dist/index.html").exists():
        pytest.fail("请先 npm --prefix web run build")
    node = shutil.which("node")
    if not node:
        pytest.fail("本机浏览器验收需要 Node 与 web/node_modules 中的 Playwright")
    from dra.api.app import app
    from dra.api.services import auth
    from dra.api.services.runs import RunRegistry
    from dra.db_tables import Conversation, ResearchTask, RunAttempt, User
    from dra.runstore import PlanAttemptStore, RunStore

    # 真实 Cookie 鉴权；邮箱/密码仅适用于本测试临时 SQLite。
    password = "synthetic-browser-test-only"
    admin = auth.create_user(database, "admin@browser-test.example", password, role="admin")
    member = auth.create_user(database, "member@browser-test.example", password, role="user")
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with database.session() as session:
        session.get(User, admin.id).display_name = "合成数据 · 本地验收"
        session.get(User, member.id).display_name = "合成普通用户"
        for i in range(10):
            session.add(User(email=f"synthetic-{i}@browser-test.example", role="guest", status="disabled" if i == 0 else "active", created_at=today - timedelta(days=i)))
        for day in range(7):
            conversation = Conversation(id=f"browser-test-{day}", user_id=admin.id, created_at=today - timedelta(days=day))
            session.add(conversation)
            session.flush()
            task = ResearchTask(user_id=admin.id, conversation_id=conversation.id, query="合成问题，不应出现在运营统计响应", created_at=today - timedelta(days=day))
            session.add(task)
            session.flush()
            session.add(RunAttempt(task_id=task.id, run_id=f"browser-run-{day}", created_at=task.created_at))
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(app.state, "store", store)
    monkeypatch.setattr(app.state, "plan_store", PlanAttemptStore(tmp_path / "plans"))
    monkeypatch.setattr(app.state, "registry", RunRegistry(store))
    # 页面只读本机接口；进一步禁止后端测试进程发起外网连接。
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("浏览器验收禁止外部网络")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    port = server_socket.getsockname()[1]
    # 测试库由 fixture 完成迁移；不运行生产启动时的孤儿扫描/执行环境探测。
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, "测试 HTTP 服务未启动"
        env = {**os.environ, "DRA_BROWSER_TEST_URL": f"http://127.0.0.1:{port}",
               "DRA_OPERATIONS_SCREENSHOTS": os.environ.get("DRA_OPERATIONS_SCREENSHOTS", str(tmp_path / "screenshots"))}
        result = subprocess.run([node, str(ROOT / "web/scripts/check-operations-browser.mjs")], cwd=ROOT / "web",
                                env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        server_socket.close()
        assert not thread.is_alive(), "测试 HTTP 服务未能退出"
