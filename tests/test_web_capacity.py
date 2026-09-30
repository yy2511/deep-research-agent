"""Web active-run 容量护栏：在创建 run 和付费调用前拒绝重复启动。"""

import asyncio
from unittest.mock import ANY, MagicMock

import pytest
from fastapi import HTTPException

from dra import runtime_config, web
from dra.runstore import RunStore
from dra.simple_research import SimpleConfig


def _active_handle(run_id: str = "active") -> web.RunHandle:
    handle = web.RunHandle(run_id)
    handle.busy = True
    return handle


def test_registry_rejects_over_capacity_before_persisting(tmp_path):
    store = RunStore(tmp_path)
    registry = web.RunRegistry(store, max_active_runs=1)
    registry.runs["active"] = _active_handle()

    with pytest.raises(web.ActiveRunLimitError, match="避免重复付费"):
        registry.start_simple_research("第二个问题", config=SimpleConfig())

    assert store.list_runs() == []


def test_finished_or_cancelled_handle_releases_capacity(tmp_path):
    registry = web.RunRegistry(RunStore(tmp_path), max_active_runs=1)
    handle = _active_handle()
    registry.runs[handle.run_id] = handle
    assert registry.active_count() == 1

    handle.busy = False
    assert registry.active_count() == 0


def test_registry_requires_positive_capacity(tmp_path):
    with pytest.raises(ValueError, match="max_active_runs"):
        web.RunRegistry(RunStore(tmp_path), max_active_runs=0)


def test_api_returns_429_when_capacity_is_full(monkeypatch):
    monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)

    def reject(*_args, **_kwargs):
        raise web.ActiveRunLimitError("已有研究运行中")

    monkeypatch.setattr(web.REGISTRY, "start_simple_research", reject)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(web.api_research(web.ResearchReq(query="新问题", mode="simple")))

    assert caught.value.status_code == 429
    assert caught.value.detail == "已有研究运行中"


def test_api_research_forwards_debug_full_io_flag(monkeypatch):
    monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
    start = MagicMock(return_value="run-debug")
    monkeypatch.setattr(web.REGISTRY, "start_simple_research", start)

    result = asyncio.run(web.api_research(web.ResearchReq(
        query="新问题", trace_full_llm_io=True,
        mode="simple",
    )))

    assert result == {"run_id": "run-debug", "mode": "simple"}
    start.assert_called_once_with(
        "新问题", config=ANY, requirements=None, brief=None, full_llm_io=True,
    )
