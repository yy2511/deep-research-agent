"""Web active-run 容量护栏：在创建 run 和付费调用前拒绝重复启动。"""

import asyncio
from unittest.mock import ANY, MagicMock

import pytest
from fastapi import HTTPException

from dra import runtime_config, web
from dra.runstore import RunStore


def _active_handle(run_id: str = "active") -> web.RunHandle:
    handle = web.RunHandle(run_id)
    handle.task = MagicMock()
    handle.task.done.return_value = False
    return handle


def test_registry_rejects_over_capacity_before_persisting(tmp_path):
    store = RunStore(tmp_path)
    registry = web.RunRegistry(store, max_active_runs=1)
    registry.runs["active"] = _active_handle()

    with pytest.raises(web.ActiveRunLimitError, match="避免重复付费"):
        registry.start_research("第二个问题", None, config=object())

    assert store.list_runs() == []


def test_finished_or_cancelled_handle_releases_capacity(tmp_path):
    registry = web.RunRegistry(RunStore(tmp_path), max_active_runs=1)
    handle = _active_handle()
    registry.runs[handle.run_id] = handle
    assert registry.active_count() == 1

    handle.done = True
    assert registry.active_count() == 0


def test_registry_requires_positive_capacity(tmp_path):
    with pytest.raises(ValueError, match="max_active_runs"):
        web.RunRegistry(RunStore(tmp_path), max_active_runs=0)


def test_api_returns_429_when_capacity_is_full(monkeypatch):
    monkeypatch.setattr(runtime_config, "current", lambda: object())
    monkeypatch.setattr(
        runtime_config, "to_orchestrator_config", lambda _settings: object()
    )

    def reject(*_args, **_kwargs):
        raise web.ActiveRunLimitError("已有研究运行中")

    monkeypatch.setattr(web.REGISTRY, "start_research", reject)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(web.api_research(web.ResearchReq(query="新问题")))

    assert caught.value.status_code == 429
    assert caught.value.detail == "已有研究运行中"


def test_api_research_forwards_debug_full_io_flag(monkeypatch):
    monkeypatch.setattr(runtime_config, "current", lambda: object())
    monkeypatch.setattr(
        runtime_config, "to_orchestrator_config", lambda _settings: object()
    )
    start = MagicMock(return_value="run-debug")
    monkeypatch.setattr(web.REGISTRY, "start_research", start)

    result = asyncio.run(web.api_research(web.ResearchReq(
        query="新问题", trace_full_llm_io=True,
    )))

    assert result == {"run_id": "run-debug"}
    start.assert_called_once_with(
        "新问题", None, config=ANY, full_llm_io=True,
    )
