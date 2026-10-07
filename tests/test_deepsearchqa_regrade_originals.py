"""原文重评：完整输入、真实并发、空答分母、失败回执与断点续评。"""
import importlib
import json
from pathlib import Path
import threading

import httpx
import pytest


@pytest.fixture
def grader(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "eval/deepsearchqa"))
    return importlib.import_module("regrade_originals")


TEMPLATES = {"DEEPSEARCH_QA_PROMPT": "Judge\n", "GRADER_RATING_OUTPUT_EXAMPLE":
             "Q:{prompt}\nType:{prompt_type}\nGold:{answer}\nResponse:{response}"}
QUESTION = {"problem": "Question", "answer_type": "Single Answer", "answer": "A"}


def reply():
    verdict = {"Answer Correctness": {"Explanation": "match", "Correctness Details": {"A": True},
                                       "Excessive Answers": []}}
    return {"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(verdict) + "\n```"}]},
                            "finishReason": "STOP"}], "modelVersion": "gemini-2.5-flash"}


def test_whole_original_is_sent_without_extraction(grader, tmp_path):
    original = "  Analysis with background B [c1].\n<final_answer>A [c2]</final_answer>\nTail evidence.  "
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = grader.grade_one({"id": 1, "response": original}, QUESTION, TEMPLATES,
                                  tmp_path, "https://example.test/grade", client)
    assert result["f1"] == 1
    assert seen == [{"contents": [{"role": "user", "parts": [{"text":
                    "Judge\nQ:Question\nType:Single Answer\nGold:A\nResponse:" + original.strip()}]}]}]
    assert (tmp_path / "1/original-answer.md").read_text() == original
    assert result["response_sha256"] == grader.sha(original)


def test_parallel_resume_and_empty_answers_keep_denominator(grader, tmp_path):
    barrier = threading.Barrier(2)
    calls = []

    def handle(request):
        calls.append(request)
        barrier.wait(timeout=5)
        return httpx.Response(200, json=reply())

    records = [{"id": i, "response": "A" if i else "", "prompt_profile": "product"} for i in range(3)]
    del records[1]["prompt_profile"]  # 早期 product 结果没有此字段。
    snapshot = {"records": records, "expected_questions": 900, "old_grades": {}, "old_judge": {},
                "judge": {"endpoint": "https://example.test/grade"}}
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        summary = grader.run_batch(tmp_path, snapshot, {i: QUESTION for i in range(3)}, TEMPLATES, client, 2)
        grader.run_batch(tmp_path, snapshot, {i: QUESTION for i in range(3)}, TEMPLATES, client, 2)
    assert len(calls) == 2  # 空答无请求；再次执行复用完成评分。
    assert summary["mean_f1"] == pytest.approx(2 / 3)
    assert summary["sample_size"] == 3
    assert not summary["research_complete"]
    assert summary["answers_produced"] == 2
    assert summary["cohorts"]["product"]["sample_size"] == 3


def test_bad_judge_response_is_recorded_and_never_scored_as_zero(grader, tmp_path):
    def handle(request):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "invalid"}]}}]})

    record = {"id": 1, "response": "A"}
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = grader.grade_one(record, QUESTION, TEMPLATES, tmp_path, "https://example.test/grade", client)
    assert result["status"] == "judge_error"
    assert len(list((tmp_path / "1").glob("attempt-*/error.json"))) == 2
    assert "f1" not in result
    summary = grader.metrics([record], {"1": result})
    assert summary["mean_f1"] is None
    assert summary["judge_errors_or_pending"] == [1]
