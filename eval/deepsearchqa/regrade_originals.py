"""并发重评已完成题的完整原始回答，独立保存 Gemini 原生接口评分。"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import time

import httpx

from dra import provider_registry
from dra.durable import atomic_json
from judge import error_info, score_verdict
from prepare import NOTEBOOK_SHA256, extract_prompt
from run_pilot import HERE, load_questions


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_verdict(text):
    # 与冻结的官方 notebook 相同：仅拆除裁判输出的 JSON 代码围栏。
    text = text.strip()
    marker = text.find("```json")
    if marker != -1:
        text = text[marker + len("```json"):].strip()
        end = text.rfind("```")
        if end != -1:
            text = text[:end].strip()
    return json.loads(text)


def grade_one(record, question, templates, target, endpoint, client):
    folder = target / str(record["id"])
    folder.mkdir(parents=True, exist_ok=True)
    response = record["response"]
    (folder / "original-answer.md").write_text(response)
    common = {"id": record["id"], "response_sha256": sha(response), "normalization": "none"}
    if not response.strip():
        result = {**common, "status": "empty_answer", "precision": 0.0, "recall": 0.0,
                  "f1": 0.0, "category": "fully_incorrect", "attempts": 0}
        atomic_json(folder / "result.json", result)
        return result
    prompt = templates["DEEPSEARCH_QA_PROMPT"] + templates["GRADER_RATING_OUTPUT_EXAMPLE"].format(
        prompt=question["problem"].strip(), prompt_type=question["answer_type"].strip(),
        answer=question["answer"].strip(), response=response.strip())
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    atomic_json(folder / "request.json", {"endpoint": endpoint, "body": body})
    common["prompt_sha256"] = sha(prompt)
    started = time.monotonic()
    errors = []
    # 沿用现有 judge.py 的最多两次尝试；每次回执独立留存，续跑不覆盖。
    offset = len(list(folder.glob("attempt-*")))
    for number in range(offset + 1, offset + 3):
        attempt = folder / f"attempt-{number}"
        attempt.mkdir()
        receipt = {"started_at": now()}
        try:
            res = client.post(endpoint, json=body)
            receipt.update(http_status=res.status_code, finished_at=now(),
                           response_headers={k: v for k, v in res.headers.items()
                                             if k.lower() in ("x-request-id", "x-oneapi-request-id", "date")})
            (attempt / "http-response.txt").write_text(res.text)
            atomic_json(attempt / "receipt.json", receipt)
            res.raise_for_status()
            payload = res.json()
            candidate = payload["candidates"][0]
            raw = "".join(part.get("text", "") for part in candidate["content"]["parts"]
                          if not part.get("thought"))
            verdict = parse_verdict(raw)
            result = {**common, "status": "graded", **score_verdict(verdict),
                      "verdict": verdict, "raw_response": raw,
                      "model_version": payload.get("modelVersion"),
                      "response_id": payload.get("responseId"),
                      "finish_reason": candidate.get("finishReason"),
                      "usage": payload.get("usageMetadata", {}),
                      "attempts": number, "errors": errors, **receipt,
                      "wall_seconds": round(time.monotonic() - started, 3)}
            atomic_json(attempt / "result.json", result)
            atomic_json(folder / "result.json", result)
            return result
        except Exception as exc:
            error = {"attempt": number, "errors": error_info(exc), **receipt}
            errors.append(error)
            atomic_json(attempt / "error.json", error)
    result = {**common, "status": "judge_error", "errors": errors, "attempts": number,
              "wall_seconds": round(time.monotonic() - started, 3)}
    atomic_json(folder / "result.json", result)
    return result


def metrics(records, grades):
    missing = [r["id"] for r in records if "f1" not in grades.get(str(r["id"]), {})]
    scored = [grades[str(r["id"])] for r in records if r["id"] not in missing]
    return {"sample_size": len(records), "scored": len(scored),
            "answers_produced": sum(bool(r["response"].strip()) for r in records),
            "judge_errors_or_pending": missing,
            "mean_f1": statistics.mean(g["f1"] for g in scored) if not missing else None,
            "fully_correct_rate": sum(g["category"] == "fully_correct" for g in scored) / len(records)
            if not missing else None,
            "categories": dict(Counter(g["category"] for g in scored))}


def save_progress(target, snapshot, grades):
    records = snapshot["records"]
    result = {"updated_at": now(), "judge": snapshot["judge"], **metrics(records, grades),
              "expected_questions": snapshot["expected_questions"],
              "research_complete": len(records) == snapshot["expected_questions"],
              "cohorts": {profile: metrics([r for r in records if r.get("prompt_profile", "product") == profile], grades)
                          for profile in sorted({r.get("prompt_profile", "product") for r in records})}}
    paired = [(snapshot["old_grades"][qid], new) for qid, new in grades.items()
              if "f1" in new and "f1" in snapshot["old_grades"].get(qid, {})]
    if paired:
        result["comparison"] = {"paired_count": len(paired),
                                "old_judge": snapshot["old_judge"],
                                "old_mean_f1": statistics.mean(old["f1"] for old, _ in paired),
                                "new_mean_f1": statistics.mean(new["f1"] for _, new in paired),
                                "changed": sum(old["f1"] != new["f1"] for old, new in paired),
                                "note": "Judge and answer preprocessing both changed; not a harness improvement estimate."}
    atomic_json(target / "results.json", {"judge": snapshot["judge"], "grades": grades})
    atomic_json(target / "summary.json", result)
    return result


def run_batch(target, snapshot, questions, templates, client, workers):
    grades = {}
    pending = []
    for record in snapshot["records"]:
        path = target / str(record["id"]) / "result.json"
        prior = json.loads(path.read_text()) if path.exists() else {}
        if "f1" in prior:
            grades[str(record["id"])] = prior
        else:
            pending.append(record)
    save_progress(target, snapshot, grades)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(grade_one, r, questions[r["id"]], templates, target,
                               snapshot["judge"]["endpoint"], client): r["id"] for r in pending}
        for future in as_completed(futures):
            qid = futures[future]
            grades[str(qid)] = future.result()
            summary = save_progress(target, snapshot, grades)
            print(json.dumps({"id": qid, "status": grades[str(qid)]["status"],
                              "finished": len(grades), "total": len(snapshot["records"]),
                              "scored": summary["scored"]}), flush=True)
    return save_progress(target, snapshot, grades)


def freeze(source, target, provider, model, workers):
    manifest = json.loads((source / "manifest.json").read_text())
    paths = [source / str(q["id"]) / "result.json" for q in manifest["protocol"]["questions"]]
    records = [json.loads(path.read_text()) for path in paths if path.exists()]
    old = json.loads((source / "judge/results.json").read_text())
    public = provider_registry.status(provider)
    root = public.base_url.rstrip("/").removesuffix("/v1")
    snapshot = {"created_at": now(), "source_batch": str(source.resolve()), "workers": workers,
                "source_result_hashes": {str(path.parent.name): sha(path.read_text())
                                         for path in paths if path.exists()},
                "expected_questions": len(paths), "records": records,
                "old_judge": old["judge"], "old_grades": old["grades"],
                "judge": {"model": model, "provider": provider, "provider_label": public.label,
                          "endpoint": f"{root}/v1beta/models/{model}:generateContent",
                          "transport": "Gemini generateContent", "normalization": "none",
                          "source_field": "result.json:response (complete delivered answer)",
                          "generation_config": "omitted, as in the official notebook",
                          "model_identity": "requested model ID; upstream identity is provider-reported",
                          "notebook_sha256": NOTEBOOK_SHA256,
                          "request_timeout_seconds": 90, "attempts_per_invocation": 2}}
    target.mkdir(parents=True)
    atomic_json(target / "manifest.json", snapshot)
    (target / "runner.py").write_text(Path(__file__).read_text())
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-batch", required=True)
    parser.add_argument("--output-batch", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args(argv)
    target = HERE / "runs" / args.output_batch
    snapshot = (json.loads((target / "manifest.json").read_text()) if target.exists()
                else freeze(HERE / "runs" / args.source_batch, target, args.provider, args.model, args.workers))
    print(json.dumps({"output": str(target), "count": len(snapshot["records"]),
                      "judge": snapshot["judge"], "run_live": args.run_live}), flush=True)
    if not args.run_live:
        return
    notebook = (HERE / ".official/starter.ipynb").read_text()
    if sha(notebook) != NOTEBOOK_SHA256:
        raise SystemExit("Official notebook checksum mismatch")
    templates = extract_prompt(notebook)
    questions = {q["id"]: q for q in load_questions()}
    key, _ = provider_registry.credentials(snapshot["judge"]["provider"])
    with httpx.Client(headers={"x-goog-api-key": key}, timeout=90) as client:
        result = run_batch(target, snapshot, questions, templates, client, snapshot["workers"])
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
