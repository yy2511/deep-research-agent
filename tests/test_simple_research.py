"""研究主流程：主 Agent 搜索、阅读、存证、交出带引用的回答。

全部离线：只替换外部 LLM 与检索，核心循环、原文落盘、存证校验、状态投影都不 mock。
重点覆盖三个反例：没读过或伪造位置的引文被拒收、空核查范围不能通过、额度用完后不再发新请求且保留材料。
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from dra import research as research_mod
from dra import simple_research as sr
from dra.llm import ToolCall, ToolTurn
from dra.models import RetrievedDoc
from dra.text import clean_markdown

QUERY = "sqlite3_rsync 怎么在主机之间同步数据库？"
CLEAN_RAW = ""  # 在下方 RAW 定义后赋值

RAW = """# sqlite3_rsync

sqlite3_rsync 是 SQLite 官方提供的命令行工具。
它通过 SSH 在两台主机之间同步数据库，默认使用 ssh 传输，需要两端都安装 SQLite 工具。
这一行用于把第一段撑到接近片段上限，使「限制」部分落到第二个片段里，便于验证按词定位。

## 限制

sqlite3_rsync 不支持通过 HTTP 直接同步。
它要求两端都安装了相同主版本的 SQLite。
"""


# 快照零修改（2026-09-17 用户决定）：落盘的就是上游原文，不再是清洗后的文本。
# 名字保留是历史原因——大量断言拿它当「快照文本」的代名词。
CLEAN_RAW = RAW

# 长文来源：用于验证分片读取与按词定位（目标短语落在第 2 片）。
LONG_RAW = (
    "# 长文来源\n\n"
    + "这是用于撑长正文的补充段落。\n\n" * 60
    + "## 限制\n\n不支持通过 HTTP 直接同步。\n"
)


def _doc(
    raw=RAW,
    *,
    id="d1",
    url="https://sqlite.org/rsync.html",
    title="sqlite3_rsync 官方文档",
    snippet="sqlite3_rsync 通过 SSH 同步数据库",
    published_at="2026-01-02",
):
    return RetrievedDoc(
        id=id,
        source_url=url,
        title=title,
        snippet=snippet,
        raw_content=raw,
        published_at=published_at,
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


class Script:
    """脚本化模型。

    - ``__call__(messages, **kw)``：按序吐出预设回合，之后一律 finish。
    - ``resolve(args)``：把 ``{$url:后缀}`` / ``{$full:后缀}`` 占位符换成真实 source_ref，
      依据是本轮之前真实发生过的工具回执（``context.receipts``）。
    """

    def __init__(self, turns, finish=None):
        self.turns = list(turns)
        self.finish = finish or (
            "finish_research",
            {
                "draft": "sqlite3_rsync 通过 SSH 同步数据库 [c1]。",
                "coverage": [{"requirement_id": "goal", "status": "answered"}],
            },
        )
        self.seen = []
        self.context = None

    def __call__(self, messages, **kwargs):
        # 生产侧会传 max_retries=0 等参数；替身记录它们以便断言「一次请求一次预算」。
        self.kwargs = kwargs
        self.seen.append(messages)
        index = len(self.seen) - 1
        return self.turns[index] if index < len(self.turns) else _turn(self.finish)

    def resolve(self, args):
        summaries, fulls = {}, {}
        excerpts: list = []
        for item in self.context.receipts if self.context is not None else []:
            payload = _payloads(item["payload"])
            if payload is None:
                continue
            if item["tool"] == "search_sources":
                for row in payload.get("results", []):
                    summaries[row["url"]] = row["source_ref"]
            elif item["tool"] == "read_source" and payload.get("total_chars"):
                fulls[payload.get("url", "")] = payload["source_ref"]
                # 最近一次成功的精读：它的编号就是模型"看得见"的那些（find 出的是命中清单，
                # 编号在回执里给，模型只能用回执给的编号——测试替身按同样规则走）。
                excerpts = [row.get("excerpt_no") for row in payload.get("excerpts") or []]

        def sub(value):
            if isinstance(value, str):
                if value.startswith("{$excerpt:") and value.endswith("}"):
                    index = int(value[len("{$excerpt:") : -1])
                    assert 0 <= index < len(excerpts), (
                        f"最近一次精读没有第 {index} 条片段：{excerpts}"
                    )
                    return excerpts[index]
                for prefix, table, label in (
                    ("{$url:", summaries, "摘要"),
                    ("{$full:", fulls, "全文"),
                ):
                    if value.startswith(prefix) and value.endswith("}"):
                        suffix = value[len(prefix) : -1]
                        hit = next((r for u, r in table.items() if u.endswith(suffix)), None)
                        assert hit, f"没有 URL 以 {suffix} 结尾的{label}快照：{list(table)}"
                        return hit
                return value
            if isinstance(value, dict):
                return {k: sub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [sub(v) for v in value]
            return value

        return sub(args)


def _payloads(text):
    """从一个工具回执文本里取出 JSON 负载（不是 JSON 就返回 None）。"""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _run(
    turns,
    *,
    finish=None,
    raw=RAW,
    search=None,
    config=None,
    queries=1,
    run_dir=None,
):
    script = Script(turns, finish=finish)

    # Script 同时是可调用对象与 ref 解析器：ToolContext 会在执行前调用它。

    result = sr.run_simple_research(
        QUERY,
        sr.requirements_from_query(QUERY),
        config=config or sr.SimpleConfig(),
        run_dir=run_dir,
        model_call=script,
        search_fn=search or (lambda q: [_doc(raw=raw)]),
        ref_resolver=script,
    )
    return result, script


def _full_sequence():
    return [
        _turn(("search_sources", {"query": "sqlite3_rsync SSH"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(
            (
                "save_evidence",
                {
                    "cards": [
                        {
                            "claim": "sqlite3_rsync 通过 SSH 在主机之间同步数据库",
                            "source_ref": "s1",
                            "excerpt_no": 1,
                        }
                    ]
                },
            )
        ),
    ]


# ---------------------------------------------------------------------------
# 主路径
# ---------------------------------------------------------------------------


def test_single_lead_loop_delivers_cited_answer_without_worker_or_plan():
    result, script = _run(_full_sequence())

    assert result.delivery_status == "delivered"
    assert result.stop_reason == "finished"
    assert result.research_coverage == {"goal": "answered"}
    assert "[c1]" in result.draft
    # 零 Worker、零计划节点：结果对象里根本不出现这些字段，也没有派工动作。
    dumped = result.model_dump()
    assert "attempts" not in dumped and "plan" not in dumped
    # 引用确实来自已落盘快照，不是模型抄的。
    assert len(result.cards) == 1
    assert result.cards[0].quote in CLEAN_RAW
    assert result.cards[0].location.startswith("chars=")
    assert result.sources[-1].snapshot_kind == "full"
    # 脚本化模型不经过真实计费口，用量为空是预期；调用账本仍完整。
    assert [c.role for c in result.calls] == ["lead"] * 4


def test_card_source_ref_points_to_the_body_snapshot(tmp_path):
    """卡片身份必须是正文快照：ref → 快照元数据 → 原文文件 → quote/定位与 hash。

    摘要 ref 只是进入阅读的入口（存在 summary_ref 里），不能写进引用依据字段。
    """
    result, _ = _run(_full_sequence(), run_dir=tmp_path / "run")
    cards = result.cards
    assert len(cards) == 1
    card = cards[0]
    by_ref = {s.ref: s for s in result.sources}
    # 卡片 ref 必须是 full 快照，且该快照的原文文件真的存在。
    assert card.source_ref != card.summary_ref
    assert by_ref[card.source_ref].snapshot_kind == "full"
    body = (tmp_path / "run" / "sources" / f"{card.source_ref}.txt").read_text(encoding="utf-8")
    assert card.quote in body
    assert (
        by_ref[card.source_ref].content_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    )
    # 定位确实落在该快照的已读范围内。
    start, end = (int(x) for x in re.fullmatch(r"chars=(\d+)-(\d+)", card.location).groups())
    assert body[start:end] == card.quote
    # 摘要快照仍然不可引用，且登记了它到正文快照的线索。
    assert by_ref[card.summary_ref].quotable is False
    assert by_ref[card.summary_ref].url == by_ref[card.source_ref].url


def test_same_url_new_content_produces_new_snapshot(tmp_path):
    """同一 URL 内容变化 → 新快照，旧 ref 的原文不被覆盖。"""
    changed = RAW.replace("默认使用 ssh 传输", "改用 TLS 传输")

    def search(_query):
        # 同一批结果里给出两个不同 URL 的来源；取全文时都从这里取，不重复检索。
        return [_doc(raw=RAW), _doc(raw=changed, id="d2", url="https://sqlite.org/other.html")]

    # 先读 s1（落盘 RAW）；再读 s3（另一个 URL，落盘 changed）→ 两个独立快照。
    turns = _full_sequence() + [
        _turn(("read_source", {"source_ref": "{$url:other.html}"})),
        _turn(
            (
                "save_evidence",
                {
                    "cards": [
                        {
                            "claim": "另一篇来源改用 TLS",
                            "source_ref": "{$url:other.html}",
                            "excerpt_no": 1,
                        }
                    ]
                },
            )
        ),
    ]
    result, _ = _run(turns, search=search, run_dir=tmp_path / "run")
    first = next(
        s for s in result.sources if s.snapshot_kind == "full" and s.url.endswith("rsync.html")
    )
    second = next(
        s for s in result.sources if s.snapshot_kind == "full" and s.url.endswith("other.html")
    )
    assert first.content_sha256 != second.content_sha256
    assert (tmp_path / "run" / "sources" / f"{first.ref}.txt").read_text(encoding="utf-8") != (
        tmp_path / "run" / "sources" / f"{second.ref}.txt"
    ).read_text(encoding="utf-8")


def test_inspect_research_reads_registered_material():
    """inspect_research 能回读已登记材料，且不改变任何状态。"""
    turns = _full_sequence() + [
        _turn(("inspect_research", {"view": "cards"})),
        _turn(("inspect_research", {"view": "sources"})),
        _turn(("inspect_research", {"view": "summary"})),
    ]
    result, _ = _run(turns)
    assert result.delivery_status == "delivered"
    # 回读不新增卡片、不新增来源快照。
    assert len(result.cards) == 1
    assert len(result.sources) == 2


# ---------------------------------------------------------------------------
# 反例一：未读/伪造定位拒收
# ---------------------------------------------------------------------------


def test_unread_evidence_is_rejected():
    """没 read_source 就存证：拒收，且不产生卡片。"""
    turns = [
        _turn(("search_sources", {"query": "sqlite3_rsync"})),
        _turn(
            (
                "save_evidence",
                {"cards": [{"claim": "未读就引用", "source_ref": "s1", "excerpt_no": 1}]},
            )
        ),
    ]
    result, script = _run(turns)
    assert result.cards == []
    tool_blob = json.dumps(script.seen, ensure_ascii=False)
    assert "先读后引" in tool_blob


def test_forged_excerpt_number_is_rejected():
    """读了第 1 段却引用不存在的序号：拒收，已读范围内才放行。"""
    turns = _full_sequence()[:2] + [
        _turn(
            (
                "save_evidence",
                {"cards": [{"claim": "伪造定位", "source_ref": "s1", "excerpt_no": 99}]},
            )
        ),
    ]
    result, script = _run(turns)
    assert result.cards == []
    assert "不在来源" in json.dumps(script.seen, ensure_ascii=False)


def test_model_supplied_fabricated_quote_is_rejected_even_with_valid_pointer():
    """模型提交不存在的quote时拒收，不回退到合法编号掩盖错误。"""
    turns = _full_sequence()[:2] + [
        _turn(
            (
                "save_evidence",
                {
                    "cards": [
                        {
                            "claim": "服务端回填",
                            "source_ref": "s1",
                            "excerpt_no": 1,
                            "quote": "这是模型自己编的原文，不存在于来源里",
                        }
                    ]
                },
            )
        ),
    ]
    result, _ = _run(turns)
    assert result.cards == []


def test_read_cursor_continues_past_first_window():
    """按范围读到后文：cursor 能继续读，读到末尾后 has_more 为假。"""
    turns = [
        _turn(("search_sources", {"query": "sqlite3_rsync"})),
        _turn(("read_source", {"source_ref": "s1", "range": "0-700"})),
        _turn(("read_source", {"source_ref": "s1", "cursor": 700})),
        _turn(
            (
                "save_evidence",
                {
                    "cards": [
                        {"claim": "引用了读过的第 1 片段", "source_ref": "s1", "excerpt_no": 1},
                        {"claim": "也引用了续读到的第 2 片段", "source_ref": "s1", "excerpt_no": 2},
                    ]
                },
            )
        ),
    ]
    result, _ = _run(turns, raw=LONG_RAW)
    assert len(result.cards) == 2, "续读后第 2 片段应可引用"
    excerpt_starts = [row[0] for row in sr._excerpt_table(LONG_RAW)]
    assert result.cards[0].location.startswith("chars=0-")
    assert result.cards[1].location.startswith(f"chars={excerpt_starts[1]}-")


def test_read_find_returns_match_list_and_the_hit_becomes_citable():
    """find 给的是**命中清单**（地址 + 行号 + 短上下文），清单里的片段可直接引用。

    改动前 find 只给一处命中、且登记"所在整片"；现在给清单，上下文是命中所在的那一行
    （≤200 字符），编号同样登记为可引用区间。
    """
    turns = [
        _turn(("search_sources", {"query": "sqlite3_rsync"})),
        _turn(("read_source", {"source_ref": "s1", "range": "0-700"})),
        _turn(
            (
                "save_evidence",
                {
                    "cards": [
                        {"claim": "后文片段还没读到就引用", "source_ref": "s1", "excerpt_no": 2}
                    ]
                },
            )
        ),
        _turn(("read_source", {"source_ref": "s1", "find": "不支持通过 HTTP 直接同步"})),
        _turn(
            (
                "save_evidence",
                {
                    "cards": [
                        {
                            "claim": "不支持 HTTP 同步",
                            "source_ref": "s1",
                            "excerpt_no": "{$excerpt:0}",
                        }
                    ]
                },
            )
        ),
    ]
    result, _ = _run(turns, raw=LONG_RAW)
    # 第一次被拒（未读），find 之后第二次通过：最终只有一张卡。
    assert len(result.cards) == 1
    assert "不支持通过 HTTP 直接同步" in result.cards[0].quote
    # 编号指向的就是清单里那条上下文，且能原样取回
    start, stop = map(int, result.cards[0].location[6:].split("-"))
    assert result.cards[0].quote == LONG_RAW[start:stop]
    assert stop - start <= sr._FIND_CONTEXT_CHARS
    assert result.delivery_status in {"delivered", "limited"}

# ---------------------------------------------------------------------------
# 反例三：预算耗尽不发新请求且保留材料
# ---------------------------------------------------------------------------


def test_call_budget_exhaustion_reserves_bounded_wrapup_and_keeps_material():
    """模型预算用尽：不再发研究请求，改为**一次**有界收尾；已存材料原样保留。

    研究循环提前停在 `reserve_wrapup_calls`，把预留的一次用于**只允许交稿**的收尾回合；
    草稿允许为空但必须有收尾记录，总调用数仍不得超上限。来源与卡片保留。
    """
    # 4 次 = 研究 3 次（搜/读/存）+ 收尾 1 次预留。
    # 这样三条研究动作都在额度内完成，用来验证「额度耗尽后仍有有界收尾」。
    config = sr.SimpleConfig(max_llm_calls=4, max_tool_calls=20)
    result, script = _run(_full_sequence(), config=config)

    assert result.stop_reason.startswith("call_budget"), "研究循环因模型额度停下"
    # 材料保留：来源已落盘、卡片已存。
    assert any(s.snapshot_kind == "full" for s in result.sources)
    assert len(result.cards) == 1
    # 总额度不可突破，且收尾恰好一次、只带交稿工具。
    assert len(result.calls) <= config.max_llm_calls
    wrapup = [c for c in result.calls if c.step == "wrapup"]
    assert len(wrapup) <= 1, "收尾回合至多一次"
    lead_calls = [c for c in result.calls if c.role == "lead"]
    assert len(lead_calls) <= config.max_llm_calls
    assert result.delivery_status == "delivered"
    assert [s["function"]["name"] for s in script.kwargs["tools"]] == ["finish_research"]
    assert result.research_coverage == {"goal": "answered"}
    if not result.draft:
        assert result.delivery_status == "failed"


def test_budget_exhausted_before_lead_loop_leaves_no_calls():
    """1 次额度 + 1 次收尾预留：研究循环一次都不发，唯一那次用于收尾。

    预留吃掉全部额度是合法配置——系统应当诚实地不发研究请求，而不是拒绝配置。
    """
    config = sr.SimpleConfig(max_llm_calls=1, reserve_wrapup_calls=1)
    result, script = _run(_full_sequence(), config=config)
    assert len(script.seen) == 1, "研究回合一次都不发；唯一一次是收尾"
    assert "本次收尾占用其中 1 次" in script.seen[0][-1]["content"]
    # 收尾没有产出正文（该脚本的收尾文本为空），因此停止原因保持纯 call_budget。
    assert result.stop_reason == "call_budget"
    assert len(result.calls) == 1
    assert result.delivery_status == "failed"


def test_tool_budget_exhaustion_keeps_saved_cards():
    """工具预算用尽：停止新增检索/阅读，已落盘材料与已存卡片保留。"""
    config = sr.SimpleConfig(max_llm_calls=12, max_tool_calls=2)
    result, _ = _run(_full_sequence(), config=config)
    assert result.stop_reason == "tool_budget"
    # 停止新增检索/阅读，但已落盘正文保留；没有草稿就是 failed，不假装交付。
    assert any(s.snapshot_kind == "full" for s in result.sources)
    assert result.delivery_status == "failed"


# ---------------------------------------------------------------------------
# 运行目录与恢复边界
# ---------------------------------------------------------------------------


def test_run_directory_persists_sources_cards_and_status(tmp_path):
    result, _ = _run(_full_sequence(), run_dir=tmp_path / "r1")
    directory = tmp_path / "r1"
    assert (directory / "sources" / "s2.txt").exists()
    assert (directory / "report.md").read_text(encoding="utf-8") == result.markdown
    ledger = json.loads((directory / sr.RunStore.filename).read_text(encoding="utf-8"))
    assert ledger["finished"] is True
    assert ledger["final"]["delivery_status"] == "delivered"
    assert [c["role"] for c in ledger["calls"]] == ["lead"] * 4
    assert all(c["status"] == "returned" for c in ledger["calls"])


def test_resume_is_explicitly_refused_and_material_preserved(tmp_path):
    directory = tmp_path / "r2"
    _run(_full_sequence(), run_dir=directory)
    before = (directory / sr.RunStore.filename).read_text(encoding="utf-8")

    with pytest.raises(sr.IncompatibleRunError) as error:
        _run(_full_sequence(), run_dir=directory)
    assert "已有运行台账" in str(error.value)
    with pytest.raises(sr.IncompatibleRunError) as error:
        sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            run_dir=directory,
            resume=True,
            model_call=lambda *a, **k: _turn(("finish_research", {"draft": "x"})),
            search_fn=lambda q: [_doc()],
        )
    assert "拒绝自动恢复" in str(error.value)
    assert (directory / sr.RunStore.filename).read_text(encoding="utf-8") == before


def test_finished_run_rejects_further_submission(tmp_path):
    """单提交入口检查运行未结束：结束后不能再追加动作。"""
    directory = tmp_path / "r3"
    _run(_full_sequence(), run_dir=directory)
    store = sr.RunStore(directory, run_id="reopen", query=QUERY)
    store.state["finished"] = True
    with pytest.raises(RuntimeError, match="运行已结束"):
        store.check_open()
    """从研究入口层跑通一题：核心循环真实执行，只有外部 LLM/搜索被替换。

    同时证明没有创建 Worker、没有初始规划、没有计划节点。
    """
    from dra import research

    script = Script(_full_sequence())
    result = research.run_minimal_research(
        QUERY,
        sr.requirements_from_query(QUERY),
        # 核查是可选功能；本用例验证入口层能跑通含核查的完整闭环。
        config=sr.SimpleConfig(),
        run_id="entry-simple",
        checkpoint_dir=tmp_path / "entry",
        model_call=script,
        search_fn=lambda q: [_doc()],
        ref_resolver=script,
    )
    assert result.delivery_status == "delivered"
    assert len(result.cards) == 1
    assert (tmp_path / "entry" / "report.md").exists()
    assert (tmp_path / "entry" / "sources").is_dir()
    # 路线同步标记是主线展示元数据；结果仍没有 Worker/DAG 计划节点字段。
    assert not any("attempt" in name or "plan" in name
                   for name in result.model_dump() if name != "plan_sync_status")


def test_script_simple_mode_offline_reports_true_status(tmp_path, capsys):
    """--mode simple 的离线默认不发调用，且如实报告未执行；默认同样走自主研究。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_research_script", "scripts/run_research.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.main(["--mode", "simple", "某问题"])
    simple_row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert simple_row["mode"] == "simple"
    assert simple_row["status"] == "dry_run"
    assert simple_row["stop_reason"] == "live_not_enabled"
    assert simple_row["cards"] == 0 and simple_row["sources"] == 0

    module.main(["某问题"])
    full_row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert full_row["mode"] == "simple"
    assert full_row["status"] == "dry_run"
    assert "worker_attempts" not in full_row


def test_script_simple_explicit_limits_used_verbatim():
    """simple CLI 显式上限原样使用：极小值不放大（SimpleConfig 支持诚实兜底）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_research_script2", "scripts/run_research.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.parser().parse_args(
        ["--mode", "simple", "--max-lead-turns", "1", "--timeout", "60", "某问题"]
    )
    cfg = module.simple_config(args)
    assert cfg.max_llm_calls == 1, "--max-lead-turns 1 不得被放大"
    assert cfg.max_tool_calls == 1
    assert cfg.wall_timeout_s == 60.0

    args = module.parser().parse_args(
        ["--mode", "simple", "--max-lead-turns", "6", "--timeout", "90", "某问题"]
    )
    cfg = module.simple_config(args)
    assert cfg.max_llm_calls == 6 and cfg.max_tool_calls == 6 and cfg.wall_timeout_s == 90.0




def test_entry_layer_defaults_to_the_confirmed_question_as_requirement(tmp_path):
    """入口层不显式给要求时，用确认后的问题本身当唯一要求。

    否则「零要求全通过」会让核查失去对象，也漏掉用户到底问了什么。
    """
    from dra import research

    script = Script(_full_sequence())
    result = research.run_minimal_research(
        QUERY,
        None,
        run_id="entry-default-req",
        checkpoint_dir=tmp_path / "entry-default",
        model_call=script,
        search_fn=lambda q: [_doc()],
        ref_resolver=script,
    )
    assert [r.id for r in result.requirements] == ["goal"]
    assert result.requirements[0].text == QUERY
    assert result.research_coverage == {"goal": "answered"}


def test_citation_gate_detects_dangling_references_without_model():
    """确定性门：草稿引用不存在的卡片，或没有任何卡片，代码当场看出来。"""
    cards = [
        sr.EvidenceCard(
            card_id="c1", claim="a", quote="q", source_ref="s1", source_url="https://x/1"
        )
    ]
    ok, problems = sr.citation_gate("结论 [c1]。", cards)
    assert ok and problems == []
    ok, problems = sr.citation_gate("结论 [c7]。", cards)
    assert not ok and "不存在的卡片" in problems[0]
    ok, problems = sr.citation_gate("凭印象的确定断言。", [])
    assert not ok and "无法溯源" in problems[0]


def test_no_card_run_cannot_claim_answered_coverage():
    """零卡片却自报 answered：覆盖降为未回答，且不按完整交付。"""
    turns = [
        _turn(("search_sources", {"query": "sqlite3_rsync"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    result, _ = _run(turns)
    assert result.cards == []
    assert result.research_coverage == {"goal": "unanswered"}
    assert any(
        "无法对应到已保存证据卡" in reason for reason in result.coverage_reasons.get("goal", [])
    )
    assert result.delivery_status == "limited"
    assert any("确定性引用门未通过" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Codex M1 审查 R1–R8：每条一个能识别错误行为的回归
# ---------------------------------------------------------------------------


def test_r2_batch_cannot_exceed_tool_budget():
    """R2：一轮多个工具调用不能突破工具额度；未执行的没有触达外部边界。"""
    searches = {"n": 0}

    def search(_query):
        searches["n"] += 1
        return [_doc()]

    config = sr.SimpleConfig(max_llm_calls=6, max_tool_calls=1)
    turns = [
        _turn(
            ("search_sources", {"query": "q1"}),
            ("search_sources", {"query": "q2"}),
            ("search_sources", {"query": "q3"}),
        )
    ]
    result, _ = _run(turns, search=search, config=config)
    assert searches["n"] == 1, "只有授权内的一次检索能触达检索边界"
    assert result.stop_reason == "tool_budget+wrapup"
    assert result.draft_rejected  # 脚本交了不存在的 c1，不能变成已验证答案。


def test_r2_tools_after_finish_are_not_executed():
    """R2：同一轮里 finish 之后的研究动作不再执行。"""
    searches = {"n": 0}

    def search(_query):
        searches["n"] += 1
        return [_doc()]

    turns = [
        _turn(("search_sources", {"query": "q1"})),
        _turn(("read_source", {"source_ref": "s1"})),
        # 先存证再交稿：交稿那一刻会核对 [cN] 是否指向真实卡片，没有卡片的引用会被拒收
        # 重交——那是这条检查该有的行为，不是本用例要测的东西。
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
                    "draft": "答案 [c1]。",
                    "coverage": [{"requirement_id": "goal", "status": "answered"}],
                },
            ),
            ("search_sources", {"query": "不该执行的检索"}),
        ),
    ]
    result, _ = _run(turns, search=search)
    # 读完 s1 用的是检索已取得的正文（R7），所以只应有第一次检索；
    # finish 同批里的第二次检索不得执行。
    assert searches["n"] == 1
    assert result.draft == "答案 [c1]。"
    first, _ = _run(_full_sequence())
    second, _ = _run(_full_sequence())
    assert first.artifact_dir != second.artifact_dir
    assert Path(first.artifact_dir).joinpath("sources").is_dir()
    assert Path(second.artifact_dir).joinpath("sources").is_dir()
    first_ledger = json.loads(
        Path(first.artifact_dir).joinpath(sr.RunStore.filename).read_text(encoding="utf-8")
    )
    assert first_ledger["query"] == QUERY
    assert first_ledger["finished"] is True


def test_r6_cancellation_stops_further_model_requests(tmp_path):
    """R6：外层取消信号必须被继承，取消后不再发模型请求。"""
    import threading

    from dra import llm

    stop = threading.Event()
    searches = {"n": 0}

    def search(_query):
        searches["n"] += 1
        stop.set()  # 第一次检索返回时取消
        return [_doc()]

    script = Script(_full_sequence())
    with llm.execution_scope(cancel_event=stop):
        result = research_mod.run_minimal_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            run_id="cancel-check",
            checkpoint_dir=tmp_path / "cancel",
            model_call=script,
            search_fn=search,
            ref_resolver=script,
        )
    assert result.stop_reason == "cancelled"
    assert len(script.seen) == 1, "取消后不得再发 Lead 请求"


def test_r7_read_uses_body_already_returned_by_search():
    """R7：检索已经拿到正文时，阅读直接落盘，不再按 URL 重搜。"""
    queries: list[str] = []

    def search(query):
        queries.append(query)
        return [_doc()]

    result, _ = _run(_full_sequence(), search=search)
    assert queries == ["sqlite3_rsync SSH"], "不应再拿 URL 当 query 二次检索"
    assert any(s.snapshot_kind == "full" for s in result.sources)
    assert len(result.cards) == 1


def test_r7_only_summary_available_reports_clearly(monkeypatch):
    """R7 反例：确实只有摘要时按 URL 提取正文；提取失败要如实报，不引用。

    合同更新（2026-09-15）：旧合同是"拿 URL 当 query 再搜一次"，本轮改为真正的按 URL
    提取（`url` 参数与只有摘要的 source_ref 走同一条路径）。**显式**把 extract 外部边界
    替换成已知失败：本套用例必须离线可跑，不能靠"恰好没有 API key"碰运气。
    """
    from dra import simple_research as sr_mod
    from dra.tools import UrlFetchError

    def failed_extract(url, *, timeout_s=None, deadline=None, include_usage=True):  # noqa: ARG001
        raise UrlFetchError("取正文失败：本次为离线用例（未联网）")

    monkeypatch.setattr(sr_mod, "fetch_url_content", failed_extract)
    turns = [
        _turn(("search_sources", {"query": "只有摘要的来源"})),
        _turn(("read_source", {"source_ref": "s1"})),
        # 第三轮让新一轮模型请求带上失败回执：直接断言模型看到的原因
        _turn(("finish_research", {"draft": "未能取得正文，如实说明缺口。"})),
    ]
    result, script = _run(turns, search=lambda q: [_doc(raw="")])
    assert result.cards == []
    blob = json.dumps(script.seen[-1], ensure_ascii=False)
    assert "取正文失败" in blob
    assert "摘要冒充正文" in blob or "不能引用" in blob


def test_r8_history_keeps_assistant_message_with_its_tool_receipts():
    """R8：下一轮消息必须成对——每个 tool 回执都有对应的 assistant tool_calls。"""
    turns = [
        _turn(
            ("search_sources", {"query": "q1"}),
            ("search_sources", {"query": "q2"}),
            ("search_sources", {"query": "q3"}),
            ("inspect_research", {"view": "summary"}),
            ("inspect_research", {"view": "sources"}),
            ("inspect_research", {"view": "cards"}),
        ),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    config = sr.SimpleConfig(max_llm_calls=8, max_tool_calls=8)
    _, script = _run(turns, config=config)
    # 预算/无进展收尾只拿已保存材料；检查最后一个仍有工具交换的研究回合。
    last = next(msgs for msgs in reversed(script.seen) if any(m["role"] == "tool" for m in msgs))
    roles = [m["role"] for m in last]
    tool_ids = [m["tool_call_id"] for m in last if m["role"] == "tool"]
    announced = {
        tc["id"] for m in last if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    }
    assert tool_ids, "本轮应带有工具回执"
    assert set(tool_ids) <= announced, f"孤儿 tool 回执：{sorted(set(tool_ids) - announced)}"
    # assistant 与 tool 必须相邻成组，不能从交换中间截断。
    for index, role in enumerate(roles):
        if role == "tool" and index and roles[index - 1] not in {"tool"}:
            assert roles[index - 1] == "assistant"


def test_record_action_is_idempotent_and_check_open_covers_writes(tmp_path):
    """声明缺口：动作按 action_id 幂等；写路径统一拒绝运行结束后的提交。"""
    store = sr.RunStore(tmp_path / "s", run_id="r", query="q")
    first = store.record_action("tool_call", {"tool": "search"}, action_id="a1")
    second = store.record_action("tool_call", {"tool": "search"}, action_id="a1")
    assert first is second
    assert len(store.state["actions"]) == 1

    store.finish({"delivery_status": "delivered"})
    with pytest.raises(RuntimeError, match="运行已结束"):
        store.add_card(
            sr.EvidenceCard(
                card_id="c1", claim="x", quote="y", source_ref="s1", source_url="https://x/1"
            )
        )
    with pytest.raises(RuntimeError, match="运行已结束"):
        store.write_source(_doc())
