"""RunStore：web 工作台的 run 落盘层（runs/<id>/ = meta.json + events.jsonl + report.md）。

设计要点（spec §4）：事件流是唯一真相源——统计（aggregate_stats）与历史回放都从
events.jsonl 推导，不另存状态快照（快照可推导、反向不行）。append 每事件即写盘
（open-append-close，事件速率 <10/s，耐用性优先于句柄复用）。CLI 的 reports/ 不动。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

from dra.events import EVENT_SCHEMA_VERSION


class IncompatibleEventSchemaError(ValueError):
    """本地事件记录不属于当前协议，调用方必须显式告知用户不可回放。"""


def replay_compatibility(meta: dict) -> tuple[bool, str | None]:
    version = meta.get("event_schema_version")
    if version == EVENT_SCHEMA_VERSION:
        return True, None
    if version is None:
        return False, "旧协议（缺少 event_schema_version），不可回放"
    return False, (
        f"旧协议（event_schema_version={version}，当前={EVENT_SCHEMA_VERSION}），不可回放"
    )


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, run_id: str) -> Path:
        return self.root / run_id

    def _meta_path(self, run_id: str) -> Path:
        return self._dir(run_id) / "meta.json"

    def _write_meta(self, run_id: str, meta: dict) -> None:
        self._meta_path(run_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def create_run(self, query: str, plan: dict | None, config_summary: dict) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        self._dir(run_id).mkdir(parents=True)
        self._write_meta(run_id, {
            "run_id": run_id, "query": query, "plan": plan,
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "status": "running", "started_at": time.time(), "finished_at": None,
            "config_summary": config_summary, "stats": None,
        })
        return run_id

    def append_event(self, run_id: str, evt: dict) -> None:
        with open(self._dir(run_id) / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")

    def save_report(self, run_id: str, md: str) -> Path:
        p = self._dir(run_id) / "report.md"
        p.write_text(md, encoding="utf-8")
        return p

    def finalize(self, run_id: str, status: str, stats: dict | None) -> None:
        meta = self.get_meta(run_id)
        if meta is None:
            return
        meta.update(status=status, finished_at=time.time(), stats=stats)
        self._write_meta(run_id, meta)

    def get_meta(self, run_id: str) -> dict | None:
        p = self._meta_path(run_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def list_runs(self) -> list[dict]:
        metas = [m for d in self.root.iterdir() if d.is_dir()
                 and (m := self.get_meta(d.name)) is not None]
        enriched = []
        for meta in metas:
            compatible, error = replay_compatibility(meta)
            enriched.append({
                **meta,
                "replay_compatible": compatible,
                "replay_error": error,
            })
        return sorted(enriched, key=lambda m: m.get("started_at") or 0, reverse=True)

    def read_events_text(self, run_id: str) -> str | None:
        p = self._dir(run_id) / "events.jsonl"
        meta = self.get_meta(run_id)
        if meta is None:
            if p.exists():
                raise IncompatibleEventSchemaError("旧协议（缺少 meta.json），不可回放")
            return None
        compatible, error = replay_compatibility(meta)
        if not compatible:
            raise IncompatibleEventSchemaError(error)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def read_report(self, run_id: str) -> str | None:
        p = self._dir(run_id) / "report.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def mark_orphans(self) -> int:
        """服务启动时清点上次没善终的 run（诚实边界：进程死了状态不能停在 running）。"""
        n = 0
        metas = [
            meta
            for directory in self.root.iterdir()
            if directory.is_dir()
            and (meta := self.get_meta(directory.name)) is not None
        ]
        for meta in metas:
            if meta["status"] == "running":
                meta.update(status="failed", fail_reason="server restart",
                            finished_at=meta.get("finished_at") or time.time())
                self._write_meta(meta["run_id"], meta)
                n += 1
        return n


class PlanAttemptStore:
    """计划阶段落盘（plans/<id>/ = meta.json + events.jsonl）。

    与 RunStore 平行：/api/plan、/api/plan/revise 在研究 run 创建前就会调 planner，
    失败时过去没有 run_id，llm_call / json_retry 无处可写。这里给每次规划尝试
    一个独立 id，便于排「缺 initial_tasks」类故障。不进 Sidebar 历史列表。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, plan_id: str) -> Path:
        return self.root / plan_id

    def _meta_path(self, plan_id: str) -> Path:
        return self._dir(plan_id) / "meta.json"

    def _write_meta(self, plan_id: str, meta: dict) -> None:
        self._meta_path(plan_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def create(self, *, query: str, kind: str, config_summary: dict) -> str:
        plan_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        self._dir(plan_id).mkdir(parents=True)
        self._write_meta(plan_id, {
            "plan_id": plan_id,
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "kind": kind,  # plan | revise
            "query": query,
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "config_summary": config_summary,
            "error": None,
        })
        return plan_id

    def append_event(self, plan_id: str, evt: dict) -> None:
        with open(self._dir(plan_id) / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")

    def finalize(
        self,
        plan_id: str,
        status: str,
        *,
        error: str | None = None,
        plan: dict | None = None,
    ) -> None:
        p = self._meta_path(plan_id)
        if not p.exists():
            return
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta.update(
            status=status,
            finished_at=time.time(),
            error=error,
        )
        if plan is not None:
            # 成功计划快照（不含超大 trace，避免 meta 膨胀）
            meta["plan"] = {
                k: plan[k]
                for k in ("clarified_query", "plan_nodes", "initial_tasks", "budget")
                if k in plan
            }
        self._write_meta(plan_id, meta)

    def get_meta(self, plan_id: str) -> dict | None:
        p = self._meta_path(plan_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def read_events_text(self, plan_id: str) -> str | None:
        p = self._dir(plan_id) / "events.jsonl"
        meta = self.get_meta(plan_id)
        if meta is None:
            if p.exists():
                raise IncompatibleEventSchemaError("旧协议（缺少 meta.json），不可回放")
            return None
        compatible, error = replay_compatibility(meta)
        if not compatible:
            raise IncompatibleEventSchemaError(error)
        return p.read_text(encoding="utf-8") if p.exists() else None


def aggregate_stats(events: list[dict]) -> dict:
    """事件流 → 统计（口径与 timing.py 一致：聚合与墙钟分列，聚合/墙钟≈并发度）。"""
    llm: dict[tuple, dict] = {}
    steps: dict[str, dict] = {}
    tools: dict[str, dict] = {}
    for e in events:
        t = e.get("type")
        if t == "llm_call":
            k = (e.get("step") or "?", e.get("model") or "?")
            r = llm.setdefault(k, {"step": k[0], "model": k[1], "calls": 0,
                                   "total_ms": 0, "in_tok": 0, "out_tok": 0})
            r["calls"] += 1
            r["total_ms"] += e.get("ms") or 0
            r["in_tok"] += e.get("in_tok") or 0
            r["out_tok"] += e.get("out_tok") or 0
        elif t == "step_done":
            r = steps.setdefault(e.get("step") or "?",
                                 {"step": e.get("step") or "?", "calls": 0, "total_ms": 0})
            r["calls"] += 1
            r["total_ms"] += e.get("ms") or 0
        elif t == "subagent_tool_call":
            r = tools.setdefault(e.get("tool") or "?",
                                 {"tool": e.get("tool") or "?", "calls": 0, "ok": 0,
                                  "accepted": 0, "rejected": 0})
            r["calls"] += 1
            r["ok"] += 1 if e.get("ok") else 0
            r["accepted"] += e.get("accepted") or 0
            r["rejected"] += e.get("rejected") or 0
    ts = [e["ts"] for e in events if "ts" in e]
    return {
        "llm": sorted(llm.values(), key=lambda r: -r["total_ms"]),
        "steps": sorted(steps.values(), key=lambda r: -r["total_ms"]),
        "tools": sorted(tools.values(), key=lambda r: -r["calls"]),
        "wall_s": round(max(ts) - min(ts), 1) if len(ts) >= 2 else 0.0,
        "agg_s": round(sum(r["total_ms"] for r in steps.values()) / 1000, 1),
    }
