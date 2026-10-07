"""固定历史未满分题，比较冻结的三套提示词；研究引擎与评分口径保持一致。"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys

# 冻结源码使用原项目的数据与凭据位置，不复制密钥进实验目录。
from dra import paths
if os.environ.get("DRA_COMPARISON_PROJECT"):
    paths.PROJECT_ROOT = Path(os.environ["DRA_COMPARISON_PROJECT"])
from dra import llm, provider_registry, research_prompts, simple_research, tools as web_tools
llm._ENV_PATH = paths.PROJECT_ROOT / ".env"
web_tools._ENV_PATH = paths.PROJECT_ROOT / ".env"
from dra.durable import atomic_json
from run_pilot import HERE, load_questions, run_one
from prepare import NOTEBOOK_SHA256, extract_prompt
import regrade_originals


ARMS = ("original", "claude", "v2")
LEGACY = "runs/concise-v2-20261003/code-snapshots/20261002T165256Z/src/dra/simple_research.py"
CLAUDE_COMMIT = "82b755ea67c221dac5979bdbeccfe59305768f81"
FROZEN_BASELINE = HERE / "experiments/hard50-20261004"


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def select_questions(records, grades, seed):
    ids = sorted(r["id"] for r in records if grades[str(r["id"])]["f1"] < 1)
    random.Random(seed).shuffle(ids)
    return ids


def import_snapshot(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prompt_bundle(module, profile, *, modern):
    systems = {}
    tools = {}
    for role in ("lead", "subagent"):
        if modern:
            normal = module._lead_system(delegation=True, execution=False) if role == "lead" else module._SUBAGENT_SYSTEM
            wrapup = module._wrapup_system(role=role, delegation=True)
            steering = module._steering_instructions(delegation=True) if role == "lead" else ""
            wrap_steering = module._WRAPUP_STEERING_INSTRUCTIONS if role == "lead" else ""
            wrap_evidence = module._WRAPUP_EVIDENCE_INSTRUCTIONS
            schemas = module.LEAD_TOOL_SCHEMAS if role == "lead" else module.SUBAGENT_TOOL_SCHEMAS
        else:
            normal = module._LEAD_SYSTEM if role == "lead" else module._SUBAGENT_SYSTEM
            wrapup = module._WRAPUP_SYSTEM
            steering = wrap_steering = module._USER_STEERING_INSTRUCTIONS
            wrap_evidence = module._EVIDENCE_USE_INSTRUCTIONS
            schemas = module.LEAD_TOOL_SCHEMAS if role == "lead" else module.TOOL_SCHEMAS
        systems[role] = {
            "normal": research_prompts.system_prompt(profile, role, normal,
                evidence=module._EVIDENCE_USE_INSTRUCTIONS, steering=steering),
            "wrapup": research_prompts.system_prompt(profile, role, wrapup,
                evidence=wrap_evidence, steering=wrap_steering, wrapup=True),
        }
        tools[role] = {s["function"]["name"]: s for s in research_prompts.tool_schemas(profile, schemas, role)}
    return {"profile": profile, "systems": systems, "tools": tools}


@contextmanager
def installed_bundle(bundle):
    original_system, original_tools = research_prompts.system_prompt, research_prompts.tool_schemas

    def frozen_system(profile, role, product_prompt, *, evidence, steering, wrapup=False, delegation=True):
        return bundle["systems"][role]["wrapup" if wrapup else "normal"]

    def frozen_tools(profile, schemas, role):
        # 保留当前阶段可用工具集合，替换为冻结的模型可见定义；收尾仍只有交稿工具。
        return [deepcopy(bundle["tools"][role][s["function"]["name"]]) for s in schemas]

    research_prompts.system_prompt, research_prompts.tool_schemas = frozen_system, frozen_tools
    try:
        yield
    finally:
        research_prompts.system_prompt, research_prompts.tool_schemas = original_system, original_tools


def prepare(target, source, *, size, seed, provider, model, workers):
    source_manifest = read(source / "manifest.json")
    grades = read(source / "results.json")["grades"]
    order = select_questions(source_manifest["records"], grades, seed)
    selected = order[:size]
    questions = {q["id"]: q for q in load_questions()}
    records = {r["id"]: r for r in source_manifest["records"]}
    target.mkdir(parents=True)
    originals = target / "prompt-sources"
    originals.mkdir()
    legacy_path = originals / "legacy_simple_research.py"
    legacy_path.write_bytes((HERE / LEGACY).read_bytes())
    claude_path = originals / "claude_simple_research.py"
    claude_path.write_bytes(subprocess.check_output(
        ["git", "show", f"{CLAUDE_COMMIT}:src/dra/simple_research.py"], cwd=paths.PROJECT_ROOT))
    # Reusing a frozen experiment must not rebuild old V2 with today's profile code.
    baseline = read(FROZEN_BASELINE / "experiment.json")
    bundles = {}
    (target / "prompts").mkdir()
    for arm in ARMS:
        path = FROZEN_BASELINE / "prompts" / f"{arm}.json"
        data = path.read_bytes()
        assert sha(data) == baseline["arms"][arm]["prompt_sha256"]
        bundles[arm] = json.loads(data)
        (target / "prompts" / f"{arm}.json").write_bytes(data)
    # 今天的未提交提示词另行归档，三臂比较不掺入它。
    atomic_json(target / "prompts/current-product-reference-only.json",
                prompt_bundle(simple_research, "product", modern=True))
    config = read(HERE / "runs/concise-v2-20261003/manifest.json")["protocol"]["config"]
    config["adapter"].update(model=model, provider=provider, reasoning=True, effort="low")
    config["prompt_profile"] = "product"
    config["evidence_judge"] = {**config.get("evidence_judge", {}), "enabled": False}
    config["compaction_enabled"] = False
    runtime = target / "runtime"
    shutil.copytree(paths.PROJECT_ROOT / "src/dra", runtime / "src/dra",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("prompt_comparison.py", "run_pilot.py", "prepare.py", "judge.py", "regrade_originals.py"):
        dest = runtime / "eval/deepsearchqa" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / name, dest)
    for name in ("data/DSQA-full.csv", "data/provenance.json", ".official/starter.ipynb"):
        dest = runtime / "eval/deepsearchqa" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / name, dest)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(paths.PROJECT_ROOT / name, runtime / name)
    hashes = {str(f.relative_to(runtime)): sha(f.read_bytes()) for f in runtime.rglob("*") if f.is_file()}
    selection = {"source_batch": str(source.resolve()), "source_results_sha256": sha((source / "results.json").read_bytes()),
                 "rule": "Latest Gemini full-original-response F1 < 1; include empty answers and failures",
                 "seed": seed, "pool_size": len(order), "pool_order": order,
                 "selected_ids": selected, "extension100_ids": order[:100],
                 "not_representative_of_full_benchmark": True,
                 "items": [{"id": qid, "previous_f1": grades[str(qid)]["f1"],
                            "previous_profile": records[qid].get("prompt_profile", "product"),
                            "original_response_empty": not records[qid]["response"].strip()}
                           for qid in order]}
    atomic_json(target / "selection.json", selection)
    # 研究 worker 只接收以下三项，不接收旧答案、历史分数或 gold。
    exposed = [{k: questions[qid][k] for k in ("id", "problem", "problem_category")} for qid in selected]
    atomic_json(target / "questions.json", exposed)
    jobs = []
    for i, q in enumerate(exposed):
        for arm in ARMS[i % 3:] + ARMS[:i % 3]:
            jobs.append({"arm": arm, "question": q})
    atomic_json(target / "jobs.json", jobs)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "size": size, "workers": workers,
                "research_config": config, "research_connection": {
                    k: v for k, v in provider_registry.status(provider).model_dump().items()
                    if k in ("name", "label", "base_url", "reasoning_format")},
                "judge": source_manifest["judge"], "notebook_sha256": NOTEBOOK_SHA256,
                "arms": {arm: {"profile": bundle["profile"],
                               "prompt_sha256": sha((target / "prompts" / f"{arm}.json").read_bytes()),
                               "source": CLAUDE_COMMIT if arm == "claude" else LEGACY}
                         for arm, bundle in bundles.items()},
                "runtime_sha256": hashes,
                "scope": "System prompts (lead/subagent/normal/wrapup) and tool schemas vary. Engine, receipts, mission rendering, evidence judge, model and budgets are shared. Not a full historical-engine replay.",
                "preflight_planning": "none", "answer_preprocessing": "none",
                "reuse_historical_answers": False, "run_live_authorized": False}
    atomic_json(target / "manifest.json", manifest)
    save_summary(target)
    return manifest


def worker(target_string, job):
    target = Path(target_string)
    manifest = read(target / "manifest.json")
    arm, question = job["arm"], job["question"]
    bundle = read(target / "prompts" / f"{arm}.json")
    config_data = deepcopy(manifest["research_config"])
    config_data["prompt_profile"] = bundle["profile"]
    config = simple_research.SimpleConfig.model_validate(config_data)
    arm_dir = target / "arms" / arm
    result_path = arm_dir / "research" / str(question["id"]) / "result.json"
    if result_path.exists():
        record = read(result_path)
    else:
        with installed_bundle(bundle):
            record = run_one(question, result_path.parent, config)
        record.update(comparison_arm=arm, frozen_prompt_sha256=manifest["arms"][arm]["prompt_sha256"])
        atomic_json(result_path, record)
    grade_path = arm_dir / "judge" / str(question["id"]) / "result.json"
    grade = read(grade_path) if grade_path.exists() else {}
    if "f1" not in grade:
        notebook = (HERE / ".official/starter.ipynb").read_text()
        assert sha(notebook) == NOTEBOOK_SHA256
        # 标准答案只在研究结束之后交给独立裁判。
        gold = next(q for q in load_questions() if q["id"] == question["id"])
        key, _ = provider_registry.credentials(manifest["judge"]["provider"])
        import httpx
        with httpx.Client(headers={"x-goog-api-key": key}, timeout=90) as client:
            grade = regrade_originals.grade_one(record, gold, extract_prompt(notebook),
                      arm_dir / "judge", manifest["judge"]["endpoint"], client)
    return {"id": question["id"], "arm": arm, "research_status": record["status"],
            "judge_status": grade["status"], "f1": grade.get("f1"), "wall_seconds": record["wall_seconds"]}


def save_summary(target):
    manifest = read(target / "manifest.json")
    questions = read(target / "questions.json")
    summaries = {}
    for arm in ARMS:
        rows, grades = [], {}
        for q in questions:
            p = target / "arms" / arm / "research" / str(q["id"]) / "result.json"
            if p.exists():
                rows.append(read(p))
            p = target / "arms" / arm / "judge" / str(q["id"]) / "result.json"
            if p.exists():
                grades[str(q["id"])] = read(p)
        scored = {k: g for k, g in grades.items() if "f1" in g}
        complete = len(scored) == manifest["size"]
        summaries[arm] = {"expected": manifest["size"], "research_finished": len(rows),
            "graded": len(scored), "mean_f1": sum(g["f1"] for g in scored.values()) / manifest["size"] if complete else None,
            "fully_correct_rate": sum(g["category"] == "fully_correct" for g in scored.values()) / manifest["size"] if complete else None,
            "empty_raw_answers": sum(not r["response"].strip() for r in rows),
            "judge_errors": [int(k) for k,g in grades.items() if "f1" not in g],
            "stop_reasons": dict(Counter(r["stop_reason"] if "stop_reason" in r else r["status"] for r in rows)),
            "total_searches": sum(r["searches"] for r in rows),
            "total_research_seconds": sum(r["wall_seconds"] for r in rows)}
    result = {"updated_at": datetime.now(timezone.utc).isoformat(), "arms": summaries,
              "completed": all(v["graded"] == manifest["size"] for v in summaries.values())}
    atomic_json(target / "summary.json", result)
    return result


def run(target):
    manifest = read(target / "manifest.json")
    manifest["run_live_authorized"] = True
    atomic_json(target / "manifest.json", manifest)
    jobs = read(target / "jobs.json")
    pending = []
    for job in jobs:
        p = target / "arms" / job["arm"] / "judge" / str(job["question"]["id"]) / "result.json"
        if not p.exists() or "f1" not in read(p):
            pending.append(job)
    with ProcessPoolExecutor(max_workers=manifest["workers"]) as pool:
        futures = {pool.submit(worker, str(target), job): job for job in pending}
        for future in as_completed(futures):
            print(json.dumps(future.result(), ensure_ascii=False), flush=True)
            save_summary(target)
    print(json.dumps(save_summary(target), ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args()
    target = args.experiment.resolve()
    if args.action == "prepare":
        result = prepare(target, args.source.resolve(), size=args.size, seed=args.seed,
                         provider=args.provider, model=args.model, workers=args.workers)
        print(json.dumps({"experiment": str(target), "size": result["size"], "arms": list(result["arms"])}))
    elif args.run_live:
        run(target)
    else:
        print(json.dumps(save_summary(target), ensure_ascii=False))


if __name__ == "__main__":
    main()
