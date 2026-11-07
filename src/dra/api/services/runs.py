"""研究运行管理：内存运行注册表、名额控制、后台研究线程与收尾，以及发起、追问、发消息、取消、订阅进度这些业务操作。

不依赖 FastAPI：报错用 ``dra.api.services.errors`` 里的类型，由接口层翻译成 HTTP 状态码。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import threading
import time as _time
import unicodedata

from pydantic import ValidationError

from dra import events, llm, mcp_client
from dra.api.services.errors import CapacityFull, Conflict, InvalidRequest, NotFound, Unavailable
from dra.api.services import run_records
from dra.api.services.ids import RUN_ID_RE as _RUN_ID_RE
from dra.api.services.planning import _strip_plan_trace
from dra.api.services.skills import skill_bundle as resolve_skill_bundle
from dra.events import EventType
from dra.library import LibraryError, get_library
from dra.research_brief import ResearchBrief, render_brief_context
from dra.research_input import ResearchInput, replay_input
from dra.db import Database
from dra.runstore import IncompatibleEventSchemaError, RunStore, new_run_id
from dra.site_filter import normalize_blocked_sites


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


class RunHandle:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.buffer: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.done = False
        self.lock = threading.Lock()
        # simple 路径专用：取消用共享 Event 传给研究入口；后台线程真正退出前
        # ``busy`` 保持 True，名额因此不会提前释放（旧行为是 task.cancel 后立即释放）。
        self.cancel_event: threading.Event | None = None
        self.busy = False
        # 完成回调可能早于本任务的 finally 排期执行，所以终态必须随本任务结束立刻可见。
        self.simple_status: str | None = None
        self.user_input: ResearchInput | None = None


class ActiveRunLimitError(CapacityFull, RuntimeError):
    """全站 active run 已达上限；必须在创建 run 和付费调用前拒绝。"""


def _admission(method):
    """同一服务进程的建轮、重试与名额检查共享临界区。"""
    from functools import wraps
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.admission_lock:
            return method(self, *args, **kwargs)
    return locked


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
        self.admission_lock = threading.RLock()
        # simple 路径的收尾任务：后台线程退出后释放名额/发 end。持有引用避免被 GC。
        self.loop: asyncio.AbstractEventLoop | None = None   # lifespan 里捕获

    def active_count(self) -> int:
        """以真实研究线程退出为准，取消请求不能提前释放名额。"""
        return sum(handle.busy for handle in self.runs.values())

    def publish(self, run_id: str, evt: dict, *, durable=False) -> None:
        h = self.runs.get(run_id)
        if h is None:
            return
        evt = dict(evt)
        evt.setdefault("ts", _time.time())
        with h.lock:
            if evt.get("event_id") and any(e.get("event_id") == evt["event_id"] for e in h.buffer):
                return
            evt["seq"] = len(h.buffer)
            subs = list(h.subscribers)
            # 落盘必须在锁内、紧跟 seq 赋值——子代理跑在 asyncio.to_thread 派生的真实
            # OS 线程里并发 emit，锁外写文件曾让落盘顺序跟 seq 顺序脱钩（相邻 seq 对调），
            # reduce() 的 seq 去重会把后到的"seq 更小"事件当重复丢弃，回放历史 run 静默
            # 少事件。
            try:
                self.store.append_event(run_id, evt)
            except Exception:  # noqa: BLE001 — 普通观测允许失败，控制事件必须传播
                if durable:
                    raise
            h.buffer.append(evt)
            # 编号、保存、排入 SSE 必须保持同一顺序。若解锁后才推送，另一条
            # 线程会先推 seq+1，客户端去重随后把这条用户消息当旧事件丢掉。
            if self.loop is not None:
                for q in subs:
                    self.loop.call_soon_threadsafe(q.put_nowait, evt)


    @_admission
    def start_simple_research(
        self,
        query: str,
        *,
        config,
        requirements=None,
        brief=None,
        full_llm_io: bool = False,
        followup_context: dict | None = None,
        run_id: str | None = None,
        user_id: int | None = None,
    ) -> str:
        """单 Lead 最小闭环：确认后的问题 → 取证 → 作答 → 核查与预算内有界修订。

        名额在创建 run 之前就检查并在整个后台执行期间持有；取消只置位共享 Event，
        不强杀线程，也不提前释放名额。

        ``requirements`` 来自开跑前确认的 brief（未带时由内核退化为"整问即一条要求"）；
        ``brief`` 有两处用处：把"这次开跑前确认了什么"写进 run 的 meta（事后可回溯），
        以及把目标 / 初步路线 / 未确认前提渲染成任务背景交给研究内核——不然用户确认过的东西
        研究时根本看不到，等于白问。
        """
        active = self.active_count()
        if active >= self.max_active_runs:
            raise ActiveRunLimitError(
                f"当前已有 {active} 个研究运行中；为避免重复付费，同时最多允许 "
                f"{self.max_active_runs} 个。请等待完成或先取消现有研究。"
            )
        budget = {
            "max_llm_calls": config.max_llm_calls,
            "max_tool_calls": config.max_tool_calls,
            "wall_timeout_s": config.wall_timeout_s,
            "reserve_wrapup_calls": config.reserve_wrapup_calls if config.max_llm_calls is not None else 0,
        }
        identity = {"resolved_config": config.model_dump(mode="json"),
                    "initial_requirements": [r.model_dump(mode="json") for r in requirements or []]}
        if user_id is not None:
            # 便于排查；归属以数据库为准（#30）。
            identity["user_id"] = user_id
        if followup_context is not None:
            identity.update({k: followup_context[k] for k in ("conversation_id","parent_run_id","turn_index","request_id")})
            identity.update(status="preparing", followup_request={"parent_run_id":followup_context["parent_run_id"],"text":query})
        run_id = self.store.create_run(
            query,
            None,
            {
                "mode": "simple",
                "subagent": f"{config.adapter.model}@{config.adapter.provider}",
                "source_mode": config.source_mode,
                "library_selection": config.library_selection,
                "library_rerank": config.library_rerank,
                "mcp_server_ids": [s.id for s in config.mcp_servers],
                "blocked_sites": config.blocked_sites,
                "budget": budget,
                "full_llm_io": full_llm_io,
                "research_brief": brief.model_dump(mode="json") if brief is not None else None,
            }, identity=identity, run_id=run_id,
        )
        if followup_context is not None:
            from dra.durable import atomic_json
            try:
                atomic_json(self.store._dir(run_id) / "followup_context.json", followup_context)
            except Exception:
                self.store.finalize(run_id, "failed", None)
                raise
            meta = self.store.get_meta(run_id)
            meta["status"] = "running"
            self.store._write_meta(run_id, meta)
        h = RunHandle(run_id)
        h.cancel_event = threading.Event()
        h.busy = True
        self.runs[run_id] = h
        h.user_input = ResearchInput(run_id, lambda event: self.publish(run_id, event, durable=True),
                                     stopped=lambda: h.done or bool(h.cancel_event and h.cancel_event.is_set()))
        sub = events.subscribe(lambda e: self.publish(run_id, e), run_id=run_id)
        job = self._run_simple(
                run_id,
                query,
                config,
                sub,
                requirements=requirements,
                brief_context=render_brief_context(brief) if brief is not None else None,
                # 开跑前确认的那份路线就是运行期列表的初始值——两处共用一个形状，不另起播种逻辑。
                initial_route=(
                    [item.model_dump(mode="json") for item in brief.route]
                    if brief is not None
                    else None
                ),
                full_llm_io=full_llm_io,
            )
        try:
            h.task = asyncio.get_running_loop().create_task(job)
        except Exception:
            job.close()
            events.unsubscribe(sub)
            h.busy = False
            h.done = True
            self.store.finalize(run_id, "failed", None)
            raise
        return run_id

    @_admission
    def start_followup(self, parent_run_id, request_id, text, *, config, run_id=None, user_id=None,
                       counted=None):
        """``counted(run_id)``：哪些轮次算数（以数据库登记为准）。启动失败后残留的目录
        不参与“重复提交”与“是否最新一轮”的判断（#39 审查）；不传时按文件全部算。"""
        from dra.research_history import HistoryError, ancestors, prepare_context
        if not isinstance(request_id,str) or not _RUN_ID_RE.fullmatch(request_id):
            raise HistoryError("request_id 须为 1–64 位字母、数字、下划线或连字符")
        if not isinstance(text,str) or not text.strip() or len(text)>6000:
            raise HistoryError("追问须为 1–6000 字符的非空文字")
        chain = ancestors(self.store, parent_run_id)
        cid = chain[0]["run_id"]
        turns = self.store.conversation_turns(cid, counted=counted)
        body = {"parent_run_id":parent_run_id,"text":text}
        for item in turns:
            if item.get("request_id") == request_id:
                if item.get("followup_request") != body:
                    raise HistoryError("同一 request_id 对应不同追问，请勿修改后重试")
                return item["run_id"]
        if turns[-1]["run_id"] != parent_run_id:
            raise HistoryError("研究已有新一轮，请回到最新一轮继续")
        h = self.runs.get(parent_run_id)
        if turns[-1]["status"] in {"running","preparing"} or (h and (h.busy or (h.task and not h.task.done()))):
            raise HistoryError("上一轮仍在运行或保存结果，请结束后继续追问")
        scope = chain[0].get("config_summary", {})
        selection = scope.get("library_selection") or {}
        source_mode = scope.get("source_mode", "web")
        if source_mode != "web":
            get_library().validate_selection(selection)
        config = config.model_copy(deep=True, update={"source_mode":source_mode,
            "library_selection":selection,"library_rerank":bool(scope.get("library_rerank")),
            "mcp_servers":mcp_client.pin_servers(scope.get("mcp_server_ids") or []),
            "blocked_sites":list(scope.get("blocked_sites") or [])})
        previous_skills = self.store._dir(parent_run_id) / "skills.json"
        if previous_skills.exists():
            try:
                inherited_skills = json.loads(previous_skills.read_text())
            except (OSError, ValueError) as exc:
                raise HistoryError("上一轮技能快照不可读，未启动追问") from exc
            config = config.model_copy(update={"skill_bundle": inherited_skills})
        context = prepare_context(self.store,parent_run_id,text,request_id)
        from dra.simple_research import Requirement
        requirements = [Requirement(id="followup",text=text)]
        requirements.extend(Requirement(id=f"inherited-{i}",text=r["text"],kind=r["kind"])
                            for i,r in enumerate(context["constraints"],1))
        return self.start_simple_research(text,config=config,requirements=requirements,
            full_llm_io=bool(chain[-1].get("config_summary",{}).get("full_llm_io")),followup_context=context,
            run_id=run_id,user_id=user_id)

    def _cancel_requested(self, run_id: str) -> bool:
        h = self.runs.get(run_id)
        return bool(h and h.cancel_event is not None and h.cancel_event.is_set())

    async def _run_simple(
        self,
        run_id: str,
        query: str,
        config,
        sub_handle,
        *,
        requirements=None,
        brief_context: str | None = None,
        initial_route=None,
        full_llm_io: bool = False,
    ) -> None:
        """simple 路径的 Web 侧善终。

        三件事必须一起成立：

        1. **取消真的传进研究内核**：``cancel_event`` 通过
           ``llm.execution_scope`` 绑定到执行上下文，内核里所有
           ``llm.cancellation_requested()`` 检查点才看得到；只建 Event 不传 = 吞掉取消。
           （执行上下文是 contextvars；``run_in_executor`` 不传播它，因此工作线程里
           要重新绑定 run_id 并显式包一层 ``execution_scope``。）
        2. **终态映射诚实**：``failed``/``partial``/``done`` 按真实交付结果写 meta，
           不能一律 done，否则列表与四状态互相矛盾。
        3. **名额持有到后台真正退出**：``asyncio.to_thread`` 不会被 ``task.cancel()``
           停下，所以这里把它当"待收尾的线程"处理——先置取消 Event，再等它自己结束；
           即使本任务被取消（服务关停）也不释放名额，直到线程真实退出或如实记录中断。
        """
        from dra import research as research_entry

        handle = self.runs[run_id]
        cancel_event = handle.cancel_event or threading.Event()
        handle.cancel_event = cancel_event
        trace_token = llm.set_trace_full_io(full_llm_io)
        events.set_run_id(run_id)
        worker: "asyncio.Future | None" = None
        status = "failed"

        def _blocking():
            # 线程上下文两件事都必须显式设置：
            #  - run_id：``events.set_run_id`` 写在协程里不会自动进入工作线程。
            #    注意两者的差别：``asyncio.to_thread`` 会复制 contextvars，
            #    而这里的 ``loop.run_in_executor`` **不传播** contextvars，
            #    所以必须在工作线程内部重新绑定，否则事件全部缺 run_id（多运行会串号）；
            #  - cancel_event：新开 execution_scope 会把 cancel_event 重置为 None，
            #    必须显式传入，内核的取消检查点才看得到。
            events.set_run_id(run_id)
            with llm.execution_scope(cancel_event=cancel_event):
                return research_entry.run_minimal_research(
                    query,
                    requirements,
                    config=config,
                    brief_context=brief_context,
                    initial_route=initial_route,
                    run_id=run_id,
                    checkpoint_dir=self.store._dir(run_id),
                    user_input=handle.user_input,
                )

        handle.simple_status = "failed"
        try:
            if handle.user_input is not None:
                handle.user_input.announce()
            self.publish(
                run_id,
                {
                    "type": EventType.RESEARCH_STATUS.value,
                    "mode": "simple",
                    "phase": "starting",
                    "allowed": _simple_allowed_budget(config),
                },
                durable=True,
            )

            # contextvars 不会自动进入 executor 线程（``run_in_executor`` 不传播；
            # 只有 ``asyncio.to_thread`` 会复制）。这里显式复制当前上下文——
            # 必须在 ``set_trace_full_io`` 之后取，otherwise 调试档的完整 I/O 会丢。
            worker_context = contextvars.copy_context()
            worker = asyncio.get_running_loop().run_in_executor(
                None, lambda: worker_context.run(_blocking)
            )

            result = await asyncio.shield(worker)
            payload = result.model_dump(mode="json")
            self.store.save_simple_result(run_id, payload)
            status = _simple_status(payload)
            self.publish(
                run_id,
                {
                    "type": EventType.RESEARCH_STATUS.value,
                    "mode": "simple",
                    "phase": "finished",
                    "tool_calls": self.store.executed_tool_calls(run_id),
                    "model_calls": len(payload.get("calls") or []),
                    "sources": len([s for s in payload.get("sources") or []
                                    if s.get("snapshot_kind") == "full"]),
                    "cards": len(payload.get("cards") or []),
                    "allowed": _simple_allowed_budget(config),
                    "stop_reason": payload.get("stop_reason"),
                    "delivery": payload.get("delivery_status"),
                    "coverage": payload.get("research_coverage"),
                    "coverage_reasons": payload.get("coverage_reasons"),
                    "requirements": payload.get("requirements"),
                    "effective_query": payload.get("effective_query"),
                    "scope_history": payload.get("scope_history", []),
                    "plan_sync_status": payload.get("plan_sync_status"),
                    "gaps": payload.get("gaps"),
                    "warnings": payload.get("warnings"),
                    "artifacts": payload.get("artifacts", []),
                    "artifact_errors": payload.get("artifact_errors", []),
                    "execution_summary": payload.get("execution_summary", {}),
                },
                durable=True,
            )
            if status == "cancelled":
                self.publish(run_id, {"type": EventType.CANCELLED.value})
            md = payload.get("markdown") or ""
            if md:
                saved = self.store._dir(run_id) / "report.md"
                self.publish(
                    run_id,
                    {"type": EventType.REPORT_MD.value, "markdown": md, "saved_path": str(saved)},
                )
        except asyncio.CancelledError:
            # 服务关停/上层取消：先让内核停下（置位共享 Event），**不在这里等线程**：
            # 本任务已被取消，再 await 会立刻抛。真正的收尾由 finally 登记到 worker
            # 完成回调上——只有线程真正结束才释放名额、发 end。
            cancel_event.set()
            status = "cancelled"
            self.publish(run_id, {"type": EventType.CANCELLED.value})
            raise
        except Exception as e:  # noqa: BLE001 — 失败也要善终入档
            status = "failed"
            self.publish(run_id, {"type": EventType.ERROR.value, "message": str(e)})
        finally:
            llm.reset_trace_full_io(trace_token)
            events.unsubscribe(sub_handle)
            handle.simple_status = status  # 终态先可见，回调读到的一定是算好的值
            if worker is None or worker.done():
                # 正常路径（或线程已结束）：按已经算好的 status 收尾
                self._finalize_simple(run_id, status)
            else:
                # 本任务已被取消/异常，而线程还在跑：**不释放名额**，
                # 把收尾挂到 worker 的完成回调上（只认线程真正结束）。
                self._arm_settle_on_worker(run_id, worker)

    def _finalize_simple(self, run_id: str, status: str) -> None:
        """运行善终：写 meta 终态、发 end、释放名额。幂等（回调与 finally 都可能到）。"""
        h = self.runs.get(run_id)
        if h is None or h.done:
            return
        if h.user_input is not None:
            try:
                h.user_input.close(status)
            except llm.LLMPersistenceError:
                status = "failed"
        try:
            self.store.finalize(run_id, status, _simple_stats(h.buffer))
        except Exception:  # noqa: BLE001
            pass
        self.publish(run_id, {"type": EventType.END.value})
        h.busy = False
        h.done = True

    def _arm_settle_on_worker(self, run_id: str, worker) -> None:
        """把收尾登记在**真实 worker future** 的完成回调上：只有它 done 才释放名额。

        为什么不用 monitor/waiter：关停时 waiter 也会被取消，而它的 done 只代表
        "等待被取消"，不代表线程结束。这里的前提是上面的 ``asyncio.shield(worker)``——
        shield 让上层取消**没有**落到 worker future 上，因此它仍会在真实线程跑完时
        变成 done，回调才是可信的收尾点。（若不 shield，future 会被取消、立即 done，
        那时"done"就不等于"线程结束"，也就不能用来判定。）
        """
        def _on_worker_done(_future) -> None:
            h = self.runs.get(run_id)
            status = (h.simple_status if h and h.simple_status else "failed")
            self._finalize_simple(run_id, status)

        worker.add_done_callback(_on_worker_done)


def _simple_status(payload: dict) -> str:
    """simple 四状态 → 运行列表状态。

    不能一律 ``done``：交付失败却是"完成"会让列表/刷新与四状态互相矛盾。
    """
    if payload.get("stop_reason") == "cancelled":
        return "cancelled"
    delivery = payload.get("delivery_status")
    if delivery == "failed":
        return "failed"
    if delivery == "limited":
        return "partial"
    return "done"


def _simple_allowed_budget(config) -> dict:
    """展示给用户的真实上限（来自 simple 配置，不用旧 Worker/Writer 旋钮充数）。"""
    return {
        "max_llm_calls": config.max_llm_calls,
        "max_tool_calls": config.max_tool_calls,
        "wall_timeout_s": config.wall_timeout_s,
        "reserve_wrapup_calls": config.reserve_wrapup_calls if config.max_llm_calls is not None else 0,
    }


def _simple_stats(buffer: list[dict]) -> dict:
    """给运行列表用的最小统计：只数 simple 自己的事件，不冒充 Worker 指标。"""
    from dra.llm_observation import aggregate_usage

    tools = model_calls = sources = cards = 0
    for evt in buffer:
        if evt.get("type") != EventType.RESEARCH_STATUS.value:
            continue
        tools = evt.get("tool_calls", tools)
        model_calls = evt.get("model_calls", model_calls)
        sources = evt.get("sources", sources)
        cards = evt.get("cards", cards)
    return {
        "mode": "simple",
        "tool_calls": tools,
        "model_calls": model_calls,
        "sources": sources,
        "cards": cards,
        "workers": 0,
        "cache": aggregate_usage(buffer),
    }


def _run_input_snapshot(run_id, store: RunStore, registry: "RunRegistry"):
    h = registry.runs.get(run_id)
    if h is not None and h.user_input is not None:
        return h.user_input.snapshot()
    try:
        text = store.read_events_text(run_id) or ""
    except IncompatibleEventSchemaError:
        # 旧协议仍可读 meta/报告；不可回放原因已由 meta 明确返回。
        return replay_input([])
    except (OSError, UnicodeError):
        return {**replay_input([]), "error": "用户消息记录暂时无法读取"}
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # 只投影已经确认的前缀，不改写日志、不假装恢复执行。
            return {**replay_input(rows), "error": "部分事件记录损坏，仅展示此前保存的用户消息"}
        if isinstance(event, dict):
            rows.append(event)
    return replay_input(rows)


# ----- 业务操作：接口层与以后的 Worker 都从这里调用 -----

def _normalise_plan_identity(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def start_research(
    registry: RunRegistry,
    query: str,
    *,
    brief: dict | None,
    skill_selection: str | None,
    source_mode: str,
    document_ids: list[str],
    library_rerank: bool,
    mcp_server_ids: list[str],
    blocked_sites: list[str],
    full_llm_io: bool,
    run_id: str | None = None,
    user_id: int | None = None,
) -> str:
    """校验已确认的计划与来源范围，按当前模型设置开始一次研究，返回 run_id。"""
    from dra import runtime_config

    typed_brief = None
    if brief is not None:
        try:
            typed_brief = ResearchBrief.model_validate(_strip_plan_trace(brief))
        except (ValidationError, TypeError, ValueError) as exc:
            raise InvalidRequest(f"brief 非法：{exc}") from exc
        if _normalise_plan_identity(query) != _normalise_plan_identity(typed_brief.original_query):
            raise InvalidRequest("query 与已确认 brief 的 original_query 不一致")
    # 主、子 Agent 共用研究模型配置。
    simple_settings = runtime_config.current()
    skill_bundle = resolve_skill_bundle(skill_selection)
    try:
        selection = get_library().pin(document_ids) if source_mode != "web" else {}
        external = mcp_client.pin_servers(mcp_server_ids)
        if external and source_mode == "library":
            raise mcp_client.MCPError("仅资料模式不允许外部 MCP，请切换网页或混合研究")
        normalized_sites = normalize_blocked_sites(blocked_sites)
    except (LibraryError, mcp_client.MCPError, ValueError) as exc:
        raise InvalidRequest(str(exc)) from exc
    return registry.start_simple_research(
        query,
        config=runtime_config.to_simple_config(simple_settings, source_mode=source_mode,
                            library_selection=selection, library_rerank=library_rerank,
                            mcp_servers=external, skill_bundle=skill_bundle,
                            blocked_sites=normalized_sites),
        requirements=typed_brief.to_run_requirements() if typed_brief is not None else None,
        brief=typed_brief,
        full_llm_io=full_llm_io,
        run_id=run_id,
        user_id=user_id,
    )


# 被取消的请求留下的“等占位写完再撤销”后台任务；保留引用，避免任务在完成前被回收。
_pending_cleanups: set[asyncio.Task] = set()


async def _reserve(db: Database, run_id: str, reserve, *args):
    """在线程里占位。等待期间请求被取消（客户端断开、服务关闭）时，线程仍会把占位写完；
    这里安排一个后台任务：等线程真正结束，写入成功就撤销，主流程照常按取消退出（#36 审查）。

    不能在取消时立刻撤销：撤销可能早于线程提交，删了个空，随后占位才写进去。
    """
    reservation = asyncio.ensure_future(asyncio.to_thread(reserve, *args))
    try:
        return await asyncio.shield(reservation)
    except asyncio.CancelledError:
        cleanup = asyncio.ensure_future(_release_after(reservation, db, run_id))
        _pending_cleanups.add(cleanup)
        cleanup.add_done_callback(_pending_cleanups.discard)
        raise


async def drain_pending_cleanups() -> None:
    """服务正常关闭时调用：等所有“等占位写完再撤销”的后台任务做完（#36 复审）。

    不等的话，关闭会结束事件循环、取消这些任务，而写库线程照样提交，留下没有运行目录的记录。
    每个任务只等一个短事务，几乎不拖慢关闭。强制杀进程不在此列。
    """
    while _pending_cleanups:
        await asyncio.gather(*list(_pending_cleanups), return_exceptions=True)


async def _release_after(reservation: asyncio.Future, db: Database, run_id: str) -> None:
    try:
        await reservation
    except Exception:  # noqa: BLE001 - 占位本身没写成，没有要撤销的
        return
    await asyncio.to_thread(run_records.release_quietly, db, run_id)


async def start_owned_research(db: Database, registry: RunRegistry, user_id: int, query: str,
                               **options) -> str:
    """先在数据库占位（线程里的短事务），再在事件循环上启动研究；启动失败就撤销占位。

    设计 §3.2、ADR 0006 规则 3。写库失败时（Unavailable → 503）研究不会启动，也不建运行目录。
    """
    run_id = new_run_id()
    await _reserve(db, run_id, run_records.reserve_new_run, db, user_id, run_id, query)
    try:
        return start_research(registry, query, run_id=run_id, user_id=user_id, **options)
    except BaseException:
        await asyncio.to_thread(run_records.release_quietly, db, run_id)
        raise


async def start_owned_followup(db: Database, store: RunStore, registry: RunRegistry, user_id: int,
                               parent_run_id: str, request_id: str, text: str) -> dict:
    """追问版的“先占位，再启动，失败就撤销”。

    同一 request_id 重复提交时，现有逻辑返回之前那一轮的编号、不再启动；这时也撤销这次的占位。
    """
    if store.get_meta(parent_run_id) is None:
        raise NotFound("run 不存在")
    run_id = new_run_id()
    _conversation, counted = await _reserve(
        db, run_id, run_records.reserve_followup, db, user_id, parent_run_id, run_id, text)
    try:
        result = start_followup(store, registry, parent_run_id, request_id, text,
                                reserved_run_id=run_id, user_id=user_id,
                                counted=counted.__contains__)
    except BaseException:
        await asyncio.to_thread(run_records.release_quietly, db, run_id)
        raise
    if result["run_id"] != run_id:
        await asyncio.to_thread(run_records.release_quietly, db, run_id)
    return result


def start_followup(store: RunStore, registry: RunRegistry, run_id: str, request_id: str, text: str,
                   *, reserved_run_id: str | None = None, user_id: int | None = None,
                   counted=None) -> dict:
    """在已有运行之后追问，开始新一轮研究；返回新一轮的身份信息。"""
    from dra import runtime_config
    from dra.research_history import HistoryError
    if store.get_meta(run_id) is None:
        raise NotFound("run 不存在")
    try:
        config=runtime_config.to_simple_config(runtime_config.current())
        new_id=registry.start_followup(run_id,request_id,text,config=config,
                                       run_id=reserved_run_id,user_id=user_id,counted=counted)
    except (HistoryError, LibraryError, mcp_client.MCPError) as exc:
        raise Conflict(str(exc)) from exc
    meta=store.get_meta(new_id)
    return {k:meta[k] for k in ("run_id","conversation_id","turn_index","status","parent_run_id")}


def post_message(store: RunStore, registry: RunRegistry, run_id: str, message_id: str, text: str) -> dict:
    """研究进行中给它补充一条消息；同一消息 ID 重发时确认原消息，不重复接收。"""
    meta = store.get_meta(run_id)
    if meta is None:
        raise NotFound("run 不存在")
    h = registry.runs.get(run_id)
    if h is not None and h.user_input is not None:
        try:
            return h.user_input.accept(message_id, text)
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        except llm.LLMPersistenceError as exc:
            raise Unavailable("消息保存失败，未确认接收；请查看运行状态") from exc
    # 结束/重启后的网络重试可确认原消息，不能以重试为由重启研究。
    snapshot = _run_input_snapshot(run_id, store, registry)
    for message in snapshot["messages"]:
        if message["message_id"] == message_id:
            if message["text"] != text:
                raise Conflict("重复消息 ID 对应的正文不同")
            return {"accepted": True, "message": message, "state": "closed"}
    if snapshot.get("error"):
        raise Unavailable("消息记录不完整，暂时无法确认是否已接收")
    raise Conflict("研究正在收尾或已结束，未接收新消息")


def cancel(store: RunStore, registry: RunRegistry, run_id: str) -> str:
    """请求协作式取消，真实研究线程结束后才释放名额。返回取消后的状态。"""
    h = registry.runs.get(run_id)
    if h is None:
        raise NotFound("run 不在内存")
    if h.done or not h.busy:
        meta = store.get_meta(run_id) or {}
        return meta.get("status", "unknown")
    if h.cancel_event is not None:
        h.cancel_event.set()
    if h.user_input is not None:
        h.user_input.close("cancelled")
    return "cancelling"


def follow_events(registry: RunRegistry, run_id: str):
    """订阅运行中的事件：先补发已有事件，再推送新事件，按 seq 去重，到 end 事件结束。

    运行不在内存时立即报错（在开始推送之前）；返回的是异步迭代器，用完或中途放弃都要 ``aclose()``。
    """
    h = registry.runs.get(run_id)
    if h is None:
        raise NotFound("run 不在内存（服务重启过？历史回放走 /api/runs/{id}/events）")
    return _follow(h)


async def _follow(h: RunHandle):
    q: asyncio.Queue = asyncio.Queue()
    with h.lock:
        backlog = list(h.buffer)
        h.subscribers.add(q)
    try:
        last = -1
        for e in backlog:
            if e["type"] == EventType.END.value and (not h.done or e is not backlog[-1]):
                continue
            yield e
            last = e["seq"]
        if h.done and backlog and backlog[-1]["type"] == EventType.END.value:
            return
        while True:
            e = await q.get()
            if e["seq"] <= last:
                continue
            yield e
            if e["type"] == EventType.END.value:
                return
    finally:
        with h.lock:
            h.subscribers.discard(q)

