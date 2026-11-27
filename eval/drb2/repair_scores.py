"""Score adjudicated DRB-II annotations independently of the repair model."""

from __future__ import annotations

import argparse
from pathlib import Path
import random
from statistics import mean, quantiles

from eval.drb2.repair_experiment import ARMS, load_json, tasks_and_split, write_json

MIN_GAIN = 0.10
MAX_LOSS = 0.02
METRICS = ("support", "coverage", "synthesis_retention")


def paired_interval(differences, draws=10_000, seed=78):
    rng = random.Random(seed)
    n = len(differences)
    boot = [mean(rng.choices(differences, k=n)) for _ in range(draws)]
    points = quantiles(boot, n=40, method="inclusive")
    return {"n": n, "estimate": mean(differences), "low": points[0], "high": points[-1]}


def load_case(directory, task):
    key = load_json(directory / "arm_key.json")
    assessed = load_json(directory / "adjudicated.json")
    if assessed["idx"] != key["idx"] or assessed["reports"].keys() != key["reports"].keys():
        raise ValueError(f"{directory} 的标注与盲评报告清单不一致")
    rubric = {f"{dim}-{i}" for dim in ("info_recall", "analysis")
              for i, _ in enumerate(task["content"]["rubric"][dim], 1)}
    synthesis = assessed["baseline_synthesis_ids"].keys()
    result = {}
    for opaque, arm in key["reports"].items():
        report = assessed["reports"][opaque]
        claims = report["claims"]
        if any(type(row["supported"]) is not bool or not row["text"].strip() for row in claims):
            raise ValueError(f"{opaque} 的声明缺少正文或支持标签")
        covered = set(report["covered_rubric_ids"])
        retained = set(report["retained_synthesis_ids"])
        if covered - rubric or retained - synthesis:
            raise ValueError(f"{opaque} 标注引用了未冻结的题目要点或综合结论")
        supported = sum(row["supported"] for row in claims)
        result[arm] = {
            "support": supported / len(claims) if claims else 0.0,
            "coverage": len(covered) / len(rubric),
            "synthesis_retention": len(retained) / len(synthesis) if synthesis else None,
            "counts": {"supported": supported, "claims": len(claims), "covered": len(covered),
                       "rubric": len(rubric), "retained": len(retained), "synthesis": len(synthesis)},
        }
    return result


def comparisons(cases, draws=10_000):
    outputs = {}
    for control in ARMS[:-1]:
        intervals = {}
        for metric in METRICS:
            pairs = [(case["typed"][metric], case[control][metric]) for case in cases.values()]
            distances = [typed - base for typed, base in pairs if typed is not None and base is not None]
            intervals[metric] = paired_interval(distances, draws) if distances else None
        outputs[control] = intervals
    return outputs


def decision(results):
    for control in ("rebind", "minedit"):
        metrics = results[control]
        if metrics["support"]["high"] < MIN_GAIN:
            return "stop"
        if any(metrics[name] is not None and metrics[name]["high"] < -MAX_LOSS
               for name in ("coverage", "synthesis_retention")):
            return "stop"
    for control in ("rebind", "minedit"):
        metrics = results[control]
        support = metrics["support"]
        if support["estimate"] < MIN_GAIN or support["low"] <= 0:
            return "inconclusive"
        if any(metrics[name] is None or metrics[name]["low"] < -MAX_LOSS
               for name in ("coverage", "synthesis_retention")):
            return "inconclusive"
    return "continue_signal"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, choices=("development", "interim", "validation"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("annotations", nargs="+", type=Path)
    args = parser.parse_args(argv)
    tasks, split = tasks_and_split()
    cases = {}
    for directory in args.annotations:
        idx = load_json(directory / "arm_key.json")["idx"]
        if idx in cases or idx not in split[args.group]:
            parser.error(f"idx {idx} 重复或不属于 {args.group}")
        cases[idx] = load_case(directory, tasks[idx])
    if args.group != "development" and set(cases) != set(split[args.group]):
        parser.error(f"该阶段缺少锁定题的标注：{sorted(set(split[args.group]) - cases.keys())}")
    results = comparisons(cases)
    write_json(args.out, {"group": args.group, "per_report": cases, "typed_minus_control": results,
                          "decision": decision(results) if args.group == "validation" else "exploratory_only"})


if __name__ == "__main__":
    main()
