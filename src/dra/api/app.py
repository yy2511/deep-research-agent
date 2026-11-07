"""研究工作台 web 服务（FastAPI + SSE）的组装入口。

为什么从 stdlib 换 FastAPI
--------------------------------------------------------------------
V0 用 stdlib http.server 零依赖跑通（不过度设计）；需求长到工作台规模
（历史回放/统计/取消/双模式/SSE 补发重连）后，手搓路由与线程模型的复杂度
超过引入框架的成本，才换正规栈——升级时机本身是「不过度设计」的证据。
- research 直接跑在服务器事件循环（asyncio task），取消 = task.cancel() 协作式；
- 每 run 事件带 seq 落盘 runs/<id>/events.jsonl，SSE 补发+续推，刷新即重连；
- 前端见 web/（Vite+React），构建产物挂载 /；API 全部 /api/*。

本文件只做组装：创建应用、创建存储与运行管理器、注册各资源的路由、生命周期、静态文件。
接口在 routers/，运行管理与规划在 services/。启动入口仍是 dra.web（兼容旧命令与部署）。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from dra.api.common import _ROOT
from dra.api.deps import current_user, same_origin
from dra.api.routers import (
    auth,
    conversations,
    health,
    library,
    planning,
    providers,
    research,
    runs,
    settings,
    skills,
)
from dra.api.services.runs import RunRegistry, drain_pending_cleanups
from dra.db import Database, DatabaseConfigError, DatabaseNotReady
from dra.paths import PLANS_DIR, RUNS_DIR
from dra.runstore import PlanAttemptStore, RunStore

_PORT = 8765
_WEB_DIST = _ROOT / "web" / "dist"

app = FastAPI(title="DRA 研究工作台")

# 启动时创建一次；接口通过 dra.api.deps 的 get_store / get_plan_store / get_registry 取用。
app.state.store = RunStore(RUNS_DIR)
app.state.plan_store = PlanAttemptStore(PLANS_DIR)
app.state.registry = RunRegistry(app.state.store)
# 数据库在启动时按 DRA_DATABASE_URL 创建（ADR 0006）；测试预先放入自己的库，启动时沿用。
app.state.db = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """检查数据库，扫描中断运行和孤儿容器，管理会话容器池，退出时清理执行资源。"""
    owns_db = _app.state.db is None
    try:
        if owns_db:
            _app.state.db = Database.from_env()
        # 连不上或迁移不是最新就拒绝启动，不静默降级，也不自动迁移（设计 §7）。
        await asyncio.to_thread(_app.state.db.check_ready)
    except (DatabaseConfigError, DatabaseNotReady) as exc:
        print(f"❌ 数据库未就绪，服务不启动：{exc}")
        if owns_db and _app.state.db is not None:
            _app.state.db.dispose()
            _app.state.db = None
        raise
    _app.state.registry.loop = asyncio.get_running_loop()
    n = _app.state.store.mark_orphans()
    if n:
        print(f"⚠️ 启动扫描：{n} 个 running 孤儿 run 标记 failed（server restart）")
    from dra.execution import ExecutionConfig, reap_orphans
    from dra.execution_pool import get_pool, shutdown_pools
    execution_config = ExecutionConfig.from_env()
    if execution_config.enabled:
        try:
            await asyncio.to_thread(reap_orphans, _app.state.store.root, execution_config)
            get_pool(_app.state.store.root,execution_config)
        except Exception:
            print("执行工作区清理失败：已暂停新执行，普通研究仍可用")
    try:
        yield
    finally:
        for handle in _app.state.registry.runs.values():
            if handle.cancel_event is not None: handle.cancel_event.set()
        try:
            # 设置页可在运行期间开关执行，池可能在启动后才创建；按实际存在的池清理，没有池时不访问 Docker。
            await asyncio.to_thread(shutdown_pools, _app.state.store.root)
            if execution_config.enabled:
                await asyncio.to_thread(reap_orphans, _app.state.store.root, execution_config)
        except Exception:
            print("执行工作区清理未确认，将在下次启动重试")
        # 释放数据库之前，等被取消请求的占位撤销做完（services/runs.drain_pending_cleanups）。
        await drain_pending_cleanups()
        if owns_db:
            _app.state.db.dispose()
            _app.state.db = None


app.router.lifespan_context = _lifespan

# 除健康检查和注册、登录外，全部 /api/* 要求登录（设计 §2）；修改类请求另做 Origin 检查。
# tests/test_auth.py 遍历路由表，检查没有遗漏。
app.include_router(health.router)
app.include_router(auth.router, dependencies=[Depends(same_origin)])
# 带研究编号 / 对话编号的分组另外挂归属检查（#31，在各自 router 文件里）；
# tests/test_run_ownership.py 遍历这类接口，检查没有遗漏。
# 改全站设置的 admin_router 分组另外要求管理员（#32）；tests/test_admin_permissions.py 检查没有遗漏。
for _router in (skills.router, skills.run_router, skills.admin_router, settings.router, settings.admin_router,
                planning.router, providers.router, providers.admin_router,
                research.router, research.run_router, runs.router,
                runs.run_router, conversations.router, conversations.conversation_router,
                library.router):
    app.include_router(_router, dependencies=[Depends(same_origin), Depends(current_user)])


# mount("/") 铁律：StaticFiles 挂 "/" 会吞掉其后注册的所有路由（含 404 兜底），
# 必须放在全部路由注册之后——新增路由在 routers/ 里写，并在上面的 include_router 注册。
if _WEB_DIST.is_dir():   # 构建产物在才挂载；开发期走 vite dev 代理
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="webui")
else:
    @app.get("/", response_class=PlainTextResponse)
    def _no_build() -> str:
        return "前端未构建：cd web && npm ci && npm run build（开发期用 npm run dev）"


def main(port: int = _PORT) -> None:
    import uvicorn

    print(f"🌐 研究工作台：http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
