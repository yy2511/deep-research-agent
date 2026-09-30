"""始终开启的逐轮工具记录；独立于 UI 摘要和完整模型 I/O 开关。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from dra.llm import LLMPersistenceError


class CallJournal:
    def __init__(self, directory: Path, run_id: str):
        self.path = directory / "tool_calls.jsonl"
        self.run_id = run_id
        self._lock = threading.RLock()
        # 只在内存保留统计元数据；完整参数和回执追加落盘，不反复重写 run.json。
        self._tools: dict[str, dict] = {}
        self._started: dict[str, float] = {}

    def record(self, *, agent_id, model_call_id, turn, call, phase, budget_counted_total, status="", result=""):
        record_id = f"{agent_id}:{model_call_id}:{call.id}"
        with self._lock:
            now = time.monotonic()
            if phase == "running":
                self._started[record_id] = now
            started = self._started.get(record_id)
            elapsed = round((now - started) * 1000, 3) if phase == "done" and started is not None else None
            row = dict(schema_version=1, run_id=self.run_id, record_id=record_id,
                       agent_id=agent_id, model_call_id=model_call_id, turn=turn,
                       tool_call_id=call.id, tool=call.name, phase=phase, status=status,
                       budget_counted_total=budget_counted_total,
                       at=time.time(), elapsed_ms=elapsed,
                       arguments=call.arguments, arguments_raw=call.arguments_raw,
                       result=result if phase == "done" else None)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, TypeError, ValueError) as exc:
                raise LLMPersistenceError(f"工具调用记录保存失败：{exc}") from exc
            prior = self._tools.get(record_id, {})
            self._tools[record_id] = {k: v for k, v in row.items()
                                     if k not in ("arguments", "arguments_raw", "result")}
            self._tools[record_id]["started"] = phase == "running" or prior.get("started", False)
            if phase == "done":
                self._started.pop(record_id, None)

    def summarize(self, calls: list[dict], agents: dict, stop_reason: str) -> dict:
        """model_calls 是逻辑请求数；传输重试另记，未知用量不视为零。"""
        with self._lock:
            tools = list(self._tools.values())

        def counts(items):
            return {
                "tool_calls": len(items),
                "started": sum(bool(t["started"]) for t in items),
                "successful": sum(t["phase"] == "done" and t["status"] == "ok" for t in items),
                "failed": sum(t["phase"] == "done" and t["status"] == "failed" for t in items),
                "rejected": sum(t["phase"] == "done" and t["status"] == "rejected" for t in items),
                "cancelled": sum(t["phase"] == "done" and t["status"] == "cancelled" for t in items),
                "unfinished": sum(t["phase"] != "done" for t in items),
            }

        rounds = []
        counted_by_agent = {}
        for call in calls:
            selected = [t for t in tools if t["model_call_id"] == call["call_id"]]
            actor = call["agent_id"]
            before = counted_by_agent.get(actor, 0)
            after = max((t["budget_counted_total"] for t in selected), default=before)
            counted_by_agent[actor] = after
            rounds.append({**call, **counts(selected), "budget_counted_tool_calls": after - before, "tools": [
                {k: t[k] for k in ("record_id", "tool", "phase", "status", "elapsed_ms")}
                for t in selected]})
        return dict(schema_version=1, run_id=self.run_id, stop_reason=stop_reason,
                    totals={"model_calls": len(calls), **counts(tools),
                            "budget_counted_tool_calls": sum(counted_by_agent.values())},
                    agents=agents, rounds=rounds)
