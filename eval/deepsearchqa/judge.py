"""使用官方提示词评分；非 Gemini 2.5 Flash 的结果明确标注为非官方裁判。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time

from dra import llm
from dra.durable import atomic_json
from run_pilot import HERE, load_questions


def error_info(exc):
    """Useful diagnostics without logging URLs, credentials or arbitrary exception bodies."""
    chain, current = [], exc
    while current is not None:
        item = {'type': type(current).__name__}
        if getattr(current, 'status_code', None) is not None:
            item['http_status'] = current.status_code
        chain.append(item)
        current = current.__cause__
    return chain


def score_verdict(verdict):
    node = verdict["Answer Correctness"]
    details, extras = node["Correctness Details"], node["Excessive Answers"]
    if (not isinstance(details, dict) or not details
            or any(not isinstance(k, str) or type(v) is not bool for k, v in details.items())
            or not isinstance(extras, list) or any(not isinstance(v, str) for v in extras)
            or not isinstance(node.get("Explanation"), str)):
        raise ValueError("Invalid grader output")
    tp, fn, fp = sum(details.values()), len(details) - sum(details.values()), len(extras)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn)
    f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
    category = ("fully_correct" if fn == fp == 0 else "fully_incorrect" if tp == 0
                else "correct_with_extras" if fn == 0 else "partially_correct")
    return {"precision": precision, "recall": recall, "f1": f1, "category": category,
            "true_positives": tp, "false_negatives": fn, "false_positives": fp}


def grade_one(question, response, templates, *, model, provider, call=None):
    if not response.strip():
        return {"status": "empty_answer", "precision": 0.0, "recall": 0.0, "f1": 0.0,
                "category": "fully_incorrect"}
    prompt = templates["DEEPSEARCH_QA_PROMPT"] + templates["GRADER_RATING_OUTPUT_EXAMPLE"].format(
        prompt=question["problem"].strip(), prompt_type=question["answer_type"].strip(),
        answer=question["answer"].strip(), response=response.strip())
    call = call or llm.chat
    raw, errors = "", []
    meter = llm.UsageMeter()
    started = time.monotonic()
    with llm.execution_scope(meter=meter, deadline=started + 180):
        for _ in range(2):
            try:
                raw = call([{"role": "user", "content": prompt}], model=model, provider=provider,
                           reasoning=True, max_tokens=8192, max_retries=0, request_timeout_s=90,
                           on_chunk=lambda _: None)
                verdict = llm.extract_json(raw)
                scores = score_verdict(verdict)
                return {"status": "graded", **scores, "verdict": verdict, "raw_response": raw,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "usage": meter.snapshot(), "wall_seconds": round(time.monotonic() - started, 2)}
            except Exception as exc:
                errors.append(error_info(exc))
    return {"status": "judge_error", "errors": errors, "raw_response": raw,
            "usage": meter.snapshot()}


def summarize(records, grades):
    count = len(records)
    missing = [r["id"] for r in records if "f1" not in grades.get(str(r["id"]), {})]
    return {
        "sample_size": count, "answers_produced": sum(bool(r["response"]) for r in records),
        "judge_errors_or_pending": missing,
        # 裁判故障不静默移出分母；有缺失就不发布完整批次质量分。
        "mean_f1": None if missing else statistics.mean(grades[str(r["id"])]["f1"] for r in records),
        "fully_correct_rate": None if missing else sum(
            grades[str(r["id"])]["category"] == "fully_correct" for r in records) / count,
        "median_wall_seconds": statistics.median(r["wall_seconds"] for r in records),
        "total_searches": sum(r["searches"] for r in records),
        "total_input_tokens": sum(v.get("input", 0) for r in records for v in r.get("usage", {}).values()),
        "total_output_tokens": sum(v.get("output", 0) for r in records for v in r.get("usage", {}).values()),
        "missing_usage_responses": sum(v.get("missing_usage", 0) for r in records for v in r.get("usage", {}).values()),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", default="pilot-20261002")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args(argv)
    batch = HERE / "runs" / args.batch
    manifest = json.loads((batch / "manifest.json").read_text())
    expected = manifest["protocol"]["questions"]
    pending = [q["id"] for q in expected if not (batch / str(q["id"]) / "result.json").exists()]
    if pending:
        raise SystemExit(f"Research unfinished: {pending}")
    records = [json.loads((batch / str(q["id"]) / "result.json").read_text()) for q in expected]
    from prepare import NOTEBOOK_SHA256, extract_prompt
    notebook = (HERE / ".official/starter.ipynb").read_text()
    if hashlib.sha256(notebook.encode()).hexdigest() != NOTEBOOK_SHA256:
        raise SystemExit("Official notebook checksum mismatch")
    templates = extract_prompt(notebook)
    identity = {"model": args.model, "provider": args.provider,
                "official_judge_model": args.model == "gemini-2.5-flash",
                "notebook_sha256": NOTEBOOK_SHA256}
    print(json.dumps({**identity, "count": len(records), "run_live": args.run_live}), flush=True)
    if not args.run_live:
        return
    target = batch / "judge"
    target.mkdir(exist_ok=True)
    path = target / "results.json"
    previous = json.loads(path.read_text()) if path.exists() else {"judge": identity, "grades": {}}
    if previous["judge"] != identity:
        raise SystemExit("Judge configuration differs from existing results")
    questions = {q["id"]: q for q in load_questions()}
    for record in records:
        qid = str(record["id"])
        if qid in previous["grades"]:
            if previous["grades"][qid].get("response_sha256") != hashlib.sha256(record["response"].encode()).hexdigest():
                raise SystemExit(f"Answer changed after grading: {qid}; preserve original batch")
            continue
        grade = grade_one(questions[record["id"]], record["response"], templates,
                          model=args.model, provider=args.provider)
        grade["response_sha256"] = hashlib.sha256(record["response"].encode()).hexdigest()
        previous["grades"][qid] = grade
        atomic_json(path, previous)
        print(json.dumps({"id": qid, **{k: grade.get(k) for k in ("status", "f1", "category")}}), flush=True)
    summary = {"judge": identity, **summarize(records, previous["grades"])}
    atomic_json(batch / "summary.json", summary)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
