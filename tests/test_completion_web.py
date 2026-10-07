"""只替换模型 HTTP/搜索边界，验证主子异步→收尾→日志落盘→Web 回放。"""
import json
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from dra import llm, research, simple_research as sr, timing, web
from test_finish_and_trace import FINISH, QUOTE
from test_web_research_e2e import isolated_web, _wait_done  # noqa: F401 -- shared fixture
from test_research_progress_stream import StreamingScript
from test_budget_wrapup import _doc


def test_late_child_cards_and_full_traces_survive_web_finish_and_replay(isolated_web, monkeypatch):
    scripts = {}
    requests = []
    child_finish = {k: v for k, v in FINISH.items() if k != "research_plan"}
    child_finish["coverage"] = [{**row, "requirement_id": "sub"} for row in FINISH["coverage"]]

    def create(**kwargs):
        ctx = timing.get_ctx()
        requests.append((ctx, kwargs))
        if ctx["step"] == "lead_wrapup":
            assert QUOTE in kwargs["messages"][-1]["content"]
            assert [s["function"]["name"] for s in kwargs["tools"]] == ["finish_research"]
            call = NS(id="wrap-finish", function=NS(name="finish_research",
                       arguments=json.dumps(FINISH, ensure_ascii=False)))
            return NS(usage=None, choices=[NS(message=NS(content="", tool_calls=[call]))])
        actor = ctx.get("delegation_id") or "lead"
        if actor not in scripts:
            turns = [("派子任务核对，主线保留收尾额度。", [("delegate_research", {
                "tasks": [{"goal": "参数核实", "context": "离线测试"},
                          {"goal": "价格核实", "context": "离线测试"}]} )])]
            if actor != "lead":
                turns = [
                    ("检索", [("search_sources", {"query": actor})]),
                    ("精读", [("read_source", {"url": _doc().source_url})]),
                    ("保存", [("save_evidence", {"cards": [{"claim": "公开参数", "quote": QUOTE}]})]),
                    ("尝试交稿", [("finish_research", {"draft": "（正文见上）核对完毕 [c1]。"})]),
                    ("交稿", [("finish_research", child_finish)]),
                ]
            scripts[actor] = StreamingScript(turns)
        assert kwargs.get("stream") is True
        return iter(scripts[actor].stream_for(kwargs))

    monkeypatch.setattr(llm, "_get_client", lambda *a, **kw: NS(chat=NS(completions=NS(create=create))))
    monkeypatch.setattr(sr, "_retrieve", lambda *a, **kw: [_doc(raw=QUOTE)])
    original = research.run_minimal_research
    def run(*args, **kwargs):
        kwargs["config"] = kwargs["config"].model_copy(update={"max_llm_calls": 2, "wall_timeout_s": 20, "wrapup_reserve_s": 0})
        return original(*args, **kwargs)
    monkeypatch.setattr(research, "run_minimal_research", run)

    with TestClient(web.app) as client:
        run_id = client.post("/api/research", json={"query": "核对参数和价格", "mode": "simple",
            "trace_full_llm_io": True}).json()["run_id"]
        _wait_done(client, run_id)
        rows = [json.loads(line) for line in client.get(f"/api/runs/{run_id}/events").text.splitlines()]
        result = client.get(f"/api/runs/{run_id}/simple").json()
        assert result["stop_reason"] == "call_budget+wrapup"
        assert result["gaps"] == FINISH["gaps"]
        assert result["research_coverage"] == {"goal": "partial"}
        assert len(result["cards"]) == 2
        terminal = next(e for e in rows if e["type"] == "research_status" and e["phase"] == "finished")
        for key in ("gaps", "coverage_reasons", "requirements"):
            assert terminal[key] == result[key]
        children = [e for e in rows if e["type"] == "delegation_updated" and e["kind"] == "result"]
        assert len(children) == 2 and all(c["task"]["status"] == "completed" for c in children)
        assert all(rows.index(c) < rows.index(terminal) for c in children)
        traces = [e for e in rows if e["type"] == "llm_call"]
        assert {e["delegation_id"] for e in traces} == {"", "sub1", "sub2"}
        assert len({e["call_id"] for e in traces}) == len(traces)
        for ctx, kwargs in requests:
            trace = next(e for e in traces if e["call_id"] == ctx["call_id"])
            assert trace["input"] == llm._render_input(kwargs["messages"])
            assert trace["tools"] == kwargs["tools"]
        saves = [e for e in rows if e["type"] == "subagent_tool_call" and e["tool"] == "save_evidence"
                 and e["phase"] == "done"]
        assert len(saves) == 2
        for saved in saves:
            assert json.loads(saved["arguments_raw"])["cards"][0]["quote"] == QUOTE
            assert saved["model_call_id"] in {t["call_id"] for t in traces}
        reads = [e for e in rows if e["type"] == "subagent_tool_call" and e["tool"] == "read_source"
                 and e["phase"] == "done"]
        assert all("暂未公开报价" in e["result_full"] for e in reads)
        assert all(len(e["result_summary"]) <= 600 for e in reads)
        assert client.get(f"/api/runs/{run_id}/events").text == client.get(f"/api/runs/{run_id}/events").text
        disk = (isolated_web[0]._dir(run_id) / "events.jsonl").read_text()
        assert [json.loads(line) for line in disk.splitlines()] == rows
        ledger = json.loads((isolated_web[0]._dir(run_id) / "run.json").read_text())
        rejected = [a for a in ledger["actions"] if a["action"] == "finish_rejected"]
        assert len(rejected) == 2, "两子任务使用相同回合/工具编号时，拒收账本也不能互相覆盖"
