"""RunStore：web 工作台的 run 落盘层（runs/<id>/ = meta.json + events.jsonl + report.md）。

自主研究的材料与调用记录保存在 run.json；events.jsonl 用于界面与观测统计。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
        from dra.durable import atomic_json
        atomic_json(self._meta_path(run_id), meta)

    def create_run(self, query: str, plan: dict | None, config_summary: dict, *, identity: dict | None = None) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        self._dir(run_id).mkdir(parents=True)
        self._write_meta(run_id, {
            "run_id": run_id, "query": query, "plan": plan,
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "status": "running", "started_at": time.time(), "finished_at": None,
            "config_summary": config_summary, "stats": None,
            **({"conversation_id": run_id, "turn_index": 1, "parent_run_id": None}
               if config_summary.get("mode") == "simple" else {}),
            **(identity or {}),
        })
        return run_id

    def append_event(self, run_id: str, evt: dict) -> None:
        with open(self._dir(run_id) / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
            if evt.get("event_id"):
                f.flush()
                os.fsync(f.fileno())


    def simple_result_path(self, run_id: str) -> Path:
        return self._dir(run_id) / "simple.json"

    def save_simple_result(self, run_id: str, payload: dict) -> Path:
        """simple 路径的四状态结果落盘。

        与 report.md 并列、不改既有台账：刷新/历史回看读这一份即可得到与结束时
        一致的覆盖/停止/交付/核查四项状态，不需要从事件流反推。
        """
        path = self.simple_result_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def read_simple_result(self, run_id: str) -> dict | None:
        path = self.simple_result_path(run_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

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

    def conversation_turns(self, conversation_id: str) -> list[dict]:
        return sorted((m for m in self.list_runs()
                       if m.get("conversation_id", m["run_id"]) == conversation_id),
                      key=lambda m: (m.get("turn_index", 1), m.get("started_at", 0)))

    def conversations(self) -> list[dict]:
        groups = {}
        for meta in self.list_runs():
            cid = meta.get("conversation_id", meta["run_id"])
            group = groups.setdefault(cid, {"conversation_id":cid,"turns":[]})
            group["turns"].append(meta)
        result = []
        for cid, group in groups.items():
            turns = sorted(group["turns"], key=lambda m:m.get("turn_index",1))
            result.append({**turns[-1], "conversation_id":cid,"query":turns[0]["query"],
                           "latest_run_id":turns[-1]["run_id"],"turn_count":len(turns)})
        return sorted(result,key=lambda m:m.get("started_at") or 0,reverse=True)

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

    def executed_tool_calls(self, run_id: str) -> int:
        """该运行成功执行的工具调用数（失败与被拒的调用不计，不是预算已消耗次数）。

        与 Web 进度事件 ``research_status.tool_calls`` 同口径，刷新/历史回看因此
        与结束时看到的一致，不需要前端自己猜。
        """
        try:
            ledger = json.loads((self._dir(run_id) / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return sum(
            1
            for action in ledger.get("actions") or []
            if action.get("action") == "tool_call" and (action.get("payload") or {}).get("ok")
        )

    def tool_event_detail(self, run_id: str, event: dict) -> dict:
        """按事件身份读取完整工具 I/O；旧记录无台账时仍返回原摘要。"""
        if (event.get("type") != "subagent_tool_call" or event.get("phase") != "done"
                or event.get("io_complete") or not event.get("model_call_id")
                or not event.get("call_id")):
            return event
        identity = {
            "run_id": run_id,
            "agent_id": event.get("delegation_id") or "lead",
            "model_call_id": event["model_call_id"],
            "tool_call_id": event["call_id"],
            "turn": event.get("turn"),
            "tool": event.get("tool"),
            "phase": "done",
        }
        try:
            with (self._dir(run_id) / "tool_calls.jsonl").open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if not isinstance(row, dict) or any(row.get(k) != v for k, v in identity.items()):
                        continue
                    if isinstance(row.get("arguments_raw"), str) and isinstance(row.get("result"), str):
                        return {**event, "arguments_raw": row["arguments_raw"],
                                "result_full": row["result"], "io_complete": True}
        except FileNotFoundError:
            pass
        return event

    def read_source_text(self, run_id: str, ref: str) -> str | None:
        """读某个来源的**已登记**保存快照；未登记、非全文、或内容对不上哈希都拒绝。

        只做"文件在不在"是不够的：
        - 孤立文件（``sources/s999.txt`` 存在但账本里没有）不是这个运行的来源；
        - 被改过的正文不能冒充当初引用的原文（卡片 quote 与区间都会因此失真）。
        因此这里要求 ref 命中账本里的 ``snapshot_kind == "full"`` 记录，并校验
        ``content_sha256``；对不上就返回 None（由调用方转成明确的 404）。
        """
        if not re.fullmatch(r"s[0-9]{1,6}", ref or ""):
            return None
        try:
            ledger = json.loads((self._dir(run_id) / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        entry = next(
            (
                item
                for item in ledger.get("sources") or []
                if item.get("ref") == ref and item.get("snapshot_kind") == "full"
            ),
            None,
        )
        if entry is None:
            return None  # 未登记 / 只是摘要快照
        path = self._dir(run_id) / "sources" / f"{ref}.txt"
        try:
            resolved = path.resolve()
            root = (self._dir(run_id) / "sources").resolve()
        except OSError:
            return None
        if root not in resolved.parents or not path.exists():
            return None
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        expected = entry.get("content_sha256")
        if expected and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
            return None  # 内容被改过：不再当作当初那份原文
        return text

    def read_report(self, run_id: str) -> str | None:
        p = self._dir(run_id) / "report.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def mark_orphans(self) -> int:
        """重启时把未结束运行标为失败，保留已有结果与材料。"""
        n = 0
        metas = [
            meta
            for directory in self.root.iterdir()
            if directory.is_dir()
            and (meta := self.get_meta(directory.name)) is not None
        ]
        for meta in metas:
            if meta["status"] in {"running", "preparing"}:
                status = "failed"
                note = "该运行不提供续跑，已提交原文与卡片保留"
                meta.update(status=status, fail_reason="server restart", fail_note=note,
                            finished_at=meta.get("finished_at") or time.time())
                self._write_meta(meta["run_id"], meta)
                n += 1
        return n


class PlanAttemptStore:
    """计划阶段落盘（plans/<id>/ = meta.json + events.jsonl）。

    与 RunStore 平行：/api/brief* 在研究 run 创建前就会调用规划模型，
    失败时过去没有 run_id，llm_call / json_retry 无处可写。这里给每次规划尝试
    一个独立 id，便于排查模型调用故障。不进 Sidebar 历史列表。
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
            "kind": kind,  # plan | revise | brief | brief_revise
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
            meta["plan"] = plan
        self._write_meta(plan_id, meta)
