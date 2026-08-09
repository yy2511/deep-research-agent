"""研究工作台 web 服务（FastAPI + SSE）。

为什么从 stdlib 换 FastAPI（2026-07-07 spec，EXPERIMENT_PLAN 决策记录同日）
--------------------------------------------------------------------
V0 用 stdlib http.server 零依赖跑通（不过度设计）；需求长到工作台规模
（历史回放/统计/取消/双模式/SSE 补发重连）后，手搓路由与线程模型的复杂度
超过引入框架的成本，才换正规栈——升级时机本身是「不过度设计」的证据。
- research 直接跑在服务器事件循环（asyncio task），取消 = task.cancel() 协作式；
- 每 run 事件带 seq 落盘 runs/<id>/events.jsonl，SSE 补发+续推，刷新即重连；
- 前端见 web/（Vite+React），构建产物挂载 /；API 全部 /api/*。
用法：uv run python -m dra.web  → http://localhost:8765
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from dra import llm

_PORT = 8765
_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIST = _ROOT / "web" / "dist"


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是正整数，当前为 {raw!r}") from exc
    if value < 1:
        raise RuntimeError(f"{name} 必须是正整数，当前为 {raw!r}")
    return value


_MAX_ACTIVE_RUNS = _positive_env_int("DRA_MAX_ACTIVE_RUNS", 1)

app = FastAPI(title="DRA 研究工作台")


# ----- 计划边界：只接受唯一的 typed plan-node DTO -----
_PUBLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_RUN_ID_RE = re.compile(_PUBLIC_ID_PATTERN)


def _require_valid_run_id(run_id: str) -> None:
    """run_id 直接拼 runs/<id> 文件路径;非法形态在触达 RunStore/文件系统前拒绝。

    此前唯一防线是 ASGI 层对编码斜杠(%2f)的路由处理——偶然安全;显式校验后
    不再依赖框架实现细节(设计安全)。合法 run 形如 20260723-224942-8dbf0bf0。
    """
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(404, "run 不存在")


def _required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("必须是非空字符串")
    return value.strip()


def _optional_text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("必须是字符串或 null")
    return value.strip() or None


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PlanNodePayload(_StrictPayload):
    id: str = Field(pattern=_PUBLIC_ID_PATTERN)
    objective: str
    kind: Literal["research", "decision"]
    dependency_ids: list[str]
    acceptance_criteria: str

    _text = field_validator("id", "objective", "acceptance_criteria", mode="before")(
        _required_text
    )

    @field_validator("dependency_ids", mode="before")
    @classmethod
    def _dependencies(cls, value):
        if not isinstance(value, list):
            raise ValueError("dependency_ids 必须是列表")
        result = [_required_text(item) for item in value]
        if len(result) != len(set(result)):
            raise ValueError("dependency_ids 不得重复")
        return result


class _ResearchTaskPayload(_StrictPayload):
    """只接收用户可确认字段；调度器生成的 downstream binding 不属于公开 DTO。"""

    id: str = Field(pattern=_PUBLIC_ID_PATTERN)
    node_id: str = Field(pattern=_PUBLIC_ID_PATTERN)
    objective: str
    search_query: str

    _text = field_validator(
        "id", "node_id", "objective", "search_query", mode="before"
    )(_required_text)


class _PlanBudgetPayload(_StrictPayload):
    max_research_rounds: int = Field(ge=1)
    max_tasks_per_round: int = Field(ge=1)
    max_total_tasks: int = Field(ge=1)
    # 出站诊断字段；用户回传已确认的新协议计划时可缺省。
    estimated_min_tasks: int | None = Field(default=None, ge=0)
    recommended_tasks: int | None = Field(default=None, ge=0)
    budget_tight: bool | None = None


class _TypedPlanPayload(_StrictPayload):
    clarified_query: str
    plan_nodes: list[_PlanNodePayload] = Field(min_length=1)
    initial_tasks: list[_ResearchTaskPayload] = Field(min_length=1)
    budget: _PlanBudgetPayload | None = None

    _query = field_validator("clarified_query", mode="before")(_required_text)


def _budget_payload(config, research_plan=None) -> dict | None:
    if config is None:
        return None
    payload = {
        "max_research_rounds": config.max_research_rounds,
        "max_tasks_per_round": config.max_tasks_per_round,
        "max_total_tasks": config.max_total_tasks,
    }
    if research_plan is not None:
        from dra.nodes import estimate_task_budget

        estimates = estimate_task_budget(research_plan)
        payload.update(estimates)
        payload["budget_tight"] = (
            estimates["estimated_min_tasks"]
            <= config.max_total_tasks
            < estimates["recommended_tasks"]
        )
    return payload


def _budget_kwargs(config, payload_budget: _PlanBudgetPayload | None = None) -> dict:
    # 用户确认后的 typed plan 自带预算快照；它是 admission/runtime 契约的一部分，
    # 不能被点击“开始研究”时已经漂移的服务端配置覆盖。
    # 诊断字段（estimated_*/budget_tight）不进入 runtime config。
    if payload_budget is not None:
        return {
            "max_research_rounds": payload_budget.max_research_rounds,
            "max_tasks_per_round": payload_budget.max_tasks_per_round,
            "max_total_tasks": payload_budget.max_total_tasks,
        }
    if config is not None:
        return {
            "max_research_rounds": config.max_research_rounds,
            "max_tasks_per_round": config.max_tasks_per_round,
            "max_total_tasks": config.max_total_tasks,
        }
    return {
        "max_research_rounds": 3,
        "max_tasks_per_round": 5,
        "max_total_tasks": 18,
    }


def _config_with_plan_budget(config, payload):
    """把已确认计划的预算原子应用到本次 run 的冻结配置。"""
    if config is None or not isinstance(payload, dict) or "budget" not in payload:
        return config
    budget = _PlanBudgetPayload.model_validate(payload["budget"])
    return config.model_copy(update=budget.model_dump(include={
        "max_research_rounds", "max_tasks_per_round", "max_total_tasks",
    }))


def _research_plan_to_payload(research_plan, *, config=None) -> dict:
    """ResearchPlan → 唯一公开 plan-node 契约，完整保留 ID 与依赖边。"""
    payload = {
        "clarified_query": research_plan.clarified_query,
        "plan_nodes": [node.model_dump(mode="json") for node in research_plan.plan_nodes],
        "initial_tasks": [
            {
                "id": task.id,
                "node_id": task.node_id,
                "objective": task.objective,
                "search_query": task.search_query,
            }
            for task in research_plan.initial_tasks
        ],
    }
    if (budget := _budget_payload(config, research_plan=research_plan)) is not None:
        payload["budget"] = budget
    return payload


def _strip_plan_trace(payload):
    """确认计划回传时丢掉调试 trace，避免 typed DTO extra=forbid 拒收。"""
    if not isinstance(payload, dict) or "trace" not in payload:
        return payload
    return {k: v for k, v in payload.items() if k != "trace"}


def _payload_to_research_plan(payload, *, config=None):
    """Strict public plan JSON → validated typed ResearchPlan, otherwise ``None``."""
    from dra.models import PlanNode, ResearchPlan, ResearchTask
    from dra.nodes import PlanValidationError, validate_research_plan

    if not isinstance(payload, dict):
        return None
    payload = _strip_plan_trace(payload)
    try:
        parsed = _TypedPlanPayload.model_validate(payload)
        plan_nodes = [
            PlanNode.model_validate(item.model_dump())
            for item in parsed.plan_nodes
        ]
        questions = [
            ResearchTask.model_validate(item.model_dump())
            for item in parsed.initial_tasks
        ]
        task_ids = [question.id for question in questions]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task id 重复")
        research_plan = ResearchPlan(
            clarified_query=parsed.clarified_query,
            plan_nodes=plan_nodes,
            initial_tasks=questions,
        )
        budget = _budget_kwargs(config, parsed.budget)
        return validate_research_plan(research_plan, **budget)
    except (ValidationError, PlanValidationError, TypeError, ValueError):
        return None


def _planner_config_summary(cfg, *, full_llm_io: bool = False) -> dict:
    return {
        "planner": f"{cfg.planner_model}@{cfg.planner_provider}",
        "effort": cfg.planner_effort,
        "full_llm_io": full_llm_io,
    }


def _capture_planner_step(
    *,
    kind: str,
    query: str,
    cfg,
    step_name: str,
    fn,
    full_llm_io: bool = False,
) -> tuple[object, dict]:
    """跑一次规划/修订：timing.step 触发 llm_call 插桩 → 订阅落盘 → 返回 (结果, trace)。

    失败时仍返回已捕获的 events（挂在异常属性 ``plan_trace`` 上），供 422 响应带回 UI。
    """
    from dra import timing

    plan_id = PLAN_STORE.create(
        query=query,
        kind=kind,
        config_summary=_planner_config_summary(cfg, full_llm_io=full_llm_io),
    )
    captured: list[dict] = []

    def sink(evt: dict) -> None:
        e = dict(evt)
        e.setdefault("seq", len(captured))
        captured.append(e)
        try:
            PLAN_STORE.append_event(plan_id, e)
        except Exception:  # noqa: BLE001 — 落盘旁路
            pass

    events.set_run_id(plan_id)
    handle = events.subscribe(sink, run_id=plan_id)
    # 规划是同步短调用：只临时关掉 timing 实时打印，避免污染 web stdout；
    # 不调用 timing.reset()——那会清掉可能并行研究 run 的计时记录。
    prev_verbose = timing._verbose
    timing._verbose = False
    trace_token = llm.set_trace_full_io(full_llm_io)
    try:
        with timing.step(step_name):
            result = fn()
        payload = result if isinstance(result, dict) else None
        PLAN_STORE.finalize(plan_id, "ok", plan=payload)
        return result, {"plan_id": plan_id, "events": list(captured)}
    except Exception as exc:
        PLAN_STORE.finalize(plan_id, "failed", error=str(exc))
        setattr(exc, "plan_trace", {"plan_id": plan_id, "events": list(captured)})
        raise
    finally:
        llm.reset_trace_full_io(trace_token)
        timing._verbose = prev_verbose
        events.unsubscribe(handle)
        events.set_run_id(None)
        timing.clear_ctx()


def _plan_payload(q_text: str, *, full_llm_io: bool = False) -> tuple[dict, dict]:
    """POST /api/plan 主体：真调 planner 拆解，返回 (计划 JSON, trace)。"""
    from dra import runtime_config
    from dra.nodes import build_research_plan

    cfg = runtime_config.to_orchestrator_config(runtime_config.current())

    def _run():
        research_plan = build_research_plan(
            q_text, max_initial_tasks=cfg.max_initial_tasks,
            model=cfg.planner_model, provider=cfg.planner_provider,
            effort=cfg.planner_effort,
            max_research_rounds=cfg.max_research_rounds,
            max_tasks_per_round=cfg.max_tasks_per_round,
            max_total_tasks=cfg.max_total_tasks,
        )
        return _research_plan_to_payload(research_plan, config=cfg)

    return _capture_planner_step(
        kind="plan",
        query=q_text,
        cfg=cfg,
        step_name="build_research_plan",
        fn=_run,
        full_llm_io=full_llm_io,
    )


def _revise_payload(
    plan_payload: dict,
    feedback: str,
    *,
    full_llm_io: bool = False,
) -> tuple[dict | None, dict | None]:
    """POST /api/plan/revise 主体：还原计划 → revise_research_plan → (新计划 JSON, trace)。
    plan 参数非法返回 (None, None)；契约失败抛 PlanValidationError（带 plan_trace）。"""
    from dra import runtime_config
    from dra.nodes import revise_research_plan

    cfg = runtime_config.to_orchestrator_config(runtime_config.current())
    try:
        cfg = _config_with_plan_budget(cfg, plan_payload)
        research_plan = _payload_to_research_plan(plan_payload, config=cfg)
    except (ValidationError, TypeError, ValueError):
        return None, None
    if research_plan is None:
        return None, None

    def _run():
        revised = revise_research_plan(
            research_plan, feedback, max_initial_tasks=cfg.max_initial_tasks,
            max_research_rounds=cfg.max_research_rounds,
            max_tasks_per_round=cfg.max_tasks_per_round,
            max_total_tasks=cfg.max_total_tasks,
            model=cfg.planner_model, provider=cfg.planner_provider,
            effort=cfg.planner_effort,
        )
        return _research_plan_to_payload(revised, config=cfg)

    return _capture_planner_step(
        kind="revise",
        query=research_plan.clarified_query,
        cfg=cfg,
        step_name="revise_research_plan",
        fn=_run,
        full_llm_io=full_llm_io,
    )


class PlanReq(BaseModel):
    query: str
    trace_full_llm_io: bool = False


class ReviseReq(BaseModel):
    plan: dict
    feedback: str
    trace_full_llm_io: bool = False


@app.get("/api/config")
def api_config() -> dict:
    """当前 3 节点模型配置与 tool-loop 调用预算。"""
    from dra import runtime_config

    settings = runtime_config.current()
    cfg = runtime_config.to_orchestrator_config(settings)
    return {
        **settings.model_dump(),
        "max_tool_calls": cfg.subagent.max_tool_calls,
    }


@app.put("/api/config")
def api_config_put(body: dict) -> dict:
    """body 收原始 dict 而非 runtime_config.RuntimeModelSettings 类型注解——runtime_config
    只在函数体内懒加载（同 _plan_payload 对 dra.nodes 的既有写法），module-level 没有这个
    名字可让 FastAPI 在路由注册期解析类型注解，故手动构造+校验，两处失败都转 400。"""
    from pydantic import ValidationError

    from dra import runtime_config

    try:
        settings = runtime_config.RuntimeModelSettings(**body)
        runtime_config.to_orchestrator_config(settings)
    except ValidationError as e:
        raise HTTPException(400, str(e))
    runtime_config.save(settings)
    return api_config()


def _plan_validation_http(exc: Exception) -> HTTPException:
    """PlanValidationError → 422；detail 带 message + trace（调试 UI 与落盘对齐）。"""
    trace = getattr(exc, "plan_trace", None) or {"plan_id": None, "events": []}
    return HTTPException(
        422,
        {
            "message": (
                f"规划器生成的计划不满足执行契约：{exc}。"
                "请重试；若持续出现，可把问题拆得更明确。"
            ),
            "trace": trace,
        },
    )


@app.post("/api/plan")
def api_plan(req: PlanReq) -> dict:
    from dra.nodes import PlanValidationError

    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    try:
        payload, trace = _plan_payload(
            req.query, full_llm_io=req.trace_full_llm_io,
        )
    except PlanValidationError as exc:
        raise _plan_validation_http(exc) from exc
    return {**payload, "trace": trace}


@app.post("/api/plan/revise")
def api_plan_revise(req: ReviseReq) -> dict:
    from dra.nodes import PlanValidationError

    if not req.feedback.strip():
        raise HTTPException(400, "feedback 不能为空")
    try:
        payload, trace = _revise_payload(
            req.plan,
            req.feedback,
            full_llm_io=req.trace_full_llm_io,
        )
    except PlanValidationError as exc:
        raise _plan_validation_http(exc) from exc
    if payload is None:
        raise HTTPException(400, "plan 非法（缺 clarified_query 或无有效 initial_tasks）")
    return {**payload, "trace": trace}


@app.get("/api/providers")
def api_providers() -> list[dict]:
    return [p.model_dump() for p in llm.list_provider_status()]


@app.post("/api/providers/refresh")
def api_providers_refresh() -> list[dict]:
    return [p.model_dump() for p in llm.list_provider_status(force_refresh=True)]


class TestConfigReq(BaseModel):
    model: str
    provider: str
    reasoning: bool = False
    effort: str | None = None


@app.post("/api/config/test")
def api_config_test(req: TestConfigReq) -> dict:
    try:
        llm.chat(
            [{"role": "user", "content": "回复 OK"}],
            model=req.model, provider=req.provider,
            reasoning=req.reasoning, effort=req.effort,
            max_tokens=5, max_retries=0,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


import asyncio
import threading
import time as _time
from contextlib import asynccontextmanager

from dra import events
from dra.events import EventType
from dra.paths import DATA_DIR, PLANS_DIR, RUNS_DIR
from dra.runstore import (
    IncompatibleEventSchemaError,
    PlanAttemptStore,
    RunStore,
    aggregate_stats,
    replay_compatibility,
)

STORE = RunStore(RUNS_DIR)
PLAN_STORE = PlanAttemptStore(PLANS_DIR)


class RunHandle:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.buffer: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.done = False
        self.lock = threading.Lock()


class ActiveRunLimitError(RuntimeError):
    """全站 active run 已达上限；必须在创建 run 和付费调用前拒绝。"""


class RunRegistry:
    """内存 run 注册表 + 事件桥：总线 sink（子代理线程会调）→ seq 编号 → buffer/落盘/订阅队列。

    线程模型：publish 可能从任意线程进（events.emit 在 to_thread 里发），
    所以 buffer 用锁、跨线程唤醒订阅者用 loop.call_soon_threadsafe——
    这是「后台线程世界」与「asyncio SSE 世界」之间唯一的桥。
    """

    def __init__(self, store: RunStore, *, max_active_runs: int = _MAX_ACTIVE_RUNS):
        if max_active_runs < 1:
            raise ValueError("max_active_runs 必须 >= 1")
        self.store = store
        self.max_active_runs = max_active_runs
        self.runs: dict[str, RunHandle] = {}
        self.loop: asyncio.AbstractEventLoop | None = None   # lifespan 里捕获

    def active_count(self) -> int:
        """只计仍在执行的 task；完成或取消善终后立即释放名额。"""
        return sum(
            1 for handle in self.runs.values()
            if not handle.done
            and handle.task is not None
            and not handle.task.done()
        )

    def publish(self, run_id: str, evt: dict) -> None:
        h = self.runs.get(run_id)
        if h is None:
            return
        evt = dict(evt)
        evt.setdefault("ts", _time.time())
        with h.lock:
            evt["seq"] = len(h.buffer)
            h.buffer.append(evt)
            subs = list(h.subscribers)
            # 落盘必须在锁内、紧跟 seq 赋值——子代理跑在 asyncio.to_thread 派生的真实
            # OS 线程里并发 emit，锁外写文件曾让落盘顺序跟 seq 顺序脱钩（相邻 seq 对调），
            # reduce() 的 seq 去重会把后到的"seq 更小"事件当重复丢弃，回放历史 run 静默
            # 少事件（2026-07-08 真实录制 240 条事件的 run 时踩过）。
            try:
                self.store.append_event(run_id, evt)
            except Exception:  # noqa: BLE001 — 落盘是旁路，绝不弄崩事件流
                pass
        if self.loop is not None:
            for q in subs:
                self.loop.call_soon_threadsafe(q.put_nowait, evt)

    def start_research(
        self,
        query: str,
        research_plan,
        *,
        config,
        full_llm_io: bool = False,
    ) -> str:
        """Start from one caller-frozen config snapshot."""
        active = self.active_count()
        if active >= self.max_active_runs:
            raise ActiveRunLimitError(
                f"当前已有 {active} 个研究运行中；为避免重复付费，同时最多允许 "
                f"{self.max_active_runs} 个。请等待完成或先取消现有研究。"
            )
        cfg = config
        run_id = self.store.create_run(
            query,
            _research_plan_to_payload(research_plan, config=cfg) if research_plan else None,
            {"planner": f"{cfg.planner_model}@{cfg.planner_provider}",
             "subagent": f"{cfg.subagent.model}@{cfg.subagent.provider}",
             "writer": f"{cfg.writer_model}@{cfg.writer_provider}",
             "budget": _budget_payload(cfg),
             "full_llm_io": full_llm_io})
        h = RunHandle(run_id)
        self.runs[run_id] = h
        handle = events.subscribe(lambda e: self.publish(run_id, e), run_id=run_id)
        h.task = asyncio.get_running_loop().create_task(
            self._run(
                run_id,
                query,
                research_plan,
                cfg,
                handle,
                full_llm_io=full_llm_io,
            ))
        return run_id

    async def _run(
        self,
        run_id,
        query,
        research_plan,
        config,
        sub_handle,
        *,
        full_llm_io: bool = False,
    ) -> None:
        from dra.nodes import render_report_markdown
        from dra.orchestrator import run_orchestrator

        status = "done"
        trace_token = llm.set_trace_full_io(full_llm_io)
        try:
            state = await run_orchestrator(
                query,
                config,
                verbose=False,
                run_id=run_id,
                research_plan=research_plan,
            )
            status = state.status
            md = (render_report_markdown(state.report, state.evidence)
                  if state.report else "(无报告)")
            saved = self.store.save_report(run_id, md)
            self.publish(run_id, {"type": EventType.REPORT_MD.value,
                                  "markdown": md, "saved_path": str(saved)})
        except asyncio.CancelledError:
            # 协作式取消善终：状态入档、事件补发；吞掉不 re-raise（顶层 task，无人再等它）
            status = "cancelled"
            self.publish(run_id, {"type": EventType.CANCELLED.value})
        except Exception as e:  # noqa: BLE001 — 失败也要善终入档
            status = "failed"
            self.publish(run_id, {"type": EventType.ERROR.value, "message": str(e)})
        finally:
            llm.reset_trace_full_io(trace_token)
            events.unsubscribe(sub_handle)
            h = self.runs[run_id]
            try:
                self.store.finalize(run_id, status, aggregate_stats(h.buffer))
            except Exception:  # noqa: BLE001
                pass
            self.publish(run_id, {"type": EventType.END.value})
            h.done = True


REGISTRY = RunRegistry(STORE)


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


@app.get("/healthz")
async def healthz() -> dict:
    """容器内部健康检查：不读取配置，不调用模型、搜索或研究编排。"""
    # 磁盘异常可能阻塞；探针放线程，容量计数仍回到事件循环读取，避免与启动 run
    # 的字典写入跨线程竞争。
    failed_dir = await asyncio.to_thread(_first_unwritable_data_dir)
    if failed_dir is not None:
        raise HTTPException(
            status_code=503,
            detail=f"{failed_dir} data directory is not writable",
        )
    return {
        "status": "ok",
        "active_runs": REGISTRY.active_count(),
        "max_active_runs": REGISTRY.max_active_runs,
    }


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """@app.on_event("startup") 在装的 FastAPI 版本上是硬 DeprecationWarning，
    换现代 lifespan 写法，语义不变（服务启动即跑一次，无需 shutdown 动作）。
    app 早在模块顶部就建好了，这里在 REGISTRY/STORE 定义完之后事后指定
    router.lifespan_context——已验证过这个赋值方式对 uvicorn 与 TestClient
    都生效（同 with TestClient(app) as c 触发 startup 的既有用法一致）。"""
    REGISTRY.loop = asyncio.get_running_loop()
    n = STORE.mark_orphans()
    if n:
        print(f"⚠️ 启动扫描：{n} 个 running 孤儿 run 标记 failed（server restart）")
    yield


app.router.lifespan_context = _lifespan


class ResearchReq(BaseModel):
    query: str
    plan: dict | None = None
    trace_full_llm_io: bool = False


def _normalise_plan_identity(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


@app.post("/api/research")
async def api_research(req: ResearchReq) -> dict:
    from dra import runtime_config

    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    cfg = runtime_config.to_orchestrator_config(runtime_config.current())
    research_plan = None
    if req.plan is not None:
        plan_body = _strip_plan_trace(req.plan)
        research_plan = _payload_to_research_plan(plan_body, config=cfg)
        if research_plan is None:
            raise HTTPException(400, "plan 非法（确认过的计划不得静默退回内部自拆）")
        try:
            cfg = _config_with_plan_budget(cfg, plan_body)
        except (ValidationError, TypeError, ValueError):
            raise HTTPException(400, "plan budget 非法") from None
        if _normalise_plan_identity(req.query) != _normalise_plan_identity(
            research_plan.clarified_query
        ):
            raise HTTPException(400, "query 与已确认 plan 的 clarified_query 不一致")
    try:
        run_id = REGISTRY.start_research(
            req.query,
            research_plan,
            config=cfg,
            full_llm_io=req.trace_full_llm_io,
        )
    except ActiveRunLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    return {"run_id": run_id}


@app.get("/api/runs")
def api_runs() -> list[dict]:
    return STORE.list_runs()


@app.get("/api/runs/{run_id}")
def api_run_detail(run_id: str) -> dict:
    _require_valid_run_id(run_id)
    meta = STORE.get_meta(run_id)
    if meta is None:
        raise HTTPException(404, "run 不存在")
    compatible, error = replay_compatibility(meta)
    return {
        "meta": {
            **meta,
            "replay_compatible": compatible,
            "replay_error": error,
        },
        "report_md": STORE.read_report(run_id),
    }


@app.get("/api/runs/{run_id}/events", response_class=PlainTextResponse)
def api_run_events(run_id: str) -> str:
    _require_valid_run_id(run_id)
    try:
        text = STORE.read_events_text(run_id)
    except IncompatibleEventSchemaError as exc:
        raise HTTPException(409, str(exc)) from exc
    if text is None:
        raise HTTPException(404, "run 无事件记录")
    return text


_FIXTURE = _ROOT / "fixtures" / "demo_run" / "events.jsonl"


def _sse_frame(evt: dict) -> str:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"


@app.get("/api/research/{run_id}/stream")
async def api_stream(run_id: str) -> StreamingResponse:
    """SSE：原子「订阅+快照」后先补发 backlog 再续推 live，按 seq 去重——
    刷新页面 = 重连 = 不丢现场。end 事件后关流（EventSource 侧 close 防自动重连）。"""
    _require_valid_run_id(run_id)
    h = REGISTRY.runs.get(run_id)
    if h is None:
        raise HTTPException(404, "run 不在内存（服务重启过？历史回放走 /api/runs/{id}/events）")

    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        with h.lock:
            backlog = list(h.buffer)
            h.subscribers.add(q)
        try:
            last = -1
            for e in backlog:
                yield _sse_frame(e)
                last = e["seq"]
            if backlog and backlog[-1]["type"] == EventType.END.value:
                return
            while True:
                e = await q.get()
                if e["seq"] <= last:
                    continue
                yield _sse_frame(e)
                if e["type"] == EventType.END.value:
                    return
        finally:
            with h.lock:
                h.subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/demo/events", response_class=PlainTextResponse)
def api_demo_events() -> str:
    if not _FIXTURE.exists():
        raise HTTPException(404, "demo fixture 缺失：uv run python scripts/gen_dev_fixture.py")
    return _FIXTURE.read_text(encoding="utf-8")


@app.post("/api/research/{run_id}/cancel")
def api_cancel(run_id: str) -> dict:
    """协作式取消（诚实边界见 spec §5）。

    cancel 会立刻取消 Web task、让前端收尾；已进入后台线程的同步 HTTP 不能被
    Python 强杀，但每个请求已有明确 timeout（writer 最多 180s，普通调用 90s），
    后台线程会在该上限内自行结束。善终路径负责 emit cancelled + finalize meta。
    """
    _require_valid_run_id(run_id)
    h = REGISTRY.runs.get(run_id)
    if h is None:
        raise HTTPException(404, "run 不在内存")
    if h.done or h.task is None or h.task.done():
        meta = STORE.get_meta(run_id) or {}
        return {"status": meta.get("status", "unknown")}
    h.task.cancel()
    return {"status": "cancelling"}


# mount("/") 铁律：StaticFiles 挂 "/" 会吞掉其后注册的所有路由（含 404 兜底），
# 必须放在全部应用路由定义之后——T5/T6 追加 /api/research、/api/runs 等新路由时，
# 都要插在这行之前，不能图省事加到文件末尾。
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


if __name__ == "__main__":
    main()
