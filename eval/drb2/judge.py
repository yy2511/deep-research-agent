"""用 DRB2 官方评分代码给 run_pilot.py 产出的报告打分。

    uv run python eval/drb2/judge.py --batch 20260929-pilot-2             # 只检查，不花钱
    uv run python eval/drb2/judge.py --batch 20260929-pilot-2 --run-live  # 真打分：调用 GPT-5.5

评分提示词、分批、结果校验和算分全部用官方仓库（固定到 ``OFFICIAL_COMMIT``）的原代码，
本脚本只负责：取官方代码、把报告放成官方要求的目录结构、传入评分模型配置、汇总结果。
与官方的两处差别，都不影响分数：请求改为流式（见 official_runner.py），每批最多重试 3 次（官方 10 次）。
官方代码 Apache-2.0，仓库 https://github.com/imlrz/DeepResearch-Bench-II 。

评分模型从项目 .env 读取：``DRB2_JUDGE_BASE_URL``、``DRB2_JUDGE_API_KEY``、
``DRB2_JUDGE_MODEL``（默认 gpt-5.5）。密钥只通过环境变量传给子进程，不出现在命令行和输出里。

输出在 ``runs/<批次>/judge/``：``result.jsonl``（官方原始逐条结果）、``scores.json``（各题各类得分）、
``scores.md``（得分表和没拿到分的检查项，便于人工复核）、``run_evaluation.log``（官方日志）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OFFICIAL = HERE / ".official"
OFFICIAL_REPO = "https://github.com/imlrz/DeepResearch-Bench-II"
OFFICIAL_COMMIT = "b38f360603db9531b102aef8c166cedb8509b6f6"
TASKS = HERE / "data" / "tasks.jsonl"
DIMS = [("info_recall", "inforecall", "信息召回"), ("analysis", "analysis", "分析"),
        ("presentation", "presentation", "表达")]


def ensure_official() -> None:
    """取官方代码并切到固定版本；已有且版本对就不动。"""
    if not (OFFICIAL / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", OFFICIAL_REPO, str(OFFICIAL)], check=True)
    head = subprocess.run(["git", "-C", str(OFFICIAL), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != OFFICIAL_COMMIT:
        subprocess.run(["git", "-C", str(OFFICIAL), "fetch", "--quiet", "origin"], check=True)
        subprocess.run(["git", "-C", str(OFFICIAL), "checkout", "--quiet", OFFICIAL_COMMIT], check=True)


def judge_env() -> dict:
    values = dotenv_values(ROOT / ".env")
    base, key = values.get("DRB2_JUDGE_BASE_URL"), values.get("DRB2_JUDGE_API_KEY")
    if not base or not key:
        raise SystemExit("缺少 DRB2_JUDGE_BASE_URL / DRB2_JUDGE_API_KEY，请在项目 .env 中配置")
    return {**os.environ,
            "OPENAI_API_URL": base.rstrip("/") + "/chat/completions",
            "OPENAI_API_KEY": key,
            "OPENAI_MODEL": values.get("DRB2_JUDGE_MODEL") or "gpt-5.5"}


def prepare_input(batch_dir: Path, label: str) -> list[int]:
    """官方要求 <输入目录>/<被测产品名>/idx-<n>.md。"""
    target = batch_dir / "judge" / "input" / label
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    indexes = []
    for report in sorted(batch_dir.glob("idx-*/report.md")):
        idx = int(report.parent.name.removeprefix("idx-"))
        shutil.copy(report, target / f"idx-{idx}.md")
        indexes.append(idx)
    return indexes


def summarize(result_path: Path, out_dir: Path) -> dict:
    sys.path.insert(0, str(OFFICIAL))
    from aggregate_scores import compute_dimension_averages  # 官方算分

    tasks = {row["idx"]: row for row in map(json.loads, TASKS.read_text(encoding="utf-8").splitlines())}
    summary, lines = {}, ["# DRB2 评分结果", ""]
    for row in map(json.loads, result_path.read_text(encoding="utf-8").splitlines()):
        idx, result = row["idx"], row["result"]
        if "error" in result:
            summary[idx] = {"error": result["error"]}
            lines += [f"## idx {idx}：评分失败", "", result["error"], ""]
            continue
        scores = compute_dimension_averages(result)
        counts = {}
        for key, _, name in DIMS:
            values = [v.get("score") for v in result["scores"].get(key, {}).values()]
            counts[key] = {"得分": values.count(1), "没提到": values.count(0), "用了屏蔽原文": values.count(-1),
                           "总数": len(values), "题目条数": len(tasks[idx]["content"]["rubric"].get(key, []))}
        summary[idx] = {"scores": scores, "counts": counts, "usage": result.get("usage_summary")}
        lines += [f"## idx {idx}（{tasks[idx]['theme']}）", "",
                  f"总分 **{scores['total']:.1%}**，用了屏蔽原文 {scores['blocked_rate']:.1%}", "",
                  "| 类别 | 得分 | 拿到 / 总数 |", "|---|---|---|"]
        for key, out_key, name in DIMS:
            c = counts[key]
            lines.append(f"| {name} | {scores[out_key]:.1%} | {c['得分']} / {c['总数']} |")
        lines += ["", "没拿到分的检查项：", ""]
        for key, _, name in DIMS:
            for item, verdict in result["scores"].get(key, {}).items():
                if verdict.get("score") != 1:
                    lines.append(f"- [{name}] {item}  \n  评分理由：{verdict.get('reason', '')}")
        lines.append("")
    (out_dir / "scores.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "scores.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--batch", required=True, help="runs/ 下的批次目录名")
    p.add_argument("--label", default="research-workbench", help="被测产品名（官方目录结构需要）")
    p.add_argument("--run-live", action="store_true", help="真打分（调用评分模型）")
    args = p.parse_args(argv)
    batch_dir = HERE / "runs" / args.batch
    if not batch_dir.is_dir():
        raise SystemExit(f"批次不存在：{batch_dir}")
    ensure_official()
    env = judge_env()
    indexes = prepare_input(batch_dir, args.label)
    print(f"待评分：idx {indexes}；评分模型 {env['OPENAI_MODEL']}；官方代码 {OFFICIAL_COMMIT[:8]}", file=sys.stderr)
    if not args.run_live:
        print("未加 --run-live，不评分。", file=sys.stderr)
        return
    out_dir = batch_dir / "judge"
    result_path = out_dir / "result.jsonl"
    result_path.unlink(missing_ok=True)  # 官方脚本是追加写，先清掉旧结果
    (out_dir / "run_evaluation.log").unlink(missing_ok=True)
    subprocess.run([sys.executable, str(HERE / "official_runner.py"),
                    "--pdf_dir", str(out_dir / "input"), "--out_jsonl", str(result_path),
                    "--tasks_jsonl", str(TASKS), "--log_file", str(out_dir / "run_evaluation.log"),
                    "--max_retries", "3"],
                   cwd=OFFICIAL, env=env, check=True)
    if not result_path.exists():
        raise SystemExit("官方脚本没有产出结果，见 run_evaluation.log")
    for idx, item in sorted(summarize(result_path, out_dir).items()):
        print(json.dumps({"idx": idx, **({"error": item["error"]} if "error" in item else item["scores"])},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
