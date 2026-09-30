"""直接研究的 URL 正文读取（本轮唯一交付）的验收回归。

合同：
- `read_source` 支持 `url`；`source_ref` 与 `url` **恰好给一个**；
- 用户直接给的链接不必先搜索；只有摘要的来源也能取到正文并走既有不可变快照/存证/引用链；
- 正文来源优先级：已落盘快照 → 本次检索已有 raw_content → 真正的 URL extract；
- **不再**拿 URL 当 query 重新搜索（旧 `_retrieve(url)` 伪抓取已移除）；
- 参数/URL/来源上限/取消/到期在联网前拒收；失败/空正文/超时如实上报，不伪造成功。

替换的边界只有两类：模型调用（脚本回合）与 Tavily extract 的网络出口
（`TavilyClient.session` 的 POST）。真实被测的部分：`read_source` 全部分支、落盘/快照/
已读范围/存证/引用门、预算与账本、结果投影。不联网、不调用真实模型。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dra import llm, simple_research as sr, tools as tools_mod  # noqa: E402
from dra.models import RetrievedDoc  # noqa: E402

from test_budget_wrapup import Script, _FINISH_SEQUENCE, _turn  # noqa: E402

QUERY = "请阅读 https://example.org/report 并总结要点。"
URL = "https://example.org/report"
RAW = "报告要点：第一，独立核查通过；第二，预算内收尾。" * 30


class ExtractOutcome:
    """一次假 extract 的返回或异常，同时记录 SDK 真正发出的请求。"""

    def __init__(self, *, results=None, failed=None, usage=None, request_id=None,
                 error=None, delay_check=None):
        self.results = results
        self.failed = failed
        self.usage = usage
        self.request_id = request_id
        self.error = error
        self.delay_check = delay_check


class FakeExtract:
    """替换 Tavily 客户端的 HTTP 出口（SDK 的 session.post），保留真实 SDK 适配层。"""

    def __init__(self, outcomes: list[ExtractOutcome] | None = None):
        self.outcomes = list(outcomes or [])
        self.requests: list[dict] = []

    def __call__(self, url, data=None, timeout=None, headers=None, **kwargs):  # noqa: A002
        payload = json.loads(data) if isinstance(data, str) else dict(data or {})
        self.requests.append({"url": url, "payload": payload, "timeout": timeout,
                              "headers": headers or {}})
        outcome = self.outcomes.pop(0) if self.outcomes else ExtractOutcome(
            results=[{"url": URL, "raw_content": RAW, "title": "示例报告"}])
        if outcome.delay_check is not None:
            outcome.delay_check()
        if outcome.error is not None:
            raise outcome.error
        body: dict = {}
        if outcome.results is not None:
            body["results"] = outcome.results
        if outcome.failed is not None:
            body["failed_results"] = outcome.failed
        if outcome.usage is not None:
            body["usage"] = outcome.usage
        if outcome.request_id is not None:
            body["request_id"] = outcome.request_id
        return _Response(body)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last_payload(self) -> dict:
        return self.requests[-1]["payload"]


class _Response:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


@pytest.fixture
def extract(monkeypatch):
    """装上假 extract 出口：真实 TavilyClient + 真实 /extract 请求构造，只有网络被替换。"""
    fake = FakeExtract()
    client = tools_mod._get_client.__wrapped__() if hasattr(tools_mod._get_client, "__wrapped__") \
        else None
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(tools_mod, "_client", None)
    real_client = tools_mod._get_client()
    monkeypatch.setattr(real_client, "session", _FakeSession(fake))
    return fake


class _FakeSession:
    def __init__(self, handler: FakeExtract):
        self.handler = handler

    def post(self, url, data=None, timeout=None, headers=None, **kwargs):
        return self.handler(url, data=data, timeout=timeout, headers=headers, **kwargs)


def _doc(*, raw=RAW, url=URL, title="示例报告"):
    return RetrievedDoc(id="d1", source_url=url, title=title,
                        snippet="报告要点摘要", raw_content=raw, published_at="2025-11-13")


class Recorded:
    """一次测试运行的结果 + 真实工具回执 + 运行目录。"""

    def __init__(self, result, script: Script, run_dir: Path, receipts: list[dict]):
        self.result = result
        self.script = script
        self.run_dir = run_dir
        self.receipts = receipts

    def ledger(self) -> dict:
        return json.loads((self.run_dir / "run.json").read_text(encoding="utf-8"))

    def actions(self, action: str) -> list[dict]:
        return [a for a in self.ledger()["actions"] if a["action"] == action]

    def reads(self) -> list[dict]:
        """每次 read_source 回执解析出的 JSON（成功回执才有）。"""
        out: list[dict] = []
        for receipt in self.receipts:
            if receipt.get("tool") != "read_source":
                continue
            try:
                out.append(json.loads(receipt["payload"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def last_read(self) -> dict:
        reads = self.reads()
        assert reads, "没有找到 read_source 回执"
        return reads[-1]

    def receipts_blob(self) -> str:
        """所有真实工具回执的原始文本（拒收原因也在这里，不依赖模型消息）。"""
        return json.dumps(self.receipts, ensure_ascii=False)


def _run(turns, *, config=None, search=None, verify=None, tmp_path: Path | None = None):
    """跑一次直接研究；`search` 默认一调用就失败。

    每次运行用独立运行目录：`read_source` 的落盘/快照/账本都是 per-run 状态，
    测试之间共用默认目录会互相看到对方的来源。
    """
    script = Script(turns)
    receipts: list[dict] = []
    original_execute = sr.ToolContext.execute

    def recording_execute(self, name, args):
        payload, ok = original_execute(self, name, args)
        receipts.append({"tool": name, "ok": ok, "args": args, "payload": payload})
        return payload, ok

    def search_fn(query):
        if search is None:
            raise AssertionError(f"本轮不应发生搜索，但收到 query={query!r}")
        return search(query)

    run_dir = (tmp_path or Path(tempfile.mkdtemp(prefix="dra-url-read-"))) / "run"
    sr.ToolContext.execute = recording_execute
    try:
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=config or sr.SimpleConfig(max_llm_calls=8, max_tool_calls=8),
            run_dir=run_dir,
            model_call=script,
            search_fn=search_fn,
            ref_resolver=script,
        )
    finally:
        sr.ToolContext.execute = original_execute
    return Recorded(result, script, run_dir, receipts)


# ---------------------------------------------------------------------------
# A. URL 直读 → 存证 → 带引用短答 → 核查 → 结果落盘（全程零搜索）
# ---------------------------------------------------------------------------


def test_url_direct_read_full_chain_with_zero_searches(extract, tmp_path):
    turns = [
        _turn(("read_source", {"url": URL})),
        _turn((
            "save_evidence",
            {"cards": [{"claim": "报告给出两条要点", "source_ref": "s1", "excerpt_no": 1}]},
        )),
        _turn(("finish_research", {"draft": "报告给出两条要点 [c1]。"})),
    ]
    run = _run(turns, tmp_path=tmp_path)   # search=None → 一旦搜索立即失败
    result = run.result

    assert result.stop_reason.startswith("finished"), result.stop_reason
    assert result.delivery_status == "delivered"
    assert extract.calls == 1, "URL 直读只应发一次 extract"
    assert [c.card_id for c in result.cards] == ["c1"]
    # 直读 URL 只产生一条正文快照，不造"仿搜索摘要"记录
    assert [s.snapshot_kind for s in result.sources] == ["full"]
    assert len(result.draft) > 0


def test_url_read_receipt_exposes_ref_url_excerpt_and_next_cursor(extract, tmp_path):
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    payload = run.last_read()
    assert payload["source_ref"].startswith("s")
    assert payload["url"] == URL
    assert payload["title"] == "示例报告"
    assert len(payload["excerpts"]) == 1
    assert payload["excerpts"][0]["excerpt_no"] > 0
    assert payload["excerpts"][0]["location"] == payload["shown_range"]
    assert payload["total_chars"] > 0
    # 真实回执里的续读信息：到底了就是到底了，没到底必须给 next_cursor
    assert payload["has_more"] is (payload["next_cursor"] is not None)


def test_extract_request_shape_is_minimal(extract, tmp_path):
    _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    payload = extract.last_payload
    assert payload["urls"] == [URL]
    assert payload["format"] == "markdown"
    assert payload["extract_depth"] == "basic"
    assert payload["include_usage"] is True
    assert "query" not in payload and "chunks_per_source" not in payload
    assert "include_images" not in payload and "include_favicon" not in payload
    assert extract.requests[0]["url"].endswith("/extract")
    assert 0 < extract.requests[0]["timeout"] <= tools_mod.EXTRACT_TIMEOUT_S


def test_extract_attempt_is_recorded_with_usage_metadata(extract, tmp_path):
    extract.outcomes = [ExtractOutcome(
        results=[{"url": URL, "raw_content": RAW, "title": "示例报告"}],
        usage={"credits": 1}, request_id="req-123",
    )]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    finishes = [a for a in run.actions("url_extract") if a["action_id"].endswith(":done")]
    assert len(finishes) == 1
    payload = finishes[0]["payload"]
    assert payload["ok"] is True and payload["url"] == URL
    assert payload["credits"] == 1 and payload["request_id"] == "req-123"
    assert payload["chars"] == len(RAW)
    assert payload["usage_unknown"] is False


# ---------------------------------------------------------------------------
# B. 只有摘要的 source_ref → 真正的 extract（不再拿 URL 当 query）
# ---------------------------------------------------------------------------


def test_summary_only_ref_fetches_body_via_extract_not_search(extract, tmp_path):
    searched: list[str] = []

    def search(query):
        searched.append(query)
        return [_doc(raw="")]        # 只有摘要，没有正文

    turns = [
        _turn(("search_sources", {"query": "示例报告 要点"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    run = _run(turns, search=search, tmp_path=tmp_path)
    assert searched == ["示例报告 要点"], "URL 不应作为检索词再次出现"
    assert extract.calls == 1, "只有摘要时必须真的按 URL 提取正文"
    assert extract.last_payload["urls"] == [URL]
    payload = run.last_read()
    assert payload["source_ref"] != "s1"          # 正文快照 ref
    assert payload["summary_ref"] == "s1"
    assert payload["total_chars"] == len(RAW)


def test_search_result_body_is_used_without_any_extract(extract, tmp_path):
    searched: list[str] = []
    _run(
        [_turn(("search_sources", {"query": "示例报告 要点"})),
         _turn(("read_source", {"source_ref": "s1"}))],
        search=lambda q: (searched.append(q), [_doc()])[1],   # 检索已带正文
        tmp_path=tmp_path,
    )
    assert searched == ["示例报告 要点"]
    assert extract.calls == 0, "检索已给正文时不得再发 extract"


# ---------------------------------------------------------------------------
# C. 重复读取与续读
# ---------------------------------------------------------------------------


def test_same_url_read_twice_extracts_once(extract, tmp_path):
    turns = [
        _turn(("read_source", {"url": URL})),
        _turn(("read_source", {"url": URL})),
        _turn(("finish_research", {"draft": "两次读取只取一次正文。"})),
    ]
    result = _run(turns, tmp_path=tmp_path).result
    assert extract.calls == 1
    assert [s.snapshot_kind for s in result.sources] == ["full"], "只应有一条正文快照"


def test_read_by_ref_after_url_read_does_not_extract_again(extract, tmp_path):
    """先按 URL 读，再按回执给的 source_ref 续读：同一份快照，不再抓取。"""
    turns = [
        _turn(("read_source", {"url": URL})),
        _turn(("read_source", {"source_ref": "s1", "cursor": 500})),
        _turn(("finish_research", {"draft": "续读同一快照 [c1]。"})),
    ]
    run = _run(turns, tmp_path=tmp_path)
    assert extract.calls == 1
    payload = run.last_read()
    assert payload["shown_range"].startswith("chars=500-") or payload["requested_range"]


def test_find_and_continuation_still_accurate_after_url_read(extract, tmp_path):
    turns = [
        _turn(("read_source", {"url": URL})),
        _turn(("read_source", {"source_ref": "s1", "find": "第二"})),
    ]
    run = _run(turns, tmp_path=tmp_path)
    assert extract.calls == 1
    payload = run.last_read()
    assert payload["view"] == "matches"
    assert payload["occurrences"] >= 1
    assert payload["matches"][0]["line"] >= 1
    assert "第二" in payload["excerpts"][0]["quote"]


# ---------------------------------------------------------------------------
# D. 联网前拒收 / 失败如实
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("args,reason", [
    ({}, "恰好给一个"),
    ({"source_ref": "s1", "url": URL}, "恰好给一个"),
    ({"url": "ftp://example.org/a"}, "只支持 http/https"),
    ({"url": "https://user:pw@example.org/a"}, "不能带账号密码"),
    ({"url": "https://127.0.0.1/a"}, "本机/内部地址"),
    ({"url": "https://intranet/a"}, "缺少有效主机名"),
    ({"url": "https://example.org/a", "find": "x", "range": "0-10"}, "只能给一个"),
    ({"url": URL, "find": " "}, "find 不能为空"),
])
def test_bad_arguments_rejected_before_any_extract(extract, tmp_path, args, reason):
    run = _run([_turn(("read_source", args))], tmp_path=tmp_path)
    assert extract.calls == 0, "参数/URL 不合法时不得联网"
    assert run.result.cards == []
    blob = run.receipts_blob()
    assert reason in blob


def test_source_reads_are_unlimited_and_still_extract(extract, tmp_path):
    """来源数量不再有上限（2026-09-16）：读过一份之后，直接给 URL 仍然正常抓取。

    回归：此前 `max_sources=8` 是运行级共享池，被拒的读会误报成"请用已有来源完成回答"，
    把 harness 的资源上限说成模型该收手。见 STATUS §4.1.16。
    """
    config = sr.SimpleConfig(max_llm_calls=8, max_tool_calls=8)
    run = _run(
        [_turn(("search_sources", {"query": "占一个来源额度"})),
         _turn(("read_source", {"source_ref": "s1"})),      # 检索带回正文 → 落盘占额度
         _turn(("read_source", {"url": "https://example.org/other"}))],
        config=config, search=lambda q: [_doc()], tmp_path=tmp_path,
    )
    assert extract.calls == 1, "没有来源数量上限之后，第二个 URL 必须照常抓取"
    assert "已达上限" not in run.receipts_blob()


def test_cancelled_run_does_not_extract(extract, tmp_path):
    """取消置位后：一个请求都不发，也不伪造读取成功。"""
    import threading

    stop = threading.Event()
    stop.set()
    with llm.execution_scope(cancel_event=stop):
        run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert extract.calls == 0, "取消后不得发请求"
    assert run.result.stop_reason == "cancelled"
    assert not [s for s in run.result.sources if s.snapshot_kind == "full"]


def test_expired_deadline_does_not_extract(extract, monkeypatch, tmp_path):
    """墙钟已到：不发请求，也不产生正文快照。"""
    import time

    monkeypatch.setattr(sr.Budget, "expired", lambda self: True)
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert extract.calls == 0, "墙钟已到不得发请求"
    assert run.result.stop_reason == "deadline"
    assert not [s for s in run.result.sources if s.snapshot_kind == "full"]
    assert time.time() > 0


def test_extract_failure_reported_and_no_fake_body(extract, tmp_path):
    extract.outcomes = [ExtractOutcome(error=RuntimeError("boom: api_key=should-not-appear"))]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert run.result.cards == []
    blob = run.receipts_blob()
    assert "取正文失败" in blob
    # 只给稳定类别，不回传任意异常全文（可能带密钥/代理地址）
    assert "should-not-appear" not in blob
    assert "api_key" not in blob
    assert "未知错误" in blob
    # 失败也要留下这次尝试的账
    finishes = [a for a in run.actions("url_extract") if a["action_id"].endswith(":done")]
    assert finishes and finishes[-1]["payload"]["ok"] is False


def test_empty_body_is_failure_not_success(extract, tmp_path):
    extract.outcomes = [ExtractOutcome(results=[{"url": URL, "raw_content": "   "}])]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert run.result.cards == []
    assert "空正文" in run.receipts_blob()
    assert not [s for s in run.result.sources if s.snapshot_kind == "full"]
    finishes = [a for a in run.actions("url_extract") if a["action_id"].endswith(":done")]
    assert finishes and finishes[-1]["payload"]["ok"] is False, "空正文不得记成成功"


def test_timeout_reported_as_actionable_reason(extract, tmp_path):
    extract.outcomes = [ExtractOutcome(error=TimeoutError(30))]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert "取正文超时" in run.receipts_blob()


def test_bad_content_type_is_reported(extract, tmp_path):
    extract.outcomes = [ExtractOutcome(results=[{"url": URL, "raw_content": {"nested": 1}}])]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert "类型异常" in run.receipts_blob()


def test_failed_results_are_surfaced(extract, tmp_path):
    extract.outcomes = [ExtractOutcome(results=[], failed=[{"url": URL, "error": "403 Forbidden"}])]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert "403 Forbidden" in run.receipts_blob()


def test_cancel_after_response_does_not_fake_success(extract, tmp_path):
    """响应回来后已取消：正文不得落盘为可引用快照，风险由调用点如实反映。"""
    import threading

    stop = threading.Event()
    extract.outcomes = [ExtractOutcome(
        results=[{"url": URL, "raw_content": RAW, "title": "示例报告"}],
        delay_check=stop.set,          # 请求发出后、响应返回前取消
    )]
    with llm.execution_scope(cancel_event=stop):
        run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    # 取消后不再把这批材料当作正常读取结果继续研究
    assert run.result.stop_reason == "cancelled", run.result.stop_reason
    assert run.result.cards == []
    finishes = [a for a in run.actions("url_extract") if a["action_id"].endswith(":done")]
    assert finishes and finishes[-1]["payload"]["ok"] is False
    assert finishes[-1]["payload"]["error"].startswith("已取消")
    # 未知用量显式标记；已知元信息（服务商给了才写）不被清空
    assert finishes[-1]["payload"]["usage_unknown"] is True
    assert finishes[-1]["payload"]["chars"] == len(RAW)


def test_persistence_failure_is_not_swallowed(extract, monkeypatch, tmp_path):
    def boom(self, doc):
        raise OSError("disk full")

    monkeypatch.setattr(sr.RunStore, "write_source", boom)
    # 持久化失败必须向上抛：LLMPersistenceError（RuntimeError 子类）不会被当成工具失败吞掉
    with pytest.raises(llm.LLMPersistenceError, match="disk full"):
        _run(
            [_turn(("read_source", {"url": URL})),
             _turn(("finish_research", {"draft": "不可能走到这一步。"}))],
            tmp_path=tmp_path,
        )


def test_malformed_body_keeps_known_usage_metadata(extract, tmp_path):
    """正文类型异常但服务商已计费：credits/request_id 要保留，不能记成未知。"""
    extract.outcomes = [ExtractOutcome(
        results=[{"url": URL, "raw_content": {}}],
        usage={"credits": 1}, request_id="offline-malformed-response",
    )]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert "类型异常" in run.receipts_blob()
    finishes = [a for a in run.actions("url_extract") if a["action_id"].endswith(":done")]
    assert finishes
    payload = finishes[-1]["payload"]
    assert payload["ok"] is False
    assert payload["credits"] == 1
    assert payload["request_id"] == "offline-malformed-response"
    assert payload["usage_unknown"] is False, "已知费用不得被写成未知"


def test_failed_results_keeps_known_usage_metadata(extract, tmp_path):
    """failed_results 分支同样保留已知元信息（与类型异常共用一个挂载方式）。"""
    extract.outcomes = [ExtractOutcome(
        results=[], failed=[{"url": URL, "error": "403 Forbidden"}],
        usage={"credits": 2}, request_id="offline-failed-results",
    )]
    run = _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    finishes = [a for a in run.actions("url_extract") if a["action_id"].endswith(":done")]
    payload = finishes[-1]["payload"]
    assert payload["credits"] == 2
    assert payload["request_id"] == "offline-failed-results"
    assert payload["usage_unknown"] is False


def _patched_store_commit(monkeypatch, *, fail_after_extract: bool, extract: FakeExtract):
    """把账本落盘打成"只在 URL 提取路径上失败"：初始化等其它 commit 照常。"""
    import inspect

    real_commit = sr.RunStore.commit

    def flaky(self):
        in_extract = any(
            frame.function == "_extract_doc" for frame in inspect.stack()
        )
        if in_extract and (fail_after_extract and extract.calls > 0
                           or not fail_after_extract and extract.calls == 0):
            raise OSError("ledger unavailable")
        return real_commit(self)

    monkeypatch.setattr(sr.RunStore, "commit", flaky)


def test_pre_request_journal_failure_propagates_without_extract(extract, monkeypatch, tmp_path):
    """请求**前**的账本落盘失败：向上抛（不降级成工具回执），且一个 extract 都不发。"""
    _patched_store_commit(monkeypatch, fail_after_extract=False, extract=extract)
    with pytest.raises(llm.LLMPersistenceError, match="ledger unavailable"):
        _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert extract.calls == 0, "尝试都没落盘，不得发请求"


def test_post_request_journal_failure_propagates_after_one_extract(
    extract, monkeypatch, tmp_path
):
    """请求**后**的账本落盘失败：向上抛，且恰好发过一次 extract。"""
    _patched_store_commit(monkeypatch, fail_after_extract=True, extract=extract)
    with pytest.raises(llm.LLMPersistenceError, match="ledger unavailable"):
        _run([_turn(("read_source", {"url": URL}))], tmp_path=tmp_path)
    assert extract.calls == 1, "请求已经发出，恰好一次"


def test_unexpected_fetch_exception_becomes_tool_failure_not_run_crash(
    extract, monkeypatch, tmp_path
):
    """抓取边界的意外异常（非 UrlFetchError）：工具如实拒收，整轮研究不被炸成 failed。"""

    def boom(url, *, timeout_s=None, deadline=None, include_usage=True):  # noqa: ARG001
        raise RuntimeError("边界实现抛了别的异常")

    monkeypatch.setattr(sr, "fetch_url_content", boom)
    run = _run(
        [_turn(("read_source", {"url": URL})),
         _turn(("finish_research", {"draft": "没能读取该链接。"}))],
        tmp_path=tmp_path,
    )
    assert run.result.stop_reason == "finished", run.result.stop_reason
    assert run.result.cards == []
    assert "意外错误" in run.receipts_blob()


# ---------------------------------------------------------------------------
# 工具/提示合同
# ---------------------------------------------------------------------------


def test_schema_documents_url_and_optional_ref():
    from dra.simple_research import TOOL_SCHEMAS

    read = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "read_source")
    props = read["function"]["parameters"]["properties"]
    assert "url" in props and "source_ref" in props
    assert read["function"]["parameters"]["required"] == []
    assert "恰好给一个" in read["function"]["description"]


def test_system_prompt_prefers_direct_url_read():
    from dra.simple_research import _LEAD_SYSTEM

    assert "read_source(url=...)" in _LEAD_SYSTEM
    assert "不要**先用 search_sources 搜这个链接" in _LEAD_SYSTEM
    assert "不保证覆盖整站" in _LEAD_SYSTEM


def test_existing_search_flow_untouched(extract, tmp_path):
    """回归：原来的"搜索 → 按 ref 精读 → 存证 → 收尾"路径行为不变。"""
    result = _run(_FINISH_SEQUENCE, search=lambda q: [_doc()], tmp_path=tmp_path).result
    assert result.stop_reason == "finished"
    assert result.delivery_status == "delivered"
    assert extract.calls == 0
    assert [c.card_id for c in result.cards] == ["c1"]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
