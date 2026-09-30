"""M6 本地端到端核验：起真实 HTTP 服务 + 只替换外部边界，跑产品入口。

用途：在没有浏览器、也不跑 live 的前提下，验证"网页默认路径"的完整数据面：
确认问题 → 单 Lead 取证 → 短答 → 四状态/材料 → 取消与容量 → 历史一致。

用法::

    uv run python scripts/m6_offline_server.py --data-dir /tmp/m6e2e --port 8877
    uv run python scripts/m6_offline_server.py --data-dir /tmp/m6e2e --port 8877 --check

``--check`` 会在起服后自测一轮（含取消/容量/历史）并打印 JSON 结论。
外部边界（模型回包、检索）用确定性替身；核心循环、落盘、容量与事件全真实。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _doc(raw: str):
    from dra.models import RetrievedDoc

    return RetrievedDoc(
        id="offline",
        title="离线核验来源",
        source_url="https://example.org/offline-fixture",
        snippet="离线固定摘要",
        raw_content=raw,
    )


def _turn(name: str, arguments: dict):
    from dra import llm

    call = llm.ToolCall(id="offline-call", name=name, arguments_raw="", arguments=arguments)
    return llm.ToolTurn(
        content="",
        tool_calls=[call],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
    )


class Fixture:
    """确定性外部边界；记录内核观察到的取消与调用次数，便于自检。"""

    def __init__(self, raw: str):
        self.raw = raw
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.queries: dict[str, str] = {}
        self.cancel_seen: list[bool] = []
        self.cancel_observed_in_kernel: list[bool] = []
        self.model_calls: list[str] = []
        self.release = threading.Event()

    def install(self) -> None:
        from dra import events, llm, simple_research as sr

        def model(messages, **kwargs):
            from dra import events as ev

            rid = ev.get_run_id() or "fixture"
            with self.lock:
                index = self.counts.get(rid, 0)
                self.counts[rid] = index + 1
                self.queries.setdefault(rid, str(messages))
                self.model_calls.append(rid)
                self.cancel_seen.append(llm.cancellation_requested())
            query = self.queries[rid]
            if "取消核验" in query and index == 2:
                # 等到 search/read/save 都已提交之后再等取消：
                # 这样才能验证"取消保留已提交材料"这条契约。
                self.release.wait(timeout=60)
            if not kwargs.get("tools"):
                body = "固定材料说明来源保存为不可变原文 [c1]。"
                return llm.ToolTurn(
                    content=body,
                    tool_calls=[],
                    assistant_message={"role": "assistant", "content": body},
                )
            sequence = [
                ("search_sources", {"query": "离线固定材料"}),
                ("read_source", {"source_ref": "s1"}),
                (
                    "save_evidence",
                    {
                        "cards": [
                            {"claim": "来源保存为不可变原文", "source_ref": "s1", "excerpt_no": 1}
                        ]
                    },
                ),
                (
                    "finish_research",
                    {
                        "draft": "固定材料说明来源保存为不可变原文 [c1]。",
                        "coverage": [{"requirement_id": "goal", "status": "answered"}],
                    },
                ),
            ]
            return _turn(*sequence[min(index, len(sequence) - 1)])

        # 内核在"执行工具前"会查取消；这里包一层，记录它是否真的观察到。
        original_checked = llm.cancellation_requested
        observed = self.cancel_observed_in_kernel

        def counting_checked() -> bool:
            value = original_checked()
            if value:
                observed.append(True)
            return value

        llm.call_tools = model
        llm.cancellation_requested = counting_checked
        sr._retrieve = lambda query, config: [_doc(self.raw)]
        del events  # 只为确认导入可用


def _get(url: str, timeout: float = 10.0):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def _post(url: str, body: dict | None = None, timeout: float = 10.0):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _wait_status(base: str, run_id: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _get(f"{base}/api/runs/{run_id}")["meta"]["status"]
        if status != "running":
            return status
        time.sleep(0.05)
    raise AssertionError(f"{run_id} 未在 {timeout}s 内结束")


def args_data_dir() -> Path:
    return STATE["data_dir"]


def selfcheck(base: str) -> dict:
    out: dict = {}

    # 1) 正常交付
    run_id = _post(f"{base}/api/research", {"query": "离线正常交付核验", "mode": "simple"})[
        "run_id"
    ]
    out["normal_status"] = _wait_status(base, run_id)
    payload = _get(f"{base}/api/runs/{run_id}/simple")
    out["normal"] = {
        "stop": payload["stop_reason"],
        "delivery": payload["delivery_status"],
        "coverage": payload["research_coverage"],
        "cards": len(payload["cards"]),
        "full_sources": [s["ref"] for s in payload["sources"] if s["snapshot_kind"] == "full"],
        "executed_tool_calls": _get(f"{base}/api/runs/{run_id}")["executed_tool_calls"],
    }
    ref = out["normal"]["full_sources"][0]
    body = (
        urllib.request.urlopen(f"{base}/api/runs/{run_id}/sources/{ref}", timeout=5).read().decode()
    )
    out["source_snapshot_ok"] = bool(body.strip())

    # 4) 取消：等待期间名额不释放
    rid4 = _post(f"{base}/api/research", {"query": "取消核验：请等待", "mode": "simple"})["run_id"]
    # 等到材料已提交（第 3 次模型回包会阻塞），再取消
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        detail4 = _get(f"{base}/api/runs/{rid4}")
        if detail4.get("executed_tool_calls", 0) >= 3:
            break
        time.sleep(0.1)
    out["cancel_after_material"] = _get(f"{base}/api/runs/{rid4}").get("executed_tool_calls")
    out["cancel_response"] = _post(f"{base}/api/research/{rid4}/cancel")
    try:
        _post(f"{base}/api/research", {"query": "取消期间的第二个", "mode": "simple"})
        out["second_start_during_cancel"] = "accepted(不应)"
    except urllib.error.HTTPError as exc:
        out["second_start_during_cancel"] = f"HTTP {exc.code}"
    fixture = STATE["fixture"]
    fixture.release.set()
    out["cancelled_status"] = _wait_status(base, rid4)
    p4 = _get(f"{base}/api/runs/{rid4}/simple")
    ledger4 = json.loads((args_data_dir() / "runs" / rid4 / "run.json").read_text(encoding="utf-8"))
    executed4 = [
        a
        for a in ledger4["actions"]
        if a["action"] == "tool_call" and (a.get("payload") or {}).get("ok")
    ]
    blocked4 = [
        a
        for a in ledger4["actions"]
        if a["action"] == "tool_call" and not (a.get("payload") or {}).get("ok")
    ]
    out["cancelled"] = {
        "stop": p4["stop_reason"],
        "delivery": p4["delivery_status"],
        # 已提交材料保留：取消前落盘的全文快照与已执行工具都还在
        "full_sources": [s["ref"] for s in p4["sources"] if s["snapshot_kind"] == "full"],
        "executed_tools_before_cancel": [a["payload"]["tool"] for a in executed4],
        # 取消后不再执行新动作：迟到回包里的工具调用**根本不进入执行**，
        # 因此这里通常为空；若存在，则必须带 cancelled 之类的原因。
        "blocked_after_cancel": [a["payload"]["reason"] for a in blocked4],
        "no_new_actions_after_cancel": all(
            "取消" in str(a["payload"].get("reason")) for a in blocked4
        ),
    }

    # 5) 释放后可以再启动
    rid5 = _post(f"{base}/api/research", {"query": "释放后再次启动", "mode": "simple"})["run_id"]
    out["restart_after_release"] = _wait_status(base, rid5)

    # 6) simple resume 必须被拒
    try:
        _post(f"{base}/api/research/{rid5}/resume")
        out["resume"] = "accepted(不应)"
    except urllib.error.HTTPError as exc:
        out["resume"] = f"HTTP {exc.code}"

    # 7) 历史与刷新一致
    detail = _get(f"{base}/api/runs/{rid5}")
    simple_again = _get(f"{base}/api/runs/{rid5}/simple")
    out["history_consistent"] = (
        detail["meta"]["status"] == "done"
        and simple_again["delivery_status"] == "delivered"
    )
    out["kernel_cancel_observed"] = bool(fixture.cancel_observed_in_kernel)
    events5 = [
        json.loads(line)
        for line in urllib.request.urlopen(f"{base}/api/runs/{rid5}/events", timeout=5)
        .read()
        .decode()
        .splitlines()
        if line.strip()
    ]
    kinds5 = [e["type"] for e in events5]
    out["zero_worker_events"] = not any(
        name in kinds5
        for name in (
            "worker_dispatched",
            "subagent_start",
            "subagent_done",
            "task_batch_dispatched",
        )
    )
    receipts = [e for e in events5 if e["type"] == "subagent_tool_call"]
    out["receipt_attribution"] = {
        "count": len(receipts),
        "all_lead_simple": all(
            e.get("role") == "lead" and e.get("mode") == "simple" for e in receipts
        ),
        "all_have_run_id": all(e.get("run_id") == rid5 for e in receipts),
    }
    return out


STATE: dict = {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--check", action="store_true", help="起服后自测一轮并打印结论")
    args = parser.parse_args()
    if not args.data_dir.is_absolute():
        parser.error("data directory must be absolute")

    args.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DRA_DATA_DIR"] = str(args.data_dir)

    fixture = Fixture(
        "离线核验固定材料：系统以不可变原文保存来源，证据卡引用连续原文。仅用于接线核验。"
    )
    STATE["fixture"] = fixture
    STATE["data_dir"] = args.data_dir
    fixture.install()

    import uvicorn

    from dra import web

    host, port = "127.0.0.1", args.port
    base = f"http://{host}:{port}"
    config = uvicorn.Config(web.app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    if not args.check:
        print(f"离线核验服务：{base}（数据目录 {args.data_dir}）")
        server.run()
        return 0

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    try:
        print(json.dumps(selfcheck(base), ensure_ascii=False, indent=2))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
