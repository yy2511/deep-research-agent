"""把一次研究的 ResearchState 渲染成自包含单文件 HTML 轨迹查看器（可观测 / demo）。

为什么这么做（对齐 refs 共识：trajectorykit/deepdog/onyx/salesforce 都做纯文件 trace）
----------------------------------------------------------------------------------
- timing.py 只给「哪步最贵」的步骤计时；这里给「agent 到底干了什么」的可点开轨迹：
  query → 研究任务 → post-research 报告蓝图 → 各子代理找到的证据卡（含逐字 grounding 原文）
  → 矛盾/世界核查 → 报告。
- **复用已有 state，零新埋点**：不插桩、不改主流程，纯「state in → HTML out」，绝不可能弄崩研究。
- 自包含单文件（内联 CSS、原生 <details> 折叠、无外部依赖、无 JS 框架）→ 离线双击就能开，
  适合在 demo 中展示「agent 每一步在做什么、每条结论挂在哪句原文上」。
- 非侵入护栏：save_trace 全程 try/except 吞异常——trace 只是观测产物，绝不能因它崩了主流程。

边界（诚实）：本版从最终 state 重建轨迹，**没有 per-LLM-call 的 token/墙钟 span**（那要插桩
chat()）。性能维先用 timing.summary() 文本嵌进来；逐调用 span + 流式是后续增量。
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def _to_dict(state) -> dict:
    """接受 ResearchState 对象或其 model_dump() dict，统一成 dict。"""
    if state is None:
        return {}
    if hasattr(state, "model_dump"):
        try:
            return state.model_dump()
        except Exception:
            return {}
    return state if isinstance(state, dict) else {}


def build_trace(state) -> dict:
    """从 state 抽出渲染所需的轻量结构（防御式 .get，缺字段不报错）。"""
    s = _to_dict(state)
    research_plan = s.get("research_plan") or {}
    report_plan = s.get("report_plan") or {}
    report = s.get("report") or {}
    evidence = s.get("evidence") or []

    domains = set()
    for e in evidence:
        url = e.get("source_url") or ""
        try:
            net = urlparse(url).netloc
            if net:
                domains.add(net)
        except Exception:
            pass

    return {
        "query": s.get("query", ""),
        "clarified_query": research_plan.get("clarified_query", ""),
        "status": s.get("status", ""),
        "research_rounds_completed": s.get("research_rounds_completed", 0),
        "n_evidence": len(evidence),
        "n_domains": len(domains),
        "initial_tasks": research_plan.get("initial_tasks") or [],
        "report_plan": report_plan,
        "sub_reports": s.get("sub_reports") or [],
        "report": report,
    }


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _evidence_card_html(c: dict) -> str:
    claim = _esc(c.get("claim"))
    quote = _esc(c.get("support_quote"))
    url = c.get("source_url") or ""
    date = c.get("published_at")
    badges = []
    if date:
        badges.append(f'<span class="badge date">📅 {_esc(date)[:10]}</span>')
    src = (
        f'<a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(urlparse(url).netloc or url)}</a>'
        if url else '<span class="muted">无来源</span>'
    )
    return (
        '<div class="card">'
        f'<div class="claim">{claim}</div>'
        f'<blockquote class="quote">{quote}</blockquote>'
        f'<div class="meta">{" ".join(badges)} <span class="src">↗ {src}</span></div>'
        '</div>'
    )


def _subagent_html(r: dict, idx: int) -> str:
    obj = _esc(r.get("objective"))
    tool_calls = _esc(r.get("tool_calls"))
    summary = _esc(r.get("summary"))
    cards = r.get("evidence") or []
    conflicts = r.get("conflicts") or []
    cards_html = "".join(_evidence_card_html(c) for c in cards) or '<div class="muted">（无证据）</div>'
    conf_html = ""
    if conflicts:
        items = "".join(
            f'<li><b>{_esc(c.get("dimension"))}</b>: {_esc(c.get("description"))}</li>'
            for c in conflicts
        )
        conf_html = f'<div class="conflicts">⚠️ 矛盾：<ul>{items}</ul></div>'
    return (
        '<details class="subagent" open>'
        f'<summary><span class="idx">子代理 {idx}</span> {obj} '
        f'<span class="pill">{tool_calls} 次工具动作</span><span class="pill">{len(cards)} 证据</span></summary>'
        f'<div class="summary-line">{summary}</div>'
        f'{conf_html}'
        f'<div class="cards">{cards_html}</div>'
        '</details>'
    )


def _report_plan_html(report_plan: dict) -> str:
    secs = report_plan.get("sections") or []
    if not secs:
        return ""
    rows = []
    for ds in secs:
        limitations = ds.get("limitations") or []
        gap_html = "".join(f'<span class="gap">{_esc(g)}</span>' for g in limitations)
        rows.append(
            f'<div class="report-plan-sec"><div class="dh">{_esc(ds.get("heading"))}</div>'
            f'<div class="dc">{_esc(ds.get("covers"))}</div>'
            f'<div class="gaps">{gap_html}</div></div>'
        )
    return (
        '<details class="block"><summary>📝 post-research 报告蓝图'
        '（结构 + 局限/后续研究提示）</summary>'
        f'<div class="report-plans">{"".join(rows)}</div></details>'
    )


def _report_html(report: dict) -> str:
    secs = report.get("sections") or []
    if not secs:
        return ""
    items = "".join(
        f'<li>{_esc(sec.get("heading"))}'
        + (f' <span class="muted">({len(sec.get("markdown") or "")} 字)</span>' if sec.get("markdown") else "")
        + "</li>"
        for sec in secs
    )
    return (
        f'<details class="block"><summary>📄 最终报告：{_esc(report.get("title"))}（{len(secs)} 节）</summary>'
        f'<ol class="report-toc">{items}</ol>'
        '<div class="muted">完整报告见同目录 .md 文件</div></details>'
    )


_CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--fg:#e6e6e6;--muted:#8a93a3;--acc:#5b9dff;--ok:#3fb950;--warn:#d29922;--err:#f85149;--sig:#a371f7;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,"PingFang SC","Microsoft YaHei",sans-serif;padding:24px;max-width:1000px;margin:0 auto;overflow-wrap:anywhere}
h1{font-size:20px;margin:0 0 4px}.q{color:var(--fg);font-weight:600}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.stat{background:var(--card);border-radius:8px;padding:6px 12px}.stat b{color:var(--acc)}
.badge,.pill{display:inline-block;border-radius:6px;padding:1px 8px;font-size:12px;margin-right:4px}
.pill{background:#2a2f3a;color:var(--muted);margin-left:6px}
.badge.sig{background:rgba(163,113,247,.18);color:var(--sig)}
.badge.date{background:#22272e;color:var(--muted)}.badge.conf{background:#22272e;color:var(--muted)}
.status{padding:2px 10px;border-radius:6px;font-weight:600}
.status.done{background:rgba(63,185,80,.18);color:var(--ok)}.status.partial{background:rgba(210,153,34,.18);color:var(--warn)}.status.failed,.status.running{background:rgba(248,81,73,.18);color:var(--err)}
details{background:var(--card);border:1px solid #262b34;border-radius:10px;margin:10px 0;padding:8px 14px}
summary{cursor:pointer;font-weight:600;outline:none}
.idx{color:var(--acc);margin-right:6px}
.summary-line{color:var(--muted);margin:8px 0;font-style:italic}
.cards{display:flex;flex-direction:column;gap:10px;margin-top:8px}
.card{background:#13161c;border-left:3px solid var(--acc);border-radius:6px;padding:10px 12px}
.claim{font-weight:600;margin-bottom:6px}
.quote{margin:6px 0;padding:6px 12px;border-left:3px solid #3a4150;color:#c9d1d9;background:#0d1017;border-radius:4px;font-size:13px}
.meta{font-size:12px;color:var(--muted)}.src a{color:var(--acc);text-decoration:none}
.conflicts{color:var(--warn);background:rgba(210,153,34,.08);border-radius:6px;padding:6px 10px;margin:8px 0}
.conflicts ul{margin:4px 0 0 0;padding-left:18px}
.report-plans{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.report-plan-sec{background:#13161c;border-radius:6px;padding:8px 12px}.dh{font-weight:600;color:var(--acc)}.dc{color:var(--muted);font-size:13px;margin:2px 0}
.gap{display:inline-block;background:#2a2f3a;color:#c9d1d9;border-radius:6px;padding:1px 8px;font-size:12px;margin:2px 4px 0 0}
.report-toc{margin:8px 0;color:#c9d1d9}.muted{color:var(--muted)}
pre.timing{background:#0d1017;border-radius:8px;padding:12px;overflow:auto;font-size:12px;color:#c9d1d9}
"""


def render_html(trace: dict, *, timing_summary: str = "") -> str:
    """把 build_trace 的结构渲染成自包含 HTML 字符串。"""
    st = (trace.get("status") or "").lower()
    status_cls = st if st in ("done", "partial", "failed", "running") else "running"

    sub_html = "".join(
        _subagent_html(r, i + 1) for i, r in enumerate(trace.get("sub_reports") or [])
    ) or '<div class="muted">（无子代理记录）</div>'

    timing_html = (
        f'<details class="block"><summary>⏱ 步骤耗时（瓶颈）</summary><pre class="timing">{_esc(timing_summary)}</pre></details>'
        if timing_summary.strip() else ""
    )
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>研究轨迹 · {_esc(trace.get("query"))[:40]}</title><style>{_CSS}</style></head>
<body>
<h1>🔎 研究轨迹</h1>
<div class="q">{_esc(trace.get("query"))}</div>
<div class="stats">
  <span class="stat">状态 <span class="status {status_cls}">{_esc(trace.get("status"))}</span></span>
  <span class="stat">初始任务 <b>{len(trace.get("initial_tasks") or [])}</b></span>
  <span class="stat">证据 <b>{trace.get("n_evidence")}</b></span>
  <span class="stat">来源域名 <b>{trace.get("n_domains")}</b></span>
  <span class="stat">研究轮次 <b>{trace.get("research_rounds_completed")}</b></span>
</div>
{_report_plan_html(trace.get("report_plan") or {})}
<h2 style="font-size:16px">🐝 子代理与证据（逐字 grounding）</h2>
{sub_html}
{_report_html(trace.get("report") or {})}
{timing_html}
<div class="muted" style="margin-top:20px">由 dra.trace 生成 · 自包含离线 HTML</div>
</body></html>"""


def save_trace(state, out_dir, *, slug: str = "", timing_summary: str = "") -> Path | None:
    """渲染并落盘 trace HTML，返回路径；任何异常都吞掉返回 None（绝不弄崩主流程）。"""
    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"trace-{ts}{('-' + slug) if slug else ''}.html"
        path = out / name
        trace = build_trace(state)
        path.write_text(render_html(trace, timing_summary=timing_summary), encoding="utf-8")
        return path
    except Exception as e:  # noqa: BLE001 — trace 是观测产物，绝不能因它崩主流程
        print(f"[trace] ⚠️ 生成轨迹 HTML 失败（已忽略，不影响报告）：{e!r}")
        return None
