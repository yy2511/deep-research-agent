"""容器健康检查接口。"""
from __future__ import annotations

import asyncio
import tempfile

from fastapi import APIRouter, HTTPException

from dra.api.deps import DatabaseDep, RegistryDep
from dra.paths import DATA_DIR, PLANS_DIR, RUNS_DIR

router = APIRouter()


def _first_unwritable_data_dir() -> str | None:
    """真实写入并清理探针；只返回逻辑名称，不向响应暴露服务器路径。"""
    for name, directory in (
        ("data", DATA_DIR),
        ("runs", RUNS_DIR),
        ("plans", PLANS_DIR),
    ):
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=".dra-health-",
                dir=directory,
            ) as probe:
                probe.write(b"ok")
                probe.flush()
        except OSError:
            return name
    return None


@router.get("/healthz")
async def healthz(*, registry: RegistryDep, db: DatabaseDep) -> dict:
    """容器内部健康检查：不读取配置，不调用模型、搜索或研究编排。"""
    # 磁盘异常可能阻塞；探针放线程，容量计数仍回到事件循环读取，避免与启动 run
    # 的字典写入跨线程竞争。
    failed_dir = await asyncio.to_thread(_first_unwritable_data_dir)
    if failed_dir is not None:
        raise HTTPException(
            status_code=503,
            detail=f"{failed_dir} data directory is not writable",
        )
    # 数据库连不上时新研究无法开始（写不了归属），健康检查如实报告。
    try:
        if db is None:
            raise RuntimeError("database not initialised")
        await asyncio.to_thread(db.ping)
    except Exception:
        raise HTTPException(status_code=503, detail="database is unavailable") from None
    return {
        "status": "ok",
        "active_runs": registry.active_count(),
        "max_active_runs": registry.max_active_runs,
    }
