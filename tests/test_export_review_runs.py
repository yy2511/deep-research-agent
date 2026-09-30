from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.export_review_runs import export_runs


def test_export_review_runs_redacts_and_keeps_jsonl_valid(tmp_path):
    source = tmp_path / "runs"
    run_dir = source / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"query": "read /Users/demo/project"}),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "llm_call", "output": "key sk-" + "x" * 20 + "中文"})
        + "\n"
        + json.dumps({"type": "end", "message": "Bearer " + "a" * 24})
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text(
        "token github_pat_"
        + "z" * 20
        + "\npublic URL https://example.com/sk-hynix-and-samsung-crash",
        encoding="utf-8",
    )
    (run_dir / "ignored.bin").write_bytes(b"do not export")

    output = tmp_path / "review"
    manifest = export_runs(
        ["run-1"],
        source_root=source,
        output_root=output,
        home="/Users/demo",
    )

    combined = "\n".join(
        (output / "run-1" / filename).read_text(encoding="utf-8")
        for filename in ("meta.json", "events.jsonl", "report.md")
    )
    assert "/Users/demo" not in combined
    assert "sk-" + "x" * 20 not in combined
    assert "Bearer " + "a" * 24 not in combined
    assert "github_pat_" + "z" * 20 not in combined
    assert "https://example.com/sk-hynix-and-samsung-crash" in combined
    assert not (output / "run-1" / "ignored.bin").exists()
    assert manifest["runs"][0]["redactions"] == {
        "bearer_token": 1,
        "github_token": 1,
        "openai_key": 1,
        "user_home": 1,
    }
    for line in (output / "run-1" / "events.jsonl").read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_diagnostic_export_is_opt_in_allowlisted_and_redacted(tmp_path):
    source = tmp_path / "source"
    run = source / "trial"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("{}\n")
    (run / "requests.jsonl").write_text(json.dumps({"error": "Bearer " + "x" * 24}) + "\n")
    (run / "summary.json").write_text(json.dumps({"path": "/Users/demo/project"}))
    (run / "checkpoint.json").write_text('"never export"')
    (run / ".env").write_text('"never export"')
    output = tmp_path / "default"
    export_runs(["trial"], source_root=source, output_root=output)
    assert not (output / "trial/requests.jsonl").exists()
    output = tmp_path / "diagnostic"
    export_runs(
        ["trial"],
        source_root=source,
        output_root=output,
        include_diagnostics=True,
        home="/Users/demo",
    )
    assert "x" * 24 not in (output / "trial/requests.jsonl").read_text()
    assert "/Users/demo" not in (output / "trial/summary.json").read_text()
    assert {p.name for p in (output / "trial").iterdir()} == {
        "events.jsonl",
        "requests.jsonl",
        "summary.json",
    }
