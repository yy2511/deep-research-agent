"""开发用 demo fixture 生成器：合成一条完整 loop 档事件序列，供前端开发/vitest 用，
不调任何真实 LLM/检索。序列参考已删的 _run_demo（git show 71a87b0^:src/dra/web.py），
子代理主体使用当前唯一的 loop 手牌流（subagent_tool_call）。

字段严格按 dra.events.EventType 的 docstring 字段契约表；只用 EventType 常量，
不写裸字符串 type（这正是事件契约守卫要防的漂移）。收口期（Task 17）换真实录制，
本脚本保留作 fixture 再生工具。
"""
import json
from pathlib import Path

from dra.events import EventType

_T = EventType


def _loop_hands(sid: str, objective: str, *, with_rejection: bool) -> list[tuple[dict, float]]:
    """4 手循环：search → fetch_page → save_evidence → finish。
    with_rejection=True 时 save_evidence 带一条拒收样本（grounding 拒收，前端渲染面）。"""
    hands = [
        ({"type": _T.SUBAGENT_TOOL_CALL.value, "sid": sid, "objective": objective, "call_no": 1,
          "tool": "search", "ok": True,
          "args_summary": '{"query": "' + objective[:20] + '"}',
          "result_summary": '{"results": [{"doc_id": "d1", "title": "相关综述"}, …共 6 篇]}',
          "links": [{"title": "相关综述", "url": "https://example.com/review"},
                    {"title": "行业白皮书", "url": "https://example.org/whitepaper"}],
          "evidence_total": 0}, 0.5),
        ({"type": _T.SUBAGENT_TOOL_CALL.value, "sid": sid, "objective": objective, "call_no": 2,
          "tool": "fetch_page", "ok": True,
          "args_summary": '{"doc_id": "d1"}',
          "result_summary": "【相关综述】正文摘录若干段，含可引用的原文语句…",
          "url": "https://example.com/review", "n_excerpts": 3,
          "evidence_total": 0}, 0.5),
    ]
    if with_rejection:
        hands.append(({
            "type": _T.SUBAGENT_TOOL_CALL.value, "sid": sid, "objective": objective, "call_no": 3,
            "tool": "save_evidence", "ok": True,
            "args_summary": '{"cards": [{"claim": "关键结论 A", "quote": "…"}, ×3]}',
            "result_summary": '{"saved_total": 2, "remaining_quota": 43}',
            "accepted": 2, "rejected": 1,
            "reject_reasons": ["quote 无法在原文中逐字找到——必须是连续原文，不得改写/翻译/缝合"],
            "evidence_total": 2,
        }, 0.7))
    else:
        hands.append(({
            "type": _T.SUBAGENT_TOOL_CALL.value, "sid": sid, "objective": objective, "call_no": 3,
            "tool": "save_evidence", "ok": True,
            "args_summary": '{"cards": [{"claim": "关键结论 B", "quote": "…"}, ×2]}',
            "result_summary": '{"saved_total": 2, "remaining_quota": 43}',
            "accepted": 2, "rejected": 0, "evidence_total": 2,
        }, 0.7))
    hands.append(({
        "type": _T.SUBAGENT_TOOL_CALL.value, "sid": sid, "objective": objective, "call_no": 4,
        "tool": "finish", "ok": True,
        "args_summary": '{"summary": "已覆盖该子问题核心证据", "conflicts": []}',
        "result_summary": "收工：核心结论已有原文支撑证据。",
        "evidence_total": 2,
    }, 0.5))
    return hands


objectives = ["RAG 的核心优点与降幻觉机制", "微调的优势与代价", "延迟与系统复杂度对比"]
initial_tasks = [
    {
        "id": f"s{i}",
        "node_id": node_id,
        "objective": objective,
        "search_query": objective,
        "boundaries": None,
    }
    for i, (node_id, objective) in enumerate(
        zip(("rag", "tuning", "compare"), objectives, strict=True)
    )
]

seq: list[tuple[dict, float]] = [
    ({"type": _T.SCOPE.value, "query": "对比微调与 RAG 在企业落地的取舍"}, 0.3),
    ({"type": _T.LLM_CALL.value, "step": "build_research_plan", "model": "glm-5.2", "ms": 18900,
      "in_tok": 600, "out_tok": 280,
      "input": "【system】\n你是研究规划器，输出 typed plan-node DAG 与当前可执行任务。\n\n"
               "【user】\n问题：对比微调与 RAG 在企业落地的取舍",
      "output": '{"plan_nodes":[{"id":"rag","kind":"research"}, …], "initial_tasks":[{"id":"task-rag","node_id":"rag"}, …]}'}, 0.3),
    # step_done（无 sid/worker_iteration = 编排级步骤计时，对应 timing.step(...) 包住的阶段）：
    # 补这些不是凑数——demo fixture 之前完全没有 step_done 事件，Task 15 的"步骤耗时"
    # 统计表 + “累计步骤耗时”指标在 demo 回放里会一直是空表/0.0，这个坑只有真跑
    # 一遍 ResultPane 统计 Tab 才会发现（tsc/vitest 都测不出表是空的）。
    ({"type": _T.STEP_DONE.value, "step": "build_research_plan", "ms": 18900}, 0.1),
    ({"type": _T.RESEARCH_PLAN.value, "count": 3, "initial_tasks": initial_tasks,
      "plan_nodes": [
          {"id": "rag", "objective": objectives[0], "kind": "research", "dependency_ids": [], "acceptance_criteria": "有可引用证据"},
          {"id": "tuning", "objective": objectives[1], "kind": "research", "dependency_ids": [], "acceptance_criteria": "有可引用证据"},
          {"id": "compare", "objective": objectives[2], "kind": "research", "dependency_ids": [], "acceptance_criteria": "形成有证据的对比"},
      ]}, 0.5),
    ({"type": _T.TASK_BATCH_DISPATCHED.value, "round_index": 0, "count": 3,
      "objectives": objectives, "phase": "initial"}, 0.3),
]

for i, obj in enumerate(objectives):
    seq.append(({"type": _T.SUBAGENT_START.value, "sid": f"s{i}", "objective": obj,
                 "node_id": initial_tasks[i]["node_id"], "round_index": 0}, 0.3))

for i, obj in enumerate(objectives):
    seq += _loop_hands(f"s{i}", obj, with_rejection=(i == 1))   # s1 带拒收样本

for i, obj in enumerate(objectives):
    seq.append(({"type": _T.SUBAGENT_DONE.value, "sid": f"s{i}", "objective": obj,
                 "node_id": initial_tasks[i]["node_id"], "round_index": 0,
                 "tool_calls": 1, "evidence_count": 2,
                 "status": "ok", "stop_reason": "sufficient"}, 0.3))
# 三个研究任务并发执行；该 step 记录整个并发批次耗时，不等于三项任务时长之和。
seq.append(({"type": _T.STEP_DONE.value, "step": "dispatch_task_batch(并发)", "ms": 2100}, 0.1))

seq += [
    ({"type": _T.NODES_ASSESSED.value,
      "assessments": [
          {"node_id": "rag", "status": "complete", "summary": "证据充分", "gaps": []},
          {"node_id": "tuning", "status": "complete", "summary": "证据充分", "gaps": []},
          {"node_id": "compare", "status": "complete", "summary": "对比证据充分", "gaps": []},
      ],
      "completed_ids": ["rag", "tuning", "compare"],
      "unresolved_node_ids": []}, 0.3),
    ({"type": _T.READY_SET_COMPUTED.value, "round_index": 1, "pending": 0,
      "reason": "所有研究步骤均已完成", "n_tasks": 0}, 0.2),
    ({"type": _T.COLLECT.value, "before": 6, "after": 6, "deduped": 0, "failures": 0, "n_sub": 3}, 0.5),
    ({"type": _T.REPORT_PLAN.value,
      "sections": ["成本结构差异", "知识更新敏捷度", "性能与延迟"],
      "n_limitations": 2, "unresolved_node_ids": []}, 0.4),
    ({"type": _T.LLM_CALL.value, "step": "build_report_plan", "model": "glm-5.2", "ms": 35400,
      "in_tok": 900, "out_tok": 1200,
      "input": "【system】\n研究已停止、证据已冻结；规划报告结构与局限/后续研究提示。",
      "output": '{"sections":[{"heading":"成本结构差异","limitations":["后续可补充微调 GPU 训练成本"]}]}'}, 0.3),
    ({"type": _T.STEP_DONE.value, "step": "build_report_plan", "ms": 35400}, 0.1),
    ({"type": _T.CROSS_WORKER_AUDIT.value, "findings": True,
      "reason": "成本与延迟覆盖充分，但缺『混合架构(RAG+微调)』的企业落地证据",
      "conflicts": [{"dimension": "成本结论", "description": "一源称微调长期更省，另一源称 RAG 更省，口径不同"}]}, 0.9),
    ({"type": _T.LLM_CALL.value, "step": "cross_worker_audit", "model": "glm-5.2", "ms": 18000,
      "in_tok": 5400, "out_tok": 320,
      "input": "【user】\n对冻结的 6 条证据做跨 Worker 覆盖风险、冲突与局限审查。",
      "output": '{"has_findings":true,"reason":"覆盖风险已记录，交由 Writer 在局限中披露"}'}, 0.2),
    ({"type": _T.STEP_DONE.value, "step": "cross_worker_audit", "ms": 18000}, 0.1),
    ({"type": _T.WRITING.value, "n_evidence": 6, "n_groups": 3}, 1.0),
    ({"type": _T.LLM_CALL.value, "step": "write_report", "model": "deepseek-v4-pro", "ms": 89000,
      "in_tok": 12000, "out_tok": 4500,
      "input": "【system】\n你是研究报告写手，按证据综合并以 finding 作小标题。",
      "output": "# 微调 vs RAG（演示报告）\n\n## 执行摘要\n知识频繁更新选 RAG，行为深度定制选微调，主流是混合。"}, 0.4),
    ({"type": _T.STEP_DONE.value, "step": "write_report", "ms": 89000}, 0.1),
    ({"type": _T.SHAPE_GATE.value, "phase": "initial",
      "missing": ["执行摘要建议补一张对比表"]}, 0.5),
    ({"type": _T.REPORT_MD.value,
      "markdown": "# 微调 vs RAG（演示报告）\n\n## 执行摘要\n这是合成 demo，未真正检索。"
                  "知识频繁更新选 RAG，行为深度定制选微调，主流是混合。\n\n"
                  "## 成本结构\n- 微调：前期 GPU 投入高\n- RAG：推理期检索开销\n\n"
                  "## 对比一览\n"
                  "结构检查提示执行摘要需要补充对比表，以下为补充后的结果。\n\n"
                  "| 维度 | 微调 | RAG |\n"
                  "| --- | --- | --- |\n"
                  "| 知识更新 | 需重新训练 | 换检索源即可 |\n"
                  "| 前期成本 | GPU 训练开销高 | 检索/索引搭建 |\n"
                  "| 推理开销 | 低（权重内化） | 高（每次检索） |\n"
                  "| 适合场景 | 行为/风格深度定制 | 知识频繁更新 |\n",
      "saved_path": "runs/demo_run/report.md"}, 0.3),
    ({"type": _T.DONE.value, "status": "done", "n_evidence": 6,
      "completion_blockers": [], "warnings": ["cross_worker_audit_findings"],
      "unresolved_node_ids": []}, 0.0),
]

out = Path(__file__).resolve().parents[1] / "fixtures" / "demo_run" / "events.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
ts = 1_700_000_000.0
with open(out, "w", encoding="utf-8") as f:
    for i, (evt, gap) in enumerate(seq):
        ts += gap
        f.write(json.dumps({**evt, "ts": round(ts, 2), "seq": i}, ensure_ascii=False) + "\n")
    ts += 0.3
    f.write(json.dumps({"type": _T.END.value, "ts": round(ts, 2), "seq": len(seq)},
                       ensure_ascii=False) + "\n")
print(f"已生成 {out}（{len(seq) + 1} 条事件）")
