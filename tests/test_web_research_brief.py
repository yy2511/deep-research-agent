"""开跑前两段的 Web 接口：先判断缺口、再生成计划。

只替换外部边界（模型回包）；路由、trace 落盘、run 注册、requirements 透传到内核
都走真实实现。这里锁住的两条行为：

- **有缺口时不生成计划**（只调一次模型），因为生成了却不给用户看等于白花一次输出，
  而且那份计划必然带兜底措辞；
- 规划没跑出来时如实报错，**不静默跳过确认直接开跑**。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dra import llm, web

from test_web_research_e2e import QUERY, _install_boundaries, _wait_done, isolated_web  # noqa: F401

BRIEF = {
    "original_query": QUERY,
    "objective": "说清取舍并给出选型建议",
    "requirements": [
        {"id": "goal", "text": QUERY, "kind": "goal"},
        {"id": "offline", "text": "必须能离线跑", "kind": "constraint"},
    ],
    "route": [
        {"id": "capability", "text": "先看清两者的能力边界"},
        {"id": "cost", "text": "再比成本与维护代价"},
    ],
    "assumptions": [],
    "needs_confirmation": True,
    "confirmation_reason": "路线存在真实取舍",
}

GAP = {
    "id": "scale",
    "header": "确认",
    "question": "你的服务规模大概是哪一档？这决定对比的重点。",
    "options": [
        {"label": "日活 10 万以内 (Recommended)", "description": "重点放在迁移成本。"},
        {"label": "日活百万级", "description": "重点放在扩展性。"},
    ],
}
GAPS_ONLY = {"original_query": QUERY, "gaps": [GAP]}
NO_GAPS = {"original_query": QUERY, "gaps": []}


def _install_brief_model(monkeypatch, replies):
    """替换模型边界（brief 走 ``llm.call_json`` → ``llm.chat``）。

    replies 可以是 dict（每次同答）或 list（按调用次序取，末项复用）。
    """
    scripted = list(replies) if isinstance(replies, list) else [replies]
    seen = {"messages": []}

    def chat(messages, **kwargs):
        seen["messages"].append(messages)
        raw = scripted[min(len(seen["messages"]) - 1, len(scripted) - 1)]
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)

    monkeypatch.setattr(llm, "chat", chat)
    return seen


class TestBriefEndpoint:
    @pytest.mark.parametrize("endpoint,body", [
        ("/api/brief", {"query": QUERY}),
        ("/api/brief/plan", {"query": QUERY, "gaps": [], "answers": []}),
        ("/api/brief/revise", {"brief": BRIEF, "feedback": "补充比较成本"}),
    ])
    @pytest.mark.parametrize("timeout", [False, True])
    def test_transport_failure_has_readable_error_and_trace(self, isolated_web, monkeypatch, endpoint, body, timeout):
        store, registry = isolated_web
        def fail(*args, **kwargs):
            if timeout:
                raise llm.LLMRequestTimeout("request timeout")
            try:
                raise ConnectionError("SSL UNEXPECTED_EOF")
            except ConnectionError as cause:
                raise RuntimeError("LLM 调用失败（已重试 1 次）: Connection error.") from cause
        monkeypatch.setattr(llm, "chat", fail)
        with TestClient(web.app, raise_server_exceptions=False) as client:
            response = client.post(endpoint, json=body)
        assert response.status_code == (504 if timeout else 503), response.text
        detail = response.json()["detail"]
        assert "超时" in detail["message"] if timeout else "连接" in detail["message"]
        assert detail["trace"]["plan_id"]
        assert store.list_runs() == [] and registry.active_count() == 0

    def test_question_with_gaps_returns_questions_only(self, isolated_web, monkeypatch):  # noqa: F811
        """有缺口就只问：**一次模型调用**，计划压根不生成。"""
        store, registry = isolated_web
        seen = _install_brief_model(monkeypatch, GAPS_ONLY)
        with TestClient(web.app) as client:
            resp = client.post("/api/brief", json={"query": QUERY})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "clarify"
        assert [gap["id"] for gap in body["gaps"]] == ["scale"]
        assert body["gaps"][0]["options"][0]["label"].endswith("(Recommended)")
        assert "brief" not in body
        assert len(seen["messages"]) == 1, "有缺口时不该再花一次调用去生成计划"
        assert body["trace"]["plan_id"]
        assert store.list_runs() == []          # 只规划、不开跑
        assert registry.active_count() == 0

    def test_question_without_gaps_goes_straight_to_plan(self, isolated_web, monkeypatch):
        store, _registry = isolated_web
        seen = _install_brief_model(monkeypatch, [NO_GAPS, BRIEF])
        with TestClient(web.app) as client:
            resp = client.post("/api/brief", json={"query": QUERY})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "plan"
        assert body["brief"]["objective"] == BRIEF["objective"]
        assert "gaps" not in body
        assert len(seen["messages"]) == 2, "判断 + 生成，两段各一次"
        assert store.list_runs() == []

    def test_empty_query_is_rejected_without_calling_model(self, isolated_web, monkeypatch):
        def _must_not_run(*args, **kwargs):
            raise AssertionError("空问题不应触达模型")

        monkeypatch.setattr(llm, "chat", _must_not_run)
        with TestClient(web.app) as client:
            resp = client.post("/api/brief", json={"query": "   "})
        assert resp.status_code == 400

    def test_planning_failure_is_502_and_does_not_start_a_run(self, isolated_web, monkeypatch):
        """模型没给出可用判断：如实报 502，绝不降级成"不用确认，直接开跑"。"""
        store, _registry = isolated_web
        _install_brief_model(monkeypatch, "这不是 JSON")
        with TestClient(web.app) as client:
            resp = client.post("/api/brief", json={"query": QUERY})
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert detail["trace"]["plan_id"], "失败也要留下 trace，否则无法排查"
        assert store.list_runs() == []


class TestClarification:
    def test_answers_reach_the_planner(self, isolated_web, monkeypatch):
        seen = _install_brief_model(monkeypatch, BRIEF)
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/brief/plan",
                json={
                    "query": QUERY,
                    "gaps": [GAP],
                    "answers": [
                        {"id": "scale", "selected": ["日活 10 万以内 (Recommended)"], "custom": ""}
                    ],
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "plan"
        assert body["brief"]["objective"] == BRIEF["objective"]
        prompt = json.dumps(seen["messages"][-1], ensure_ascii=False)
        assert "日活 10 万以内" in prompt
        assert "确认" in prompt

    def test_skipping_everything_is_recorded_as_skipped(self, isolated_web, monkeypatch):
        """跳过不是"没这回事"：答案文本要标出来，模型据此把前提写进 assumptions。"""
        seen = _install_brief_model(
            monkeypatch,
            {**BRIEF, "assumptions": ["假设服务规模中等、痛点在 JSON 查询"]},
        )
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/brief/plan", json={"query": QUERY, "gaps": [GAP], "answers": []}
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["brief"]["assumptions"] == ["假设服务规模中等、痛点在 JSON 查询"]
        assert "跳过" in json.dumps(seen["messages"][-1], ensure_ascii=False)

    def test_invalid_gaps_are_rejected(self, isolated_web, monkeypatch):
        _install_brief_model(monkeypatch, BRIEF)
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/brief/plan", json={"query": QUERY, "gaps": [{"foo": "bar"}], "answers": []}
            )
        assert resp.status_code == 502     # 契约不满足：按"规划没跑出来"处理

    def test_empty_query_is_rejected(self, isolated_web, monkeypatch):
        _install_brief_model(monkeypatch, BRIEF)
        with TestClient(web.app) as client:
            resp = client.post("/api/brief/plan", json={"query": "  ", "gaps": [], "answers": []})
        assert resp.status_code == 400


class TestBriefRevision:
    def test_feedback_reaches_the_planner_and_ids_are_stable(self, isolated_web, monkeypatch):
        revised = {
            **BRIEF,
            "requirements": [
                *BRIEF["requirements"],
                {"id": "budget", "text": "预算不超过 1 万", "kind": "constraint"},
            ],
        }
        seen = _install_brief_model(monkeypatch, revised)
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/brief/revise", json={"brief": BRIEF, "feedback": "预算不超过 1 万"}
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "plan"
        assert [r["id"] for r in body["brief"]["requirements"]] == ["goal", "offline", "budget"]
        assert "预算不超过 1 万" in json.dumps(seen["messages"][-1], ensure_ascii=False)

    def test_wrong_shape_brief_is_rejected(self, isolated_web, monkeypatch):
        """是 dict 但不满足 brief 契约：400，由 brief 自己拒绝而不是硬塞给规划器。"""
        _install_brief_model(monkeypatch, BRIEF)
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/brief/revise", json={"brief": {"foo": "bar"}, "feedback": "随便改改"}
            )
        assert resp.status_code == 400

    @pytest.mark.parametrize("bad", [None, [], "brief"])
    def test_non_dict_brief_is_rejected_by_request_contract(self, isolated_web, monkeypatch, bad):
        """连 dict 都不是：请求体契约层就拒（422），不进入规划。"""
        _install_brief_model(monkeypatch, BRIEF)
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/brief/revise", json={"brief": bad, "feedback": "随便改改"}
            )
        assert resp.status_code == 422

    def test_empty_feedback_is_rejected(self, isolated_web, monkeypatch):
        _install_brief_model(monkeypatch, BRIEF)
        with TestClient(web.app) as client:
            resp = client.post("/api/brief/revise", json={"brief": BRIEF, "feedback": "  "})
        assert resp.status_code == 400


class TestResearchWithBrief:
    def test_confirmed_requirements_reach_kernel_and_meta(self, isolated_web, monkeypatch):
        """确认过的目标与硬要求必须真的进了运行，而不是只在请求里走一遭。"""
        store, _registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/research", json={"query": QUERY, "mode": "simple", "brief": BRIEF}
            )
            assert resp.status_code == 200, resp.text
            run_id = resp.json()["run_id"]
            detail = _wait_done(client, run_id)
            result = client.get(f"/api/runs/{run_id}/simple").json()
        assert [r["id"] for r in result["requirements"]] == ["goal", "offline"]
        assert {r["kind"] for r in result["requirements"]} == {"goal", "constraint"}
        # 开跑前确认了什么，事后要能从这个 run 自己查到。
        recorded = detail["meta"]["config_summary"]["research_brief"]
        assert recorded["objective"] == BRIEF["objective"]
        assert [item["id"] for item in recorded["route"]] == ["capability", "cost"]

    def test_query_mismatching_brief_is_rejected(self, isolated_web, monkeypatch):
        seen = _install_brief_model(monkeypatch, BRIEF)
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/research",
                json={"query": "另一个问题", "mode": "simple", "brief": BRIEF},
            )
        assert resp.status_code == 400
        assert "original_query" in resp.json()["detail"]
        assert seen["messages"] == []

    def test_confirmed_brief_reaches_the_kernel_as_mission_context(self, isolated_web, monkeypatch):
        """确认过的目标 / 路线 / 假设必须进研究上下文。

        只写进 meta 是不够的：那样用户确认了半天，研究时模型照样看不到——等于白问。
        """
        _install_boundaries(monkeypatch)
        from dra import research as research_entry

        seen: dict = {}
        inner = research_entry.run_minimal_research

        def wrapper(query, requirements=None, *, brief_context=None, **kwargs):
            seen["brief_context"] = brief_context
            return inner(query, requirements, brief_context=brief_context, **kwargs)

        monkeypatch.setattr(research_entry, "run_minimal_research", wrapper)
        brief = {**BRIEF, "assumptions": ["假设服务规模中等"]}
        with TestClient(web.app) as client:
            run_id = client.post(
                "/api/research", json={"query": QUERY, "mode": "simple", "brief": brief}
            ).json()["run_id"]
            _wait_done(client, run_id)

        context = seen["brief_context"]
        assert "【确认过的目标】" in context and BRIEF["objective"] in context
        assert "【初步路线】" in context and "先看清两者的能力边界" in context
        assert "【未经用户确认的前提】" in context and "假设服务规模中等" in context

    def test_without_brief_falls_back_to_whole_query(self, isolated_web, monkeypatch):
        """不带 brief 仍能开跑（脚本与旧客户端）：退化为"整问即一条要求"，不报错。"""
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            _wait_done(client, run_id)
            result = client.get(f"/api/runs/{run_id}/simple").json()
        assert result["requirements"] == [{"id": "goal", "text": QUERY, "kind": "goal", "output_formats": []}]


class TestBriefStreaming:
    """``?stream=true``：规划边算边推，先报进入哪一步和思考片段，最后给出与普通接口相同的结果。"""

    @staticmethod
    def _events(response):
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def test_stream_reports_stages_thinking_and_result(self, isolated_web, monkeypatch):  # noqa: F811
        replies = [NO_GAPS, BRIEF]

        def chat(messages, **kwargs):
            kwargs["on_reasoning"]("先想")
            kwargs["on_reasoning"]("一想")
            return json.dumps(replies.pop(0), ensure_ascii=False)

        monkeypatch.setattr(llm, "chat", chat)
        with TestClient(web.app) as client:
            response = client.post("/api/brief?stream=true", json={"query": QUERY})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = self._events(response)
        stages = [e["stage"] for e in events if e["type"] == "stage"]
        assert stages == ["gaps", "plan"]
        thinking = "".join(e["text"] for e in events if e["type"] == "reasoning" and e["stage"] == "gaps")
        assert thinking == "先想一想"
        result = events[-1]
        assert result["type"] == "result" and result["data"]["kind"] == "plan"
        assert result["data"]["brief"]["objective"] == BRIEF["objective"]
        assert result["data"]["trace"]["plan_id"]

    def test_stream_reports_failure_as_last_event(self, isolated_web, monkeypatch):  # noqa: F811
        def fail(*args, **kwargs):
            raise llm.LLMRequestTimeout("request timeout")

        monkeypatch.setattr(llm, "chat", fail)
        with TestClient(web.app) as client:
            response = client.post("/api/brief?stream=true", json={"query": QUERY})
        last = self._events(response)[-1]
        assert last["type"] == "error" and last["status"] == 504
        assert "超时" in last["detail"]["message"] and last["detail"]["trace"]["plan_id"]

    def test_stream_still_rejects_empty_query_before_streaming(self, isolated_web):  # noqa: F811
        with TestClient(web.app) as client:
            assert client.post("/api/brief?stream=true", json={"query": "  "}).status_code == 400


def test_planner_stream_stops_background_work_when_browser_leaves():
    """浏览器断开后，后台规划收到停止信号，不再继续调用模型花钱。"""
    import asyncio
    import threading
    import time

    started, stopped = threading.Event(), threading.Event()

    def work():
        started.set()
        for _ in range(300):
            if llm.cancellation_requested():
                stopped.set()
                raise RuntimeError("cancelled")
            time.sleep(0.01)
        return {"kind": "plan"}

    class LeftPage:
        async def is_disconnected(self):
            return started.is_set()

    response = web._planner_stream(work, LeftPage())

    async def drain():
        return [chunk async for chunk in response.body_iterator]

    assert asyncio.run(drain()) == []
    assert stopped.wait(2), "后台规划没有收到停止信号"
