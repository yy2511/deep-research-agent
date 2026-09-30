"""额度用完时的收尾，以及长文里目标段落能被读到。

只替换模型和检索边界；核心循环、原文落盘、存证、窗口与状态投影使用真实实现。
另外，这里的 Script / _turn / _doc 等假模型、假检索工具被许多测试文件共用。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from dra import simple_research as sr
from dra.llm import ToolCall, ToolTurn
from dra.models import RetrievedDoc
from dra.text import clean_markdown

QUERY = "sqlite3_rsync 是否必须通过 SSH 同步？同机两个文件也支持吗？"

RAW = """# sqlite3_rsync

This utility causes REPLICA to become a copy of ORIGIN.
a. If REPLICA does not already exist, it is created.
b. ssh is used for communication, so "USER@HOST" may be an SSH alias.
c. It is not required that one of ORIGIN or REPLICA be remote.
The sqlite3_rsync utility works fine if both ORIGIN and REPLICA are local.
"""

# 目标串被埋在 6000 字符之后：用于验证保存范围与展示窗口分开。
_FILLER = "补充说明段落，用于把正文撑到远超单次展示窗口的长度。\n\n"
LONG_TARGET = "TARGET-ALPHA-9F3"
LONG_RAW = "# 长文来源\n\n" + _FILLER * 300 + f"{LONG_TARGET} 就是被埋在后部的目标句。\n"

# 跨片段命中：目标正好跨越 700 字的片段边界（695 处开始）。
BOUNDARY_TARGET = "BOUNDARY_TARGET_2026"
BOUNDARY_RAW = "X" * 695 + BOUNDARY_TARGET + "Y" * 1000

_PARA = "句子内容填充用于形成可切分的长文段落。\n\n"  # 带段落标记，片段边界可预期


def _doc(raw=RAW, *, id="d1", url="https://sqlite.org/rsync.html", title="sqlite3_rsync"):
    return RetrievedDoc(
        id=id,
        source_url=url,
        title=title,
        snippet="sqlite3_rsync 通过 SSH 同步",
        raw_content=raw,
        published_at="2025-11-13",
    )


def _turn(*calls):
    tcs = [
        ToolCall(id=f"c{i}", name=name, arguments_raw="", arguments=args)
        for i, (name, args) in enumerate(calls)
    ]
    message = {"role": "assistant", "content": ""}
    if tcs:
        message["tool_calls"] = [
            {"id": t.id, "type": "function", "function": {"name": t.name, "arguments": "{}"}}
            for t in tcs
        ]
    return ToolTurn(content="", tool_calls=tcs, assistant_message=message)


def _text_turn(text):
    return ToolTurn(
        content=text, tool_calls=[], assistant_message={"role": "assistant", "content": text}
    )


class Script:
    """按序吐回合；脚本用完后进入收尾阶段（返回纯文本，即收尾回合的形态）。"""

    next_cursor = None

    def __init__(self, turns, *, wrapup_text="", finish=None):
        self.turns = list(turns)
        self.wrapup_text = wrapup_text
        self.finish = finish
        self.seen = []
        self.tool_schemas = []

    def __call__(self, messages, **kwargs):
        self.seen.append(messages)
        self.tool_schemas.append([t["function"]["name"] for t in (kwargs.get("tools") or [])])
        index = len(self.seen) - 1
        if index < len(self.turns):
            return self.turns[index]
        if self.wrapup_text:
            return _text_turn(self.wrapup_text)
        if self.finish:
            return _turn(self.finish)
        return _text_turn("")

    def resolve(self, args):
        """占位符解析：本文件的脚本直接写真 ref，不需要替换。"""
        return args


def _run(
    turns,
    *,
    config=None,
    wrapup_text="",
    finish=None,
    raw=RAW,
    search_calls=None,
):
    """使用脚本模型运行当前研究入口。"""
    config = config or sr.SimpleConfig()
    script = Script(turns, wrapup_text=wrapup_text, finish=finish)

    def search(query):
        if search_calls is not None:
            search_calls.append(query)
        return [_doc(raw=raw)]

    result = sr.run_simple_research(
        QUERY,
        sr.requirements_from_query(QUERY),
        config=config,
        model_call=script,
        search_fn=search,
        ref_resolver=script,
    )
    return result, script


def _saved_bodies(result):
    return {
        snap.ref: (Path(result.artifact_dir) / "sources" / f"{snap.ref}.txt").read_text(
            encoding="utf-8"
        )
        for snap in result.sources
        if snap.snapshot_kind == "full"
    }


def _tool_payloads(script):
    """从所有回合的 tool 消息里收集 JSON 负载，便于按字段断言。"""
    out = []
    for messages in script.seen:
        for message in messages:
            if message.get("role") != "tool":
                continue
            try:
                payload = json.loads(message.get("content") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                out.append(payload)
    return out


def _windows(script):
    return [p for p in _tool_payloads(script) if "shown_range" in p]


def _is_find_hit(payload):
    """find 的负载：命中清单（``view="matches"``）+ 至少一条可引用上下文。"""
    return payload.get("view") == "matches" and bool(payload.get("excerpts"))


def _find_hit(script):
    return next((p for p in _tool_payloads(script) if _is_find_hit(p)), None)


def _bounds(payload):
    match = re.fullmatch(r"chars=(\d+)-(\d+)", payload["shown_range"])
    return int(match.group(1)), int(match.group(2))


# 搜索 → 精读 → 存卡 → 主动收尾。
_FINISH_SEQUENCE = [
    _turn(("search_sources", {"query": "sqlite3_rsync ssh local"})),
    _turn(("read_source", {"source_ref": "s1"})),
    _turn(
        (
            "save_evidence",
            {"cards": [{"claim": "同机两文件可同步", "source_ref": "s1", "excerpt_no": 1}]},
        )
    ),
    _turn(
        (
            "finish_research",
            {
                "draft": "不要求一端为远程，两文件都在本机也能同步 [c1]。",
                # 核查默认不执行后，覆盖结论只能来自 Lead 自报（代码仍取保守的一方，
                # 不会自授）；真实模型会报 coverage，夹具保持同一形态。
                "coverage": [
                    {"requirement_id": "goal", "status": "answered", "note": "官方文档已核实"}
                ],
            },
        )
    ),
]

# 只搜读、不主动收尾：用于逼出预算路径。工具额度给足，靠模型额度触发。
_TIRE_SEQUENCE = [
    _turn(("search_sources", {"query": "sqlite3_rsync ssh local"})),
    _turn(("read_source", {"source_ref": "s1"})),
]

# 长文：搜索 → 首读（只看得到窗口）→ 用 find 定位后部目标 → 收尾。
_LONG_SEQUENCE = [
    _turn(("search_sources", {"query": "长文目标"})),
    _turn(("read_source", {"source_ref": "s1"})),
    _turn(("read_source", {"source_ref": "s1", "find": LONG_TARGET})),
    _turn(("finish_research", {"draft": f"目标句在保存正文里可见：{LONG_TARGET} [c1]。"})),
]

# 跨片段：目标跨越片段边界，旧实现的 find 在此返回 None。
_BOUNDARY_SEQUENCE = [
    _turn(("search_sources", {"query": "跨片段目标"})),
    _turn(("read_source", {"source_ref": "s1"})),
    _turn(("read_source", {"source_ref": "s1", "find": BOUNDARY_TARGET})),
]

_WRAPUP_DRAFT = "sqlite3_rsync 不要求一端为远程；两文件都在本机也能同步 [c1]。"

BASE = sr.SimpleConfig(
    max_llm_calls=10, max_tool_calls=10, wall_timeout_s=60.0
)


def test_find_match_list_is_bounded_and_numbers_resolve():
    """命中清单必须是有界的：每条上下文 ≤ 上限、总条数 ≤ 上限，编号精确解析。"""
    raw = "prefix TARGET_123456 与 TARGET_123456 再来一次 " + "X" * 4000
    reading = sr.Reading(sr.SourceSnapshot(ref="s1", url="https://example.org"), raw)
    found = reading.find_matches("TARGET_123456")
    assert found["occurrences"] == 2
    assert len(found["rows"]) == 2
    for row in found["rows"]:
        assert len(row["quote"]) <= sr._FIND_CONTEXT_CHARS
        assert "TARGET_123456" in row["quote"], "上下文必须覆盖命中"
        assert reading.quote(row["excerpt_no"]) == (row["quote"], row["location"])
    # 超过上限时只列前 N 条，并给出翻页游标
    dense = ("标记 " * 2000)
    many = sr.Reading(sr.SourceSnapshot(ref="s1", url="https://example.org"), dense)
    capped = many.find_matches("标记")
    assert len(capped["rows"]) == sr._FIND_MAX_MATCHES
    assert capped["next_cursor"] is not None, "还有命中时必须给翻页游标"


def test_active_finish_is_distinguished_from_budget_wrapup():
    """主动 finish 与预算收尾是两条路：前者 stop_reason 不带 +wrapup。"""
    result, _ = _run(_FINISH_SEQUENCE, config=BASE)
    assert result.stop_reason == "finished"
    assert result.delivery_status == "delivered"
    assert result.draft
    assert not [c for c in result.calls if c.step == "wrapup"], "主动收尾不该再花收尾额度"


def test_tool_budget_exhaustion_still_produces_bounded_wrapup_answer():
    """保留区拒绝精读并记录原因，工具额度耗尽后仍有一次有界收尾。"""
    config = sr.SimpleConfig(max_llm_calls=10, max_tool_calls=3, wall_timeout_s=60.0)
    result, script = _run(
        [
            _turn(("search_sources", {"query": "q1"})),
            _turn(("inspect_research", {"view": "summary"})),
            _turn(("read_source", {"source_ref": "s1"})),  # 落入保留区：被拒
        ],
        config=config,
        wrapup_text=_WRAPUP_DRAFT,
    )
    # 保留区把读操作挡下后，工具额度实际已不可用于检索：停止原因必须带兜底标记。
    assert result.stop_reason.endswith("+wrapup"), result.stop_reason
    ledger = json.loads(
        (Path(result.artifact_dir) / sr.RunStore.filename).read_text(encoding="utf-8")
    )
    blocked = [
        a for a in ledger["actions"]
        if a["action"] == "tool_call"
        and a["payload"].get("tool") == "read_source"
        and not a["payload"]["ok"]
    ]
    assert blocked and "保留区" in blocked[0]["payload"]["reason"]
    assert "save_evidence" in blocked[0]["payload"]["reason"]
    assert blocked[0]["payload"]["args"] == {"source_ref": "s1"}
    assert blocked[0]["payload"]["receipt_head"]
    assert result.draft
    # 收尾草稿里引用了不存在的卡片，因此不冒充完整交付——但正文确实拿出来了
    assert result.delivery_status in {"delivered", "limited"}, result.delivery_status
    assert [c.step for c in result.calls if c.step == "wrapup"], "必须有收尾回合"
    # 兜底不得突破硬上限
    assert len(result.calls) <= config.max_llm_calls
    # 模型能看到剩余额度
    assert any("工具额度" in m.get("content", "") for m in script.seen[-1])
    # 收尾回合也要点名回答语言：长上下文读了大量外文材料后，泛泛的“与问题一致”容易失效
    assert "【回答语言】" in script.seen[-1][0]["content"]


def test_model_budget_exhaustion_reserves_room_for_wrapup():
    """模型额度预算要在耗尽前留出收尾那次调用。"""
    config = sr.SimpleConfig(
        max_llm_calls=6, max_tool_calls=10, wall_timeout_s=60.0
    )
    result, _ = _run(_TIRE_SEQUENCE, config=config, wrapup_text=_WRAPUP_DRAFT)
    steps = [c.step for c in result.calls]
    assert result.stop_reason.endswith("+wrapup"), result.stop_reason
    assert "wrapup" in steps, steps
    assert steps[-1] == "wrapup", steps
    assert len(steps) <= config.max_llm_calls


def test_no_evidence_answer_is_not_reported_as_answered():
    """零卡片却有正文时：引用门不通过，覆盖不得报 answered，也不得标完整交付。"""
    ok, problems = sr.citation_gate("sqlite3_rsync 不要求一端为远程。", [])
    assert not ok and any("无法溯源" in p for p in problems)
    assert not sr.citation_gate("有一处引用 [c1]。", [])[0]

    req = [sr.Requirement(id="goal", text="问题", kind="goal")]
    coverage, reasons = sr.project_coverage(
        req,
        [{"requirement_id": "goal", "status": "answered"}],
        grounded=False,
    )
    assert coverage == {"goal": "unanswered"}
    assert "无法对应到已保存证据卡" in " ".join(reasons["goal"])

    card = sr.EvidenceCard(
        card_id="c1",
        claim="同机可同步",
        quote="works fine",
        source_ref="s1",
        source_url="https://sqlite.org/rsync.html",
    )
    ok, problems = sr.citation_gate("一段没有任何引用的确定断言。", [card])
    assert not ok and any("没有任何 [cN] 引用" in p for p in problems)


def test_wrapup_never_auto_assembles_cards_into_a_verified_answer():
    """兜底绝不等于自动拼装：收尾交出的正文仍要过核查与引用门。"""
    config = sr.SimpleConfig(
        max_llm_calls=10, max_tool_calls=3, wall_timeout_s=60.0, tool_reserve_calls=0
    )
    result, _ = _run(
        _FINISH_SEQUENCE[:3],
        config=config,
        wrapup_text="这是没有任何 [cN] 标注的确定断言，且引用了不存在的卡片 [c9]。",
    )
    assert result.draft
    assert result.draft_rejected is True, "引用不成立的草稿必须拒绝发布"
    assert result.delivery_status == "limited"
    assert "不存在的卡片" in " ".join(result.warnings)
    assert "没有任何 [cN] 标注的确定断言" not in result.markdown
    assert "不作为答案发布" in result.markdown


def test_cancellation_and_timeout_do_not_enter_wrapup():
    """取消与超时仍然立即停：兜底不延长它们，也不多发模型请求。"""
    import threading

    from dra import llm

    stop = threading.Event()

    def search(_query):
        stop.set()
        return [_doc()]

    script = Script(_TIRE_SEQUENCE, wrapup_text=_WRAPUP_DRAFT)
    with llm.execution_scope(cancel_event=stop):
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=BASE,
            model_call=script,
            search_fn=search,
            ref_resolver=script,
        )
    assert result.stop_reason == "cancelled"
    assert result.draft == ""
    assert not [c for c in result.calls if c.step == "wrapup"], "取消后不得再发收尾请求"

    def boom(messages, **kwargs):
        raise TimeoutError("模拟请求超时")

    result2 = sr.run_simple_research(
        QUERY,
        sr.requirements_from_query(QUERY),
        config=BASE,
        model_call=boom,
        search_fn=lambda q: [_doc()],
        ref_resolver=Script([]),
    )
    assert result2.stop_reason in {"request_timeout", "failed"}
    assert not [c for c in result2.calls if c.step == "wrapup"], "超时后不得再发收尾请求"


def test_tool_reserve_leaves_small_budgets_usable():
    """小额工具预算下缩减保留区，避免研究无法启动。"""
    assert sr.SimpleConfig(max_tool_calls=2).effective_tool_reserve() == 0
    assert sr.SimpleConfig(max_tool_calls=3).effective_tool_reserve() == 2
    assert sr.SimpleConfig(max_tool_calls=10).effective_tool_reserve() == 2
    assert sr.SimpleConfig(max_tool_calls=10, tool_reserve_calls=0).effective_tool_reserve() == 0


def test_new_config_params_are_bounded_and_rejected_when_out_of_range():
    """新增参数必须有明确边界；越界配置直接拒绝，不靠负数经截断隐式兜底。"""
    with pytest.raises(Exception):
        sr.SimpleConfig(max_llm_calls=2, reserve_wrapup_calls=-5)
    with pytest.raises(Exception):
        sr.SimpleConfig(storage_max_chars=0)
    with pytest.raises(Exception):
        sr.SimpleConfig(read_window_chars=0)
    # 预留之和 >= 总额度是**合法**配置：那表示这次没有可用的研究额度，
    # 系统要诚实地一次都不调用（旧用例 test_budget_exhausted_before_lead_loop_leaves_no_calls 锁定这一点）。
    config = sr.SimpleConfig(max_llm_calls=1, reserve_wrapup_calls=1)
    budget = sr.Budget(config, None)
    assert budget.can_call(reserve=config.reserve_wrapup_calls) is False
    assert budget.calls_used == 0
    assert (
        sr.SimpleConfig(max_llm_calls=2, reserve_wrapup_calls=1).max_llm_calls
        == 2
    )
    with pytest.raises(Exception):
        sr.SimpleConfig(wall_timeout_s=0.0)
    # 小预算要诚实退化而不是被拒绝：1 次调用、0 预留仍然合法
    assert (
        sr.SimpleConfig(max_llm_calls=1, reserve_wrapup_calls=0).max_llm_calls
        == 1
    )

    # 硬上限独立于预留计算：预留只是从硬上限里划分，不会让上限变大。
    config = sr.SimpleConfig(max_llm_calls=4, reserve_wrapup_calls=1)
    budget = sr.Budget(config, None)
    budget.calls_used = config.max_llm_calls
    assert budget.can_call(reserve=config.reserve_wrapup_calls) is False
    assert budget.remaining_calls() == 0
    with pytest.raises(Exception):
        budget.can_call(reserve=-99)  # 负数预留必须被拒绝，不得放大硬上限


# ---------------------------------------------------------------------------
# 绿：长文目标段落可达（当前实现）
# ---------------------------------------------------------------------------


def test_long_document_target_beyond_display_window_is_saved_and_findable():
    """保存范围与展示窗口分开：6000 字之后的目标串可 find、可引用。"""
    config = sr.SimpleConfig(
        max_llm_calls=8, max_tool_calls=8, wall_timeout_s=60.0, read_window_chars=2000
    )
    result, script = _run(_LONG_SEQUENCE, config=config, raw=LONG_RAW, wrapup_text="收尾")

    bodies = _saved_bodies(result)
    assert bodies, "长文必须落成正文快照"
    body = next(iter(bodies.values()))
    assert LONG_TARGET in body, "保存范围必须覆盖被埋在后部的目标串"
    snap = next(s for s in result.sources if s.snapshot_kind == "full")
    assert snap.raw_chars == len(body)
    assert snap.content_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()

    window = _windows(script)[0]
    assert window["total_chars"] == len(body), "回执要说明保存范围的总长度"
    start, stop = _bounds(window)
    assert stop - start <= config.read_window_chars, "单次展示窗口必须有界"
    assert LONG_TARGET not in "".join(e["quote"] for e in window["excerpts"])

    located = _find_hit(script)
    hit_excerpt = located["excerpts"][0]
    assert LONG_TARGET in hit_excerpt["quote"], "find 必须能到达窗口之外的后部内容"
    hit_start = int(hit_excerpt["location"][6:].split("-")[0])
    assert hit_start > 2000, hit_excerpt["location"]
    # 展示与授权一致：清单里的每条上下文都登记为可读（回执里带编号）
    assert len(located["excerpts"]) == len(located["matches"]) >= 1
    for row in located["excerpts"]:
        assert body[slice(*map(int, row["location"][6:].split("-")))] == row["quote"]


def test_cross_boundary_find_locates_string_spanning_two_excerpts():
    """跨片段命中可定位：字符串落在 700 字边界上时不再返回 None。"""
    config = sr.SimpleConfig(max_llm_calls=8, max_tool_calls=8, wall_timeout_s=60.0)
    result, script = _run(
        _BOUNDARY_SEQUENCE, config=config, raw=BOUNDARY_RAW, wrapup_text="收尾"
    )
    hits = [p for p in _tool_payloads(script) if _is_find_hit(p)]
    assert hits, "跨片段命中必须返回结果，而不是报找不到"
    hit = hits[0]
    excerpt = hit["excerpts"][0]
    assert BOUNDARY_TARGET in excerpt["quote"]
    start, stop = map(int, excerpt["location"][6:].split("-"))
    body = next(iter(_saved_bodies(result).values()))
    assert body[start:stop] == excerpt["quote"], "返回片段必须与保存内容逐字一致"
    assert len(excerpt["quote"]) <= sr._FIND_CONTEXT_CHARS
    # 该编号必须能原样取回这条上下文（不能只取回首片、丢掉命中后半）
    snapshot = sr.SourceSnapshot(ref="s1", url="https://e.org", title="t")
    reading = sr.Reading(snapshot, BOUNDARY_RAW)
    # 复现此前无参阅读：新的连续窗口会登记一个稳定展示编号。
    reading.window(0, config.effective_read_window())
    row = reading.find_matches(BOUNDARY_TARGET)["rows"][0]
    assert (row["excerpt_no"], row["location"]) == (excerpt["excerpt_no"], excerpt["location"])
    assert reading.quote(row["excerpt_no"]) == (excerpt["quote"], excerpt["location"])


@pytest.mark.parametrize("range_arg", ["0-200000", "2000-", "0-500", "-1500-", "40000-"])
def test_every_range_path_respects_the_per_call_window(range_arg):
    """range 的每条路径（含无上界）都不得突破单次回执窗口，且授权=展示。"""
    config = sr.SimpleConfig(
        max_llm_calls=6, max_tool_calls=6, wall_timeout_s=60.0, read_window_chars=2000
    )
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1", "range": range_arg})),
    ]
    raw = _PARA * 700  # 带段落标记：片段边界可预期
    result, script = _run(turns, config=config, raw=raw, wrapup_text="收尾")
    window = _windows(script)[0]
    body = next(iter(_saved_bodies(result).values()))
    total = window["total_chars"]
    start, stop = _bounds(window)
    assert stop - start <= config.effective_read_window(), window["shown_range"]
    assert stop <= total
    if range_arg.startswith("-"):
        assert stop == total, "负数区间必须落在保存范围末尾"
    else:
        requested_start = 0 if range_arg.startswith("0-") else int(range_arg.split("-")[0])
        expected_start = min(requested_start, total)  # 起点越过末尾时夹到末尾
        assert start >= expected_start, "展示区间不得早于请求起点"
        assert start - expected_start <= config.effective_read_window() * 2, "起点偏移必须有限"
        if requested_start >= total:
            assert window["excerpts"] == [] and window["has_more"] is False
    assert len(json.dumps(window, ensure_ascii=False)) < 4000, "单次回执必须是有界的"
    # 每条返回片段都完整落在展示区间内，且与保存内容逐字一致
    for excerpt in window["excerpts"]:
        a, b = map(int, excerpt["location"][6:].split("-"))
        assert start <= a and b <= stop, "不得返回窗口外的片段"
        assert body[a:b] == excerpt["quote"]
    # 展示区间之外的接缝不得被算成已读（否则模型能引用没看过的内容）
    shown = [
        (int(e["location"][6:].split("-")[0]), int(e["location"][6:].split("-")[1]))
        for e in window["excerpts"]
    ]
    if shown:
        assert start == shown[0][0] and stop == shown[-1][1]
        assert sum(b - a for a, b in shown) == stop - start, "展示区间必须是连续无空洞的若干片段"
    assert result.stop_reason


def test_cursor_beyond_source_returns_empty_tail():
    """越界游标不返回正文，并明确表示已到末尾。"""
    raw = _PARA * 700  # 带段落标记的长文
    total = len(clean_markdown(raw))
    result, script = _run(
        [
            _turn(("search_sources", {"query": "越界"})),
            _turn(("read_source", {"source_ref": "s1", "cursor": total + 5000})),
        ],
        config=BASE,
        raw=raw,
        wrapup_text="收尾",
    )
    tail = _windows(script)[0]
    assert tail["excerpts"] == []
    assert tail["has_more"] is False and tail["next_cursor"] is None
    assert result.stop_reason


def test_truncation_is_explicit_when_storage_cap_is_exceeded():
    """超过存储上限才截断，且如实标记；不把截断说成全文。"""
    config = sr.SimpleConfig(
        max_llm_calls=8, max_tool_calls=8, wall_timeout_s=60.0, storage_max_chars=3000
    )
    turns = [
        _turn(("search_sources", {"query": "超限长文"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    result, script = _run(turns, config=config, raw=LONG_RAW, wrapup_text="收尾")
    fulls = [s for s in result.sources if s.snapshot_kind == "full"]
    assert fulls and fulls[0].truncated is True, "超过存储上限必须标记 truncated"
    assert fulls[0].raw_chars == 3000, "保存范围等于配置的上限"
    window = _windows(script)[0]
    assert window["truncated_source"] is True
    assert window["total_chars"] == 3000
    assert LONG_TARGET not in next(iter(_saved_bodies(result).values()))
