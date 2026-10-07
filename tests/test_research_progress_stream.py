"""阶段 1：真实过程消息链路的离线端到端验收。

链路：真实 ``llm.call_tools`` 流式装配 → Lead 循环按执行当时 emit ``lead_message`` /
``subagent_tool_call``(running|done) → ``RunRegistry`` 落盘 events.jsonl + SSE 补发 →
历史读取与 SSE 内容一致；普通说明与调试 I/O 开关无关。

只替换外部边界（模型流式客户端、检索结果）：问题确认、预算、工具执行、账本、
事件落盘、SSE、结果投影都走真实实现。不联网、不产生真实模型费用。

为什么不用 ``events.subscribe`` 断言：那只证明"事件总线里出现过"，证明不了 Web 侧
分配全局 seq、落盘、SSE 补发后消息仍可拼装。这里走 HTTP/SSE 真实链路。
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dra import llm, runtime_config, web  # noqa: E402
from dra.models import RetrievedDoc  # noqa: E402
from dra.runstore import PlanAttemptStore, RunStore  # noqa: E402

from test_budget_wrapup import RAW  # noqa: E402

QUERY = "sqlite3_rsync 同机两个文件也能同步吗？"


# ---------------------------------------------------------------------------
# 假流式边界：只产 OpenAI 流式 chunk
# ---------------------------------------------------------------------------


def _chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = NS(content=content, tool_calls=tool_calls, role=None,
               reasoning_content=None, reasoning_details=None)
    return NS(choices=[NS(delta=delta, index=0, finish_reason=finish_reason)], usage=usage)


def _tc(index, *, id=None, name=None, arguments=None, type="function"):
    return NS(index=index, id=id, type=type, function=NS(name=name, arguments=arguments))


def _usage(prompt=396, completion=98):
    return NS(prompt_tokens=prompt, completion_tokens=completion)


def _split(text: str, parts: int) -> list[str]:
    """把字符串切成 parts 段，模拟 arguments 分片。"""
    size = max(1, len(text) // parts)
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class StreamingScript:
    """按序吐"带普通说明的流式回合"（Lead 的 tool-calling 循环），用完进入纯文本收尾。

    每个回合 = (content 文本, [(工具名, 参数 dict), ...])。
    工具 arguments 一定跨 2 片以上到达，这样测的是真实分片装配而不是单块解析。

    一个脚本实例只服务**一次研究**；测试之间不复用实例（游标会互相吃掉回合）。
    """

    def __init__(self, turns, *, wrapup_text="", aux_text="", stream_usage=True):
        self.turns = list(turns)
        self.wrapup_text = wrapup_text
        self.aux_text = aux_text
        self.stream_usage = stream_usage  # False 模拟"中转不回传流式用量"
        self.index = 0
        self.requests: list[dict] = []

    def _tool_chunks(self, tool_calls):
        chunks = []
        for i, (name, args) in enumerate(tool_calls):
            raw = json.dumps(args, ensure_ascii=False)
            pieces = _split(raw, 3)
            chunks.append(_chunk(tool_calls=[_tc(i, id=f"call-{self.index}-{i}", name=name,
                                                 arguments=pieces[0])]))
            for piece in pieces[1:]:
                chunks.append(_chunk(tool_calls=[_tc(i, arguments=piece)]))
        return chunks

    def stream_for(self, kwargs) -> list:
        """一次带 tools 的请求 → chunk 列表（含普通说明与工具分片）。"""
        turn_index = self.index
        self.index += 1
        if turn_index < len(self.turns):
            content, tool_calls = self.turns[turn_index]
        else:
            content, tool_calls = self.wrapup_text, []
        chunks = [_chunk(content=piece) for piece in _split(content, 4)] if content else []
        chunks.extend(self._tool_chunks(tool_calls))
        finish = "tool_calls" if tool_calls else "stop"
        chunks.append(_chunk(finish_reason=finish,
                             usage=_usage() if self.stream_usage else None))
        return chunks

    def chat_for(self, kwargs):
        """非流式请求：收尾回合回纯文本。"""
        content = self.aux_text or "材料不足，如实说明缺口。"
        return NS(choices=[NS(message=NS(content=content, tool_calls=None))], usage=_usage())


class FakeOpenAI:
    """最小 OpenAI 兼容替身：同一客户端既服务流式工具调用，也服务非流式核查。

    脚本按**运行身份**分配（``events.get_run_id()``，工作线程里由 Web 层显式绑定）：
    同一个 run 的多次请求始终用同一个脚本，不同 run 各拿各的——这样"两个运行不串号"
    才是真的在测隔离，而不是替身在测试间共享游标。
    """

    def __init__(self, factory):
        self.factory = factory
        self.by_run: dict[str, list[StreamingScript]] = {}
        self.chat_requests: list[dict] = []
        self.stream_requests: list[dict] = []

    def _pool(self, run_id: str | None) -> list[StreamingScript]:
        key = run_id or "-"
        if key not in self.by_run:
            lead = self.factory()
            # 收尾/修稿回合是非流式的独立一次性脚本：与 Lead 循环共用游标会把
            # "Lead 说了几句话"和"还有没有回合"混成一个数。
            aux = type(lead)([], wrapup_text=lead.wrapup_text, aux_text=lead.aux_text,
                             stream_usage=lead.stream_usage)
            self.by_run[key] = [lead, aux]
        return self.by_run[key]

    def script_for(self, run_id: str | None, *, lead: bool = False) -> StreamingScript:
        pool = self._pool(run_id)
        if lead:
            return pool[0]
        if len(pool) > 2:
            return pool.pop()
        return pool[1]

    @property
    def script(self) -> StreamingScript:
        return next(iter(self.by_run.values()))[0]

    @property
    def chat(self):
        from dra import events

        outer = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                # 只有真实 Lead 循环走流式；收尾/修稿/核查都是非流式调用。
                script = outer.script_for(
                    events.get_run_id(), lead=bool(kwargs.get("stream")))
                if kwargs.get("stream"):
                    outer.stream_requests.append(kwargs)
                    return iter(script.stream_for(kwargs))
                outer.chat_requests.append(kwargs)
                return script.chat_for(kwargs)

        return NS(completions=_Completions())


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _doc():
    return RetrievedDoc(
        id="d1", source_url="https://sqlite.org/rsync.html", title="sqlite3_rsync",
        snippet="sqlite3_rsync 通过 SSH 同步", raw_content=RAW, published_at="2025-11-13",
    )


def _simple_config():
    from dra.simple_research import SimpleConfig
    from dra.subagent import SubAgentConfig

    return SimpleConfig(
        adapter=SubAgentConfig(model="test-model", provider="test", reasoning=False),
        max_llm_calls=8, max_tool_calls=8, wall_timeout_s=30.0, wrapup_reserve_s=0,
    )


def _adapter_config(provider="test"):
    from dra.subagent import SubAgentConfig
    return SubAgentConfig(model="test-model", provider=provider, reasoning=False)


@pytest.fixture
def isolated_web(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", web.RunRegistry(store, max_active_runs=2))
    monkeypatch.setattr(web, "PLAN_STORE", PlanAttemptStore(tmp_path / "plans"))
    monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
    monkeypatch.setattr(runtime_config, "to_subagent_config", lambda _: _adapter_config())
    return store


def _install_boundaries(monkeypatch, factory=None):  # noqa: ANN001
    """只替换外部边界：模型客户端 + 检索结果。

    factory 是"每个运行一个新脚本"的工厂（见 StreamingScript 的游标语义）；
    检索必须打在 ``simple_research`` 模块自己的引用上——它是
    ``from dra.subagent import _retrieve``，改 subagent 模块属性不影响已绑定的名字。
    """
    from dra import simple_research as sr

    client = FakeOpenAI(factory or _script)
    monkeypatch.setattr(llm, "_get_client", lambda provider="openai", _c=client: _c)
    monkeypatch.setattr(sr, "_retrieve", lambda query, config, **kw: [_doc()])
    return client


def _wait_done(client, run_id, timeout=20.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/runs/{run_id}").json()
        if (detail.get("meta") or {}).get("status") not in {"running", None}:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} 未在 {timeout}s 内结束")


def _events(client, run_id) -> list[dict]:
    return [json.loads(line) for line in client.get(f"/api/runs/{run_id}/events").text.splitlines()
            if line.strip()]


def _join_messages(events: list[dict]) -> dict[str, str]:
    """按后端契约把 lead_message 拼装成每段说明的正文（append / replace / complete）。"""
    messages: dict[str, str] = {}
    for event in events:
        if event["type"] != "lead_message":
            continue
        msg_id = event["msg_id"]
        if event["mode"] == "replace":
            messages[msg_id] = event["text"]
        elif event["mode"] == "append":
            messages[msg_id] = messages.get(msg_id, "") + event["text"]
        else:  # complete：只标结束，不追加
            assert event["text"] == "", "complete 不得重复追加全文"
            messages.setdefault(msg_id, "")
    return messages


# 真实链路的一次完整研究：说明 + 工具同轮，最后交答案。
# 用工厂而不是共享实例：StreamingScript 有游标，测试之间共享会互相吃掉回合。
_TURNS = [
    ("我先明确 sqlite3_rsync 的同步前提，再查官方文档。",
     [("search_sources", {"query": "sqlite3_rsync ssh local"})]),
    ("已拿到官方页面，我读一下原文里对本地路径的说明。",
     [("read_source", {"source_ref": "s1"})]),
    ("原文支持同机两文件直接同步，我把这条存为证据卡。",
     [("save_evidence", {"cards": [{"claim": "同机两文件可同步", "source_ref": "s1",
                                    "excerpt_no": 1}]})]),
    ("证据足够，我按原文给出答案。",
     [("finish_research", {"draft": "不要求一端为远程，两文件都在本机也能同步 [c1]。"})]),
]


def _script(**kwargs) -> StreamingScript:
    return StreamingScript([(text, list(calls)) for text, calls in _TURNS], **kwargs)


def _run_research(client, query=QUERY, *, mode="simple", full_llm_io=False):
    # 请求字段名以 ResearchReq 为准（trace_full_llm_io）；写错会静默走非调试档。
    body = {"query": query, "mode": mode, "trace_full_llm_io": full_llm_io}
    resp = client.post("/api/research", json=body)
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    detail = _wait_done(client, run_id)
    return run_id, detail


# ---------------------------------------------------------------------------
# 1. 完整消息链路（真实 llm.call_tools 流式 → 事件 → 落盘）
# ---------------------------------------------------------------------------


class TestProgressChain:
    def test_lead_messages_are_real_model_text_with_stable_identity(self, isolated_web, monkeypatch):
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, detail = _run_research(client)
            events = _events(client, run_id)
        assert detail["meta"]["status"] == "done"

        messages = _join_messages(events)
        assert list(messages.values()) == [content for content, _ in _TURNS], messages
        # 身份稳定：同一段说明的所有分片共享同一个 msg_id（不是一片一个 id），
        # turn 单调递增；段的集合与真实回合一一对应。
        lead = [e for e in events if e["type"] == "lead_message"]
        assert set(e["msg_id"] for e in lead) == {f"{run_id}:{i}" for i in range(1, 5)}
        assert [e["turn"] for e in lead] == sorted(e["turn"] for e in lead)
        # 每段说明的分片数 > 1：真的是增量，而不是一次性整段
        by_msg: dict[str, int] = {}
        for e in lead:
            by_msg[e["msg_id"]] = by_msg.get(e["msg_id"], 0) + 1
        assert all(n > 1 for n in by_msg.values()), by_msg
        # 每段说明有且只有一条 complete，且 complete 不重复全文
        for msg_id in messages:
            completes = [e for e in events
                         if e["type"] == "lead_message" and e["msg_id"] == msg_id
                         and e["mode"] == "complete"]
            assert len(completes) == 1, msg_id
        # 增量确实到了：至少一段说明被切成多条 append
        appends = [e for e in events if e["type"] == "lead_message" and e["mode"] == "append"]
        assert len(appends) > len(messages)

    def test_part_seq_is_message_local_and_global_seq_is_untouched(self, isolated_web, monkeypatch):
        """part_seq 与全局 seq 必须解耦：全局 seq 由 Web 层连续分配，用于补发去重。"""
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, _ = _run_research(client)
            events = _events(client, run_id)
        seqs = [e["seq"] for e in events]
        assert seqs == list(range(len(events))), "全局 seq 必须连续且唯一"
        lead = [e for e in events if e["type"] == "lead_message"]
        assert lead and all("part_seq" in e for e in lead)
        for msg_id in {e["msg_id"] for e in lead}:
            parts = [e["part_seq"] for e in lead
                     if e["msg_id"] == msg_id and e["mode"] == "append"]
            assert parts == list(range(len(parts))), (msg_id, parts)

    def test_tool_lifecycle_running_then_terminal_with_correlation(self, isolated_web, monkeypatch):
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, _ = _run_research(client)
            events = _events(client, run_id)
        tools = [e for e in events if e["type"] == "subagent_tool_call"]
        assert tools, "工具事件必须存在"

        running = [e for e in tools if e["phase"] == "running"]
        done = [e for e in tools if e["phase"] == "done"]
        assert running and done
        # 每次执行的调用都有 running→done 配对，身份由 (turn, call_id) 唯一定位
        running_keys = {(e["turn"], e["call_id"]) for e in running}
        done_keys = {(e["turn"], e["call_id"]) for e in done}
        assert running_keys <= done_keys
        for event in running:
            assert event["status"] == "running", "运行中的工具不能标为失败"
            assert event["ok"] is False and event["reason"] == ""  # running 不是成功态
            assert event["result_summary"] == ""
        # 序号单调：同一 (turn, call_id) 的 running 先于 done
        order = {id(e): i for i, e in enumerate(tools)}
        for key in running_keys:
            first_running = next(e for e in running if (e["turn"], e["call_id"]) == key)
            last_done = [e for e in done if (e["turn"], e["call_id"]) == key][-1]
            assert order[id(first_running)] < order[id(last_done)]
        # 成功执行的工具终态为 ok，并带上真实参数与结果摘要
        search_done = next(e for e in done if e["tool"] == "search_sources")
        assert search_done["status"] == "ok" and search_done["ok"] is True
        assert "sqlite3_rsync" in search_done["args_summary"]
        assert search_done["result_summary"]

    def test_llm_call_events_are_wired_for_lead_and_verifier(self, isolated_web, monkeypatch):
        """F7 的另一半：普通说明之外，调试 I/O 也要真的通（按 full_llm_io 语义）。"""
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, _ = _run_research(client, full_llm_io=True)
            events = _events(client, run_id)
        llm_calls = [e for e in events if e["type"] == "llm_call"]
        steps = {e["step"] for e in llm_calls}
        assert "lead_research" in steps, steps
        # 核查当前默认不执行（EXPERIMENT_PLAN §2）：产品入口不该出现 verify_draft 的调试 I/O。
        assert "verify_draft" not in steps, steps
        lead_call = next(e for e in llm_calls if e["step"] == "lead_research")
        assert lead_call["io_complete"] is True  # 调试档：完整 I/O
        assert lead_call["input"] and lead_call["output"]
        assert lead_call["in_tok"] == 396 and lead_call["out_tok"] == 98


    def test_stream_requests_usage_and_records_real_tokens(self, isolated_web, monkeypatch):
        """已核实的通道（OpenCode）必须真的请求流式用量，否则每次调用都变用量未知。"""
        from dra.simple_research import SimpleConfig  # noqa: F401

        client = _install_boundaries(monkeypatch, _script)
        monkeypatch.setattr(runtime_config, "to_subagent_config",
                            lambda _: _adapter_config(provider="opencode"))
        with TestClient(web.app) as web_client:
            run_id, _ = _run_research(web_client)
            payload = web_client.get(f"/api/runs/{run_id}/simple").json()
        streams = [r for r in client.stream_requests if r.get("tools")]
        assert streams, "Lead 循环必须走流式"
        assert all(r["stream_options"] == {"include_usage": True} for r in streams)
        # 用量真实入账（按角色分列），而不是一律未知
        assert payload["usage"]["lead"]["input"] > 0
        assert payload["usage"]["lead"]["output"] > 0
        assert payload["usage"]["lead"]["missing_usage"] == 0
        lead_calls = [c for c in payload["calls"] if c["step"].startswith("turn-")]
        assert lead_calls and all(not c["usage_unknown"] for c in lead_calls)

    def test_missing_stream_usage_is_recorded_unknown_not_zero(self, isolated_web, monkeypatch):
        """中转不回流式用量：如实标 usage_unknown，不能当成 0。"""
        _install_boundaries(monkeypatch, lambda: StreamingScript(
            [(text, list(calls)) for text, calls in _TURNS], stream_usage=False))
        monkeypatch.setattr(runtime_config, "to_subagent_config",
                            lambda _: _adapter_config(provider="opencode"))
        with TestClient(web.app) as web_client:
            run_id, _ = _run_research(web_client)
            payload = web_client.get(f"/api/runs/{run_id}/simple").json()
        lead = [c for c in payload["calls"] if c["step"].startswith("turn-")]
        assert lead and all(c["usage_unknown"] for c in lead)
        assert all(c.get("input_tokens") is None for c in lead)
        # 但不能把未知当成 0：missing_usage 要如实计数
        assert payload["usage"]["lead"]["missing_usage"] == len(lead)
        assert payload["usage"]["lead"]["input"] == 0


    def test_tool_event_count_matches_ledger_without_doubling(self, isolated_web, monkeypatch):
        """running/done 双发不能改变权威计数：账本 tool_call 条数与终态事件一一对应。"""
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, _ = _run_research(client)
            events = _events(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
            detail = client.get(f"/api/runs/{run_id}").json()
        tools = [e for e in events if e["type"] == "subagent_tool_call"]
        done = [e for e in tools if e["phase"] == "done"]
        executed_tools = [e for e in done if e["status"] == "ok" and e["tool"] != "finish_research"]
        # 账本工具条数 = 每个模型回合实际执行的研究工具（终态事件同数，不因 running 翻倍）
        assert payload["stop_reason"] == "finished"
        assert [e["tool"] for e in executed_tools] == [
            "search_sources", "read_source", "save_evidence"]
        # Web 的"成功工具调用"进度数来自账本：等于实际执行数，不是事件条数
        assert detail["executed_tool_calls"] == len(executed_tools) == 3
        # running 只出现在真要执行的调用上（finish 收工不发 running）
        assert [e["tool"] for e in running] if (running := [
            e for e in tools if e["phase"] == "running"]) else True
        assert all(e["tool"] != "finish_research" for e in running)

    def test_tool_execute_raising_emits_failed_terminal_then_propagates(
        self, isolated_web, monkeypatch
    ):
        """工具实现抛错：先补失败终态（卡片不卡在 running），异常照既有路径传播。"""
        from dra import simple_research as sr

        def boom(self, name, args):
            raise RuntimeError("工具边界炸了")

        monkeypatch.setattr(sr.ToolContext, "execute", boom)
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, detail = _run_research(client)
            events = _events(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
        tools = [e for e in events if e["type"] == "subagent_tool_call"]
        running = [e for e in tools if e["phase"] == "running"]
        done = [e for e in tools if e["phase"] == "done"]
        assert len(running) == 1 and len(done) == 1
        assert running[0]["status"] == "running"
        assert done[0]["status"] == "failed" and done[0]["ok"] is False
        assert "工具边界炸了" in done[0]["reason"]
        # 异常不被当成 ok，也不凭空继续研究：如实收口为 failed 终态
        assert payload["stop_reason"] == "failed"
        assert detail["meta"]["status"] == "failed"

    def test_tool_failure_return_is_recorded_as_failed_terminal(self, isolated_web, monkeypatch):
        """工具正常返回 ok=False（如未知工具/被拒）：终态如实标 failed，不是成功。"""
        _install_boundaries(monkeypatch, lambda: StreamingScript([
            ("我试一个不存在的工具。", [("no_such_tool", {})]),
            ("", [("finish_research", {"draft": "无法执行该工具。"})]),
        ], aux_text="无法执行该工具。"))
        with TestClient(web.app) as client:
            run_id, _ = _run_research(client)
            events = _events(client, run_id)
        done = [e for e in events
                if e["type"] == "subagent_tool_call" and e["phase"] == "done"]
        assert done and done[0]["tool"] == "no_such_tool"
        assert done[0]["status"] == "failed" and done[0]["ok"] is False
        assert "未知工具" in done[0]["reason"]


# ---------------------------------------------------------------------------
# 2. SSE 实时/补发/断线语义
# ---------------------------------------------------------------------------


class TestSseDelivery:
    def test_stream_replays_persisted_events_after_disconnect(self, isolated_web, monkeypatch):
        """断线重连：SSE 补发的 backlog 必须与落盘事件一致，且不重复。"""
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, _ = _run_research(client)
            persisted = _events(client, run_id)
            frames = []
            with client.stream("GET", f"/api/research/{run_id}/stream") as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[len("data: "):]))
                    if line.strip() == "" and frames and frames[-1]["type"] == "end":
                        break
        assert [f["seq"] for f in frames] == [e["seq"] for e in persisted]
        assert [f["type"] for f in frames] == [e["type"] for e in persisted]
        # 补发路径里消息仍能拼成同一份正文
        assert _join_messages(frames) == _join_messages(persisted)

    def test_runs_do_not_mix_events(self, isolated_web, monkeypatch):
        """两个运行的事件互不串号：SSE 与历史各归各的 run_id。"""
        _install_boundaries(monkeypatch, lambda: StreamingScript([
            ("第一问的说明。", [("search_sources", {"query": "one"})]),
            ("", [("finish_research", {"draft": "第一问答案。"})]),
        ]))
        with TestClient(web.app) as client:
            run_a, _ = _run_research(client, "问题 A")
            run_b, _ = _run_research(client, "问题 B")
            events_a = _events(client, run_a)
            events_b = _events(client, run_b)
        assert run_a != run_b
        # 管线事件带 run_id；web 层补发的 research_status 不带，
        # 因此只校验确有 run_id 的事件，不要为了断言去改协议。
        tagged_a = [e for e in events_a if "run_id" in e]
        tagged_b = [e for e in events_b if "run_id" in e]
        assert tagged_a and tagged_b
        assert all(e["run_id"] == run_a for e in tagged_a)
        assert all(e["run_id"] == run_b for e in tagged_b)
        texts_a = _join_messages(events_a)
        texts_b = _join_messages(events_b)
        assert texts_a[f"{run_a}:1"] == "第一问的说明。"
        assert texts_b[f"{run_b}:1"] == "第一问的说明。"
        # 所有消息仍按 run 隔离。
        assert all(key.startswith(f"{run_a}:") for key in texts_a)
        assert all(key.startswith(f"{run_b}:") for key in texts_b)
        assert not set(texts_a) & set(texts_b), "两个 run 的 msg_id 不得相同"

    def test_historical_read_matches_stream(self, isolated_web, monkeypatch):
        """直接打开历史（不经 SSE）看到的是同一份真实消息记录。"""
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id, _ = _run_research(client)
            from_history = _events(client, run_id)
            again = _events(client, run_id)
        assert from_history == again
        assert _join_messages(from_history) == {
            f"{run_id}:{i}": text for i, (text, _) in enumerate(_TURNS, start=1)}


# ---------------------------------------------------------------------------
# 3. 与调试 I/O 开关无关
# ---------------------------------------------------------------------------


def test_lead_messages_do_not_depend_on_debug_io(isolated_web, monkeypatch):
    """普通用户消息必须始终存在：关掉调试档也一样有说明与工具事件，只是没有完整 I/O。"""
    _install_boundaries(monkeypatch)
    with TestClient(web.app) as client:
        run_id, _ = _run_research(client, full_llm_io=False)
        events = _events(client, run_id)
    assert _join_messages(events) == {f"{run_id}:{i}": text
                                     for i, (text, _) in enumerate(_TURNS, start=1)}
    assert [e for e in events if e["type"] == "subagent_tool_call"]
    # 调试档关闭时 llm_call 仍按截断语义存在（不是"没有 I/O 就没有调用记录"）
    llm_calls = [e for e in events if e["type"] == "llm_call"]
    assert llm_calls and all(e["io_complete"] is False for e in llm_calls)


# ---------------------------------------------------------------------------
# 4. 非流式调用方兼容（注入 model_call 的旧签名）
# ---------------------------------------------------------------------------


def test_injected_model_call_still_emits_full_message_once(isolated_web, monkeypatch):
    """注入 model_call（旧签名、不支持 on_chunk）时：说明走 replace，一次给全文。"""
    from dra import research as research_entry
    from test_budget_wrapup import Script, _turn

    script = Script([
        _turn(("search_sources", {"query": "sqlite3_rsync ssh local"})),
        _turn(("finish_research", {"draft": "不要求一端为远程。"})),
    ])
    for turn in script.turns:
        turn.content = "我先查官方文档。"
        turn.assistant_message["content"] = turn.content
    original = research_entry.run_minimal_research

    def run_minimal(query, requirements=None, *, config=None, run_id="simple-research",
                    checkpoint_dir=None, resume=False, **kwargs):
        return original(query, requirements, config=config, run_id=run_id,
                        checkpoint_dir=checkpoint_dir, resume=resume,
                        model_call=script,
                        search_fn=lambda q: [_doc()],
                        ref_resolver=script)
    monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)
    # 确认真实入口签名仍接受 ref_resolver（防未来分叉悄悄失去解析器）
    assert "ref_resolver" in inspect.signature(original).parameters

    with TestClient(web.app) as client:
        run_id, _ = _run_research(client)
        events = _events(client, run_id)
    lead = [e for e in events if e["type"] == "lead_message"]
    assert lead, "非流式路径也必须发说明"
    assert all(e["mode"] == "replace" for e in lead)
    assert all(e["text"] == "我先查官方文档。" for e in lead)
    assert _join_messages(events) == {f"{run_id}:1": "我先查官方文档。",
                                     f"{run_id}:2": "我先查官方文档。"}


# ---------------------------------------------------------------------------
# 5. 有界空转保护不被过程消息削弱
# ---------------------------------------------------------------------------


def test_lead_prompt_requires_public_progress_and_stays_bounded():
    """提示词里必须有"用普通正文说明公开进展"的规则，且保留有界约束。

    断言的是**要求存在**（含同轮优先、不机械播报、纯文本仍有界三条），不是逐字镜像文案；
    文案可以改，但去掉这层要求会让过程消息重新依赖模型自发行为。
    """
    from dra.simple_research import _LEAD_SYSTEM

    assert "过程说明" in _LEAD_SYSTEM
    assert "同一次回复" in _LEAD_SYSTEM
    assert "不逐个工具机械播报" in _LEAD_SYSTEM
    assert "无进展" in _LEAD_SYSTEM


def test_text_only_turns_continue_until_model_finishes(isolated_web, monkeypatch):
    """纯文本无工具回合只提醒模型调用工具，不替用户结束研究；模型交稿后正常结束。"""
    _install_boundaries(monkeypatch, lambda: StreamingScript(
        [*[("我先说明一下方向。", []) for _ in range(3)],
         ("", [("finish_research", {"draft": "材料不足，如实说明缺口。"})])]))
    with TestClient(web.app) as client:
        run_id, detail = _run_research(client)
        payload = client.get(f"/api/runs/{run_id}/simple").json()
        events = _events(client, run_id)
    assert payload["stop_reason"] == "finished", (
        payload["stop_reason"], payload["warnings"],
        [(c["step"], c["status"], c.get("error")) for c in payload["calls"]])
    assert len([e for e in events if e["type"] == "lead_message"]) >= 3
    assert [c["step"] for c in payload["calls"]].count("wrapup") == 0
    assert len(payload["calls"]) == 4
