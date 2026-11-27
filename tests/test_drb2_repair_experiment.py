from collections import Counter
from hashlib import sha256
import json

import pytest

from eval.drb2.repair_experiment import ARMS, compare, export_blind, freeze_run, tasks_and_split, write_json
from eval.drb2.repair_scores import comparisons, decision, load_case


def test_lock_is_disjoint_balanced_and_keeps_used_tasks_in_development():
    tasks, split = tasks_and_split()
    dev, validation = split["development"], split["validation"]
    assert {15, 43} <= set(dev)
    assert len(set(dev)) == 6 and len(set(validation)) == 36
    assert not set(dev) & set(validation)
    assert not set(split["excluded"]) & set(dev + validation)
    assert Counter(tasks[idx]["language"] for idx in validation) == {"en": 18, "zh": 18}
    assert Counter(tasks[idx]["language"] for idx in split["interim"]) == {"en": 9, "zh": 9}


def test_freeze_preserves_only_verified_visible_spans(tmp_path):
    task = {"idx": 15, "content": {"task": "What happened?", "rubric": {
        "info_recall": ["Mention the outcome"], "analysis": ["Explain it"]}}}
    raw = "read\r\nseen\u2028never opened"
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources/s1.txt").write_bytes(raw.encode())
    ranges = [{"location": "chars=0-4", "quote": "read"},
              {"location": "chars=6-10", "quote": "seen"}]
    write_json(tmp_path / "run.json", {
        "run_id": "fixture", "query": task["content"]["task"], "actions": [],
        "sources": [{"ref": "s1", "title": "Source", "url": "https://example.com/",
                     "snapshot_kind": "full", "content_sha256": sha256(raw.encode()).hexdigest()}],
        "cards": [{"card_id": "c1", "quotes": [{"source_ref": "s1", **ranges[0]}]}],
        "final": {"draft": "An outcome[c1].", "delivery_status": "delivered"},
    })
    visible = {"source_ref": "s1", "excerpts": ranges}
    unviewed = {"source_ref": "s1", "excerpts": [{"location": "chars=11-23", "quote": "never opened"}]}
    rows = [
        {"tool": "read_source", "phase": "done", "status": "ok", "result": json.dumps(visible)},
        {"tool": "read_source", "phase": "done", "status": "failed", "result": unviewed},
        {"tool": "search_sources", "phase": "done", "status": "ok", "result": unviewed},
    ]
    (tmp_path / "tool_calls.jsonl").write_text("\n".join(map(json.dumps, rows)))
    frozen = freeze_run(tmp_path, task)
    assert [p["text"] for p in frozen["passages"]] == ["read", "seen"]
    assert frozen["cards"]["c1"] == [{"passage": "p1", "start": 0, "end": 4, "text": "read"}]
    state = json.loads((tmp_path / "run.json").read_text())
    state["actions"] = [{"action": "context_tool_page"}]
    write_json(tmp_path / "run.json", state)
    with pytest.raises(ValueError, match="分页"):
        freeze_run(tmp_path, task)


def test_four_arms_share_inputs_and_blind_scoring(tmp_path):
    frozen = {"idx": 15, "question": "What happened?", "draft": "It worked[c1].",
              "cards": {"c1": []}, "passages": [{"id": "p1"}], "rubric": {"analysis-1": "gold"}}
    calls = {}

    def offline(arm, system, user):
        calls[arm] = json.loads(user)
        assert "gold" not in user
        return {
            "diagnostic": '{"claims":[{"claim":"It worked","status":"citation_gap"}]}',
            "rebind": '{"edits":[{"line":1,"text":"It worked[p1]."}]}', "minedit": "It worked.", "typed": "It worked[p1].",
        }[arm]

    reports = compare(frozen, offline)
    assert set(reports) == set(ARMS)
    assert calls["rebind"] == calls["minedit"] == calls["typed"]
    assert calls["rebind"]["material"] == calls["diagnostic"]
    assert reports["original"] == frozen["draft"]
    output = tmp_path / "annotations"
    export_blind(frozen, reports, output, seed=78)
    key = json.loads((output / "arm_key.json").read_text())
    assert set(key["reports"].values()) == set(ARMS)
    assert not (output / "blind" / "arm_key.json").exists()
    labels = {}
    for opaque, arm in key["reports"].items():
        assert (output / "blind" / f"{opaque}.md").read_text() == reports[arm]
        labels[opaque] = {"claims": [{"supported": arm == "typed", "text": "It worked"}],
                          "covered_rubric_ids": ["analysis-1"], "retained_synthesis_ids": ["s1"]}
    write_json(output / "adjudicated.json", {
        "idx": 15, "baseline_synthesis_ids": {"s1": "It worked"}, "reports": labels,
    })
    task = {"content": {"rubric": {"analysis": ["gold"], "info_recall": []}}}
    per_report = load_case(output, task)
    results = comparisons({15: per_report, 43: per_report}, draws=40)
    assert results["rebind"]["support"] == {"n": 2, "estimate": 1, "low": 1, "high": 1}
    assert decision(results) == "continue_signal"


def test_rebind_body_changes_are_not_scored():
    frozen = {"question": "Q", "draft": "Unchanged.", "cards": {}, "passages": []}

    def offline(arm, *_):
        return '{"claims":[{}]}' if arm == "diagnostic" else '{"edits":[{"line":1,"text":"Changed."}]}'

    with pytest.raises(ValueError, match="rebind"):
        compare(frozen, offline)


def test_uncertainty_and_coverage_loss_have_distinct_decisions():
    stable = {"support": {"estimate": 0.11, "low": 0.02, "high": 0.20},
              "coverage": {"low": -0.01, "high": 0.01},
              "synthesis_retention": {"low": -0.01, "high": 0.01}}
    weak = {**stable, "support": {"estimate": 0.09, "low": -0.03, "high": 0.19}}
    loss = {**stable, "coverage": {"low": -0.10, "high": -0.03}}
    assert decision({"rebind": stable, "minedit": weak}) == "inconclusive"
    assert decision({"rebind": stable, "minedit": loss}) == "stop"
