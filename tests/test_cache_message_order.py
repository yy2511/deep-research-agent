"""Keep prior tool protocol and full evidence while moving changing state behind history."""
from copy import deepcopy

from dra import simple_research as sr


def setup(tmp_path):
    config = sr.SimpleConfig(max_llm_calls=20, max_tool_calls=20)
    context = sr.ToolContext(sr.RunStore(tmp_path, run_id="cache-order", query="research goal"), config)
    base = [{"role": "system", "content": "fixed rules"}, {"role": "user", "content": "research goal"}]
    history = [[
        {"role": "assistant", "content": "Inspect the source.", "tool_calls": [
            {"id": "read1", "type": "function", "function": {"name": "inspect_research", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "read1", "content": "Original source receipt"},
    ]]
    return context, base, history


def test_budget_countdown_does_not_change_prior_history_prefix(tmp_path):
    context, base, history = setup(tmp_path)
    before = deepcopy(history)
    a = sr._build_messages(base, context, [], history, seconds_left=90)
    context.tool_calls = 2
    b = sr._build_messages(base, context, [], history, seconds_left=80)
    prefix = base + history[0]
    assert a[:len(prefix)] == b[:len(prefix)] == prefix
    assert "约 90 秒" in a[-1]["content"]
    assert "约 80 秒" in b[-1]["content"] and "已用 2/" in b[-1]["content"]
    assert history == before


def test_new_exchange_reuses_existing_history_and_keeps_tool_pair_adjacent(tmp_path):
    context, base, history = setup(tmp_path)
    a = sr._build_messages(base, context, [], history, seconds_left=90)
    history.append([{"role": "assistant", "content": "Continue research."}])
    b = sr._build_messages(base, context, [], history, seconds_left=80)
    # Delivered status stays where it was; only changed sections are appended.
    assert b[:len(a)] == a
    assistant_index = next(i for i, message in enumerate(b) if message.get("tool_calls"))
    assert b[assistant_index+1]["tool_call_id"] == b[assistant_index]["tool_calls"][0]["id"]
    assert sum("【当前状态】" in m.get("content", "") for m in b) == 1


def test_latest_plan_and_child_message_remain_after_history(tmp_path):
    context, base, history = setup(tmp_path)

    class Manager:
        def inspect(self):
            return [{"id": "sub1", "goal": "check", "status": "completed"}]

    context.delegation_manager = Manager()
    context.delegation_messages = [{"message_id": "m1", "kind": "result", "sender": "sub1",
                                    "message": "Check the exception condition.", "result": {"card_ids": ["c7"]}}]
    context.plan_items = [{"id": "p1", "text": "Check latest scope", "status": "doing"}]
    messages = sr._build_messages(base, context, [], history, seconds_left=30)
    assert messages[:len(base) + len(history[0])] == base + history[0]
    assert "Check the exception condition." in messages[-1]["content"]
    assert "Check latest scope" in messages[-1]["content"]
