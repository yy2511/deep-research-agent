"""结构化输出加固层：三层解析 + 失败分类重试。全部 mock chat，零 API 成本。"""
import pytest

import dra.llm as llm


def test_extract_json_think_block():
    raw = '<think>先想一想…{"draft": 1}</think>\n{"sufficient": true}'
    assert llm.extract_json(raw) == {"sufficient": True}


def test_extract_json_fenced():
    raw = '好的，结果如下：\n```json\n{"a": [1, 2]}\n```\n希望有帮助！'
    assert llm.extract_json(raw) == {"a": [1, 2]}


def test_extract_json_trailing_prose():
    raw = '推理过程省略。{"evidence": []} 以上就是全部内容。'
    assert llm.extract_json(raw) == {"evidence": []}


def test_extract_json_hopeless():
    assert llm.extract_json("完全没有 JSON") is None
    assert llm.extract_json("") is None


def _chat_seq(monkeypatch, replies):
    """让 llm.chat 按序返回 replies，记录每次收到的 messages。"""
    calls = []

    def fake_chat(messages, **kw):
        calls.append(list(messages))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def test_call_json_empty_then_ok(monkeypatch):
    calls = _chat_seq(monkeypatch, ["", '{"sufficient": true}'])
    data = llm.call_json([{"role": "user", "content": "x"}], expect_keys=("sufficient",))
    assert data == {"sufficient": True}
    assert len(calls) == 2                      # 空响应触发了一次重试


def test_call_json_missing_key_nudge(monkeypatch):
    calls = _chat_seq(monkeypatch, ['{"sufficient": true}',
                                    '{"sufficient": true, "unmet_gap_ids": [2]}'])
    data = llm.call_json([{"role": "user", "content": "x"}],
                         expect_keys=("sufficient", "unmet_gap_ids"))
    assert data["unmet_gap_ids"] == [2]
    # 第二次调用带上了纠错 nudge（追加了 assistant + user 两条消息）
    assert len(calls[1]) == 3
    assert "unmet_gap_ids" in calls[1][-1]["content"]


def test_call_json_nested_validator_nudge(monkeypatch):
    calls = _chat_seq(monkeypatch, [
        '{"decisions": [{"node_id": "m4"}]}',
        '{"decisions": [{"node_id": "m4", "evidence_ids": [1]}]}',
    ])

    def validate(data):
        decision = data["decisions"][0]
        return None if "evidence_ids" in decision else "decisions[0] 缺少 evidence_ids"

    data = llm.call_json(
        [{"role": "user", "content": "x"}],
        expect_keys=("decisions",),
        validate=validate,
    )

    assert data["decisions"][0]["evidence_ids"] == [1]
    assert len(calls) == 2
    assert "decisions[0] 缺少 evidence_ids" in calls[1][-1]["content"]


def test_call_json_exhausted_returns_empty(monkeypatch):
    _chat_seq(monkeypatch, ["not json at all"])
    data = llm.call_json([{"role": "user", "content": "x"}], json_retries=1)
    assert data == {}
