"""不设隐藏上限：Bash 只计数、无时限时容器续期、收尾延续原对话。只用协议替身，不调用 Docker 或模型。"""
import json
import socket
import struct
import time
from types import SimpleNamespace as N

import pytest

from dra import execution, simple_research as sr
from dra.execution import ExecutionConfig, ExecutionError, ExecutionSession
from test_budget_wrapup import RAW, Script, _doc, _turn


class FakeDocker:
    """按 argv 回包的 exec 替身：租期续期回 ok JSON，其余命令回 ok。"""

    def __init__(self):
        self.started = []

    def exec_create(self, container, argv, **kw):
        self.started.append(argv)
        return {'Id': f'exec-{len(self.started)}'}

    def exec_start(self, *a, **kw):
        body = b'{"ok": true}' if '--lease' in self.started[-1] else b'ok'
        read, write = socket.socketpair()
        write.sendall(struct.pack('>BxxxI', 1, len(body)) + body)
        write.close()
        return read

    def exec_inspect(self, *a):
        return {'Running': False, 'ExitCode': 0}


def ready_session(tmp_path, config, **kwargs):
    session = ExecutionSession(tmp_path, 'open-limits', config, **kwargs)
    session.state = 'ready'
    session.workspace.prepare()
    api = FakeDocker()
    session.client = N(api=api, close=lambda: None)
    session.container = N(id='shared', remove=lambda **kw: None, exec_run=lambda argv, **kw: N(exit_code=0))
    return session, api


def leases(api):
    return [argv for argv in api.started if '--lease' in argv]


def test_bash_keeps_running_past_twenty_commands_and_counts_them(tmp_path):
    session, _ = ready_session(tmp_path, ExecutionConfig(enabled=True), deadline=time.monotonic() + 600)
    assert ExecutionConfig().max_commands is None
    for i in range(25):
        assert session.tool('bash', {'command': f'echo {i}'})['ok']
    assert session.commands == 25 and session.summary()['commands'] == 25


def test_explicit_command_cap_is_still_honoured(tmp_path):
    session, _ = ready_session(tmp_path, ExecutionConfig(enabled=True, max_commands=2), deadline=time.monotonic() + 600)
    for _ in range(2):
        session.tool('bash', {'command': 'true'})
    with pytest.raises(ExecutionError) as error:
        session.tool('bash', {'command': 'true'})
    assert error.value.code == 'command_budget' and session.commands == 2


def test_untimed_session_has_no_deadline_and_renews_container_lease(tmp_path, monkeypatch):
    clock = N(now=1000.0)
    monkeypatch.setattr(execution.time, 'monotonic', lambda: clock.now)
    session, api = ready_session(tmp_path, ExecutionConfig(enabled=True))
    assert session.deadline == float('inf')
    session.tool('bash', {'command': 'first'})
    assert len(leases(api)) == 1 and leases(api)[0][-1] == str(execution.OPEN_LEASE_S)
    clock.now += execution.LEASE_RENEW_S - 1
    session.tool('bash', {'command': 'soon after'})
    assert len(leases(api)) == 1  # 续期间隔内不重复续期
    clock.now += 7200  # 两小时后仍可执行：没有隐藏的 600 秒会话截止
    assert session.tool('bash', {'command': 'much later'})['ok']
    assert len(leases(api)) == 2 and session.commands == 3


def test_timed_session_keeps_its_deadline_without_extra_renewals(tmp_path):
    session, api = ready_session(tmp_path, ExecutionConfig(enabled=True), deadline=time.monotonic() + 600)
    session.tool('bash', {'command': 'true'})
    assert not leases(api)
    assert session._lease_ttl() <= 600


QUOTE = "The sqlite3_rsync utility works fine if both ORIGIN and REPLICA are local."


def _budget_run(tmp_path, script, **config):
    config = sr.SimpleConfig(max_tool_calls=2, tool_reserve_calls=0, delegation_enabled=False, **config)
    return sr.run_simple_research('sqlite3_rsync 两端都在本机可以吗？', config=config, run_dir=tmp_path,
                                  model_call=script, search_fn=lambda q: [_doc(RAW)])


def test_budget_wrapup_continues_the_original_conversation(tmp_path):
    """工具额度用尽后的收尾沿用原对话：已看到的工具结果仍在，收尾要求追加在末尾。"""
    script = Script([_turn(('search_sources', {'query': 'sqlite3_rsync local'})),
                     _turn(('read_source', {'source_ref': 's1'}))],
                    finish=('finish_research', {'draft': '原文显示两端都在本机也能工作，但尚未完成存证。',
                                                'gaps': ['关键原文尚未存证']}))
    result = _budget_run(tmp_path, script)
    assert result.stop_reason == 'tool_budget+wrapup' and result.draft
    research, wrapup = script.seen[-2], script.seen[-1]
    assert script.tool_schemas[-1] == ['finish_research']
    assert wrapup[:len(research)] == research  # 原请求整段保留为前缀
    assert wrapup[0] == research[0]
    text = json.dumps(wrapup, ensure_ascii=False)
    assert QUOTE in text  # 读到但未存证的原文仍在上下文中
    assert wrapup[-1]['role'] == 'user' and wrapup[-1]['content'].startswith('【进入收尾】')
    assert '研究工具额度已用尽' in wrapup[-1]['content']
    assert '尚未完成存证' in wrapup[-1]['content'] and '未取到可引用材料' not in text
    actions = [a['action'] for a in json.loads((tmp_path / 'run.json').read_text())['actions']]
    assert 'wrapup_context' not in actions


def test_wrapup_falls_back_to_cards_only_when_original_conversation_overflows(tmp_path):
    """原对话超出上下文时改用任务与证据卡收尾，并记录这次改用。"""
    seen = []
    turns = [_turn(('search_sources', {'query': 'sqlite3_rsync local'})),
             _turn(('read_source', {'source_ref': 's1'}))]
    def model(messages, **kwargs):
        seen.append(messages)
        if len(seen) <= len(turns):
            return turns[len(seen) - 1]
        if len(seen) == len(turns) + 1:
            raise RuntimeError("This model's maximum context length is 1000 tokens")
        return _turn(('finish_research', {'draft': '资料尚未存证，暂无确定结论。', 'gaps': ['未存证']}))
    result = _budget_run(tmp_path, model)
    assert result.stop_reason == 'tool_budget+wrapup' and result.draft
    assert seen[-2][-1]['content'].startswith('【进入收尾】')
    assert seen[-1][0]['role'] == 'system' and len(seen[-1]) == 2  # 只含任务与证据卡的独立收尾
    run = json.loads((tmp_path / 'run.json').read_text())
    assert [c['step'] for c in run['calls']][-2:] == ['wrapup', 'wrapup']
    assert any(a['action'] == 'wrapup_context' for a in run['actions'])
