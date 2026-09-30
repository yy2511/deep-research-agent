"""生成自主研究的离线界面演示：合成事件，明确标注，不调用模型或检索。"""
import json
from pathlib import Path

from dra.events import EventType as E


def demo_events() -> list[dict]:
    rows = [
        {"type": E.RESEARCH_STATUS.value, "mode": "simple", "phase": "starting", "effective_query": "自主 Agent 如何完成一次研究？", "requirements": [{"id": "r1", "text": "展示研究过程"}]},
        {"type": E.LEAD_MESSAGE.value, "msg_id": "demo-note", "turn": 1, "part_seq": 0, "mode": "replace", "text": "这是离线演示。我会检索资料、阅读来源，然后整理答案。", "complete": True, "role": "lead", "kind": "note"},
        {"type": E.RESEARCH_PLAN_UPDATED.value, "items": [{"id": "p1", "text": "检索并阅读资料", "status": "doing"}, {"id": "p2", "text": "整理答案", "status": "todo"}], "note": "先确认来源，再组织结论。"},
    ]
    for turn, (tool, args, result) in enumerate([
        ("search_sources", {"query": "自主研究演示资料"}, {"results": [{"source_ref": "s1", "title": "离线演示资料（合成）", "url": "https://example.org/demo", "snippet": "此内容仅供界面演示。"}], "total": 1, "shown": 1}),
        ("read_source", {"source_ref": "s1"}, {"title": "离线演示资料（合成）", "source_ref": "s1", "excerpts": [{"quote": "此内容仅供界面演示。", "location": "chars=0-11"}]}),
    ], 1):
        for phase in ("running", "done"):
            rows.append({"type": E.SUBAGENT_TOOL_CALL.value, "mode": "simple", "role": "lead", "turn": turn, "call_id": f"demo-{turn}", "call_no": turn, "tool": tool, "phase": phase, "ok": phase == "done", "status": "ok" if phase == "done" else "running", "args_summary": json.dumps(args, ensure_ascii=False), "result_summary": json.dumps(result, ensure_ascii=False) if phase == "done" else "", "display": result if phase == "done" else {}})
    rows.extend([
        {"type": E.RESEARCH_PLAN_UPDATED.value, "items": [{"id": "p1", "text": "检索并阅读资料", "status": "done"}, {"id": "p2", "text": "整理答案", "status": "done"}], "note": "演示结束。"},
        {"type": E.REPORT_MD.value, "markdown": "## 自主研究演示\n\nAgent 根据问题选择工具、读取资料并整理答案。此处为合成事件演示，没有发起真实检索或模型调用。"},
        {"type": E.RESEARCH_STATUS.value, "mode": "simple", "phase": "finished", "tool_calls": 2, "model_calls": 0, "sources": 0, "cards": 0, "stop_reason": "finished", "delivery": "delivered", "coverage": {"r1": "answered"}},
        {"type": E.END.value},
    ])
    return [{**row, "seq": seq, "ts": 1700000000 + seq, "run_id": "demo"} for seq, row in enumerate(rows)]


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[1] / "fixtures/demo_run/events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in demo_events()), encoding="utf-8")
    print(f"已生成 {path}")
