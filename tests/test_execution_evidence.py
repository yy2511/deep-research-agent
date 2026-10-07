"""CSV 由真实本机 Python 处理；仅替换容器传输层，不访问网络/模型/Docker。"""
import hashlib
import json
import subprocess
import sys
from uuid import uuid4

import pytest

from dra import simple_research as sr
from dra.execution import ExecutionConfig
from dra.models import RetrievedDoc
from test_research_history import child_context, history_fixture


class LocalSession:
    state = 'ready'

    def __init__(self, store, *, preview=4096):
        self.store = store
        self.preview = preview

    def import_file(self, name, data):
        target = self.store.dir / 'input' / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        return str(target)

    def tool(self, name, args):
        assert name == 'bash'
        proc = subprocess.run(args['command'], shell=True, executable='/bin/bash',
                              capture_output=True, cwd=self.store.dir)
        execution_id = 'exec_' + uuid4().hex
        directory = self.store.dir / 'execution' / 'logs'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{execution_id}.stdout').write_bytes(proc.stdout)
        (directory / f'{execution_id}.stderr').write_bytes(proc.stderr)
        return {'ok': proc.returncode == 0, 'exit_code': proc.returncode,
                'stdout': proc.stdout[:self.preview].decode('utf-8', 'replace'),
                'stderr': proc.stderr.decode(), 'execution_id': execution_id,
                'truncated': len(proc.stdout) > self.preview, 'log_truncated': False}


def context(tmp_path, **config):
    store = sr.RunStore(tmp_path, run_id='compute-test', query='筛选表格')
    ctx = sr.ToolContext(store, sr.SimpleConfig(execution=ExecutionConfig(enabled=True), **config))
    ctx.execution = LocalSession(store)
    return ctx


def input_ref(ctx, text='value\n1\n'):
    """登记一个可声明的输入来源；计算结果只有声明输入才会登记为证据来源。"""
    source, _ = ctx.store.write_source(RetrievedDoc(title='input.csv', source_url='https://example.org/input.csv',
                                                    snippet='', raw_content=text))
    return source.ref


def bash(ctx, code, **kwargs):
    command = f"{sys.executable} - <<'PY'\n{code}\nPY"
    result, ok = ctx.execute('bash', {'command': command, **kwargs})
    return json.loads(result), ok


def cite(ctx, ref, quote):
    return ctx.execute('save_evidence', {'cards': [
        {'source_ref': ref, 'quote': quote, 'claim': '按数据筛选后的计算结果'}]})


def test_csv_filter_to_evidence_without_character_reread(tmp_path):
    ctx = context(tmp_path)
    raw = b'county,obesity,uninsured\r\n"Alpha, County",0.35,0.10\r\nBeta,0.31,0.12\r\nGamma,0.40,0.08\r\n'
    digest = hashlib.sha256(raw).hexdigest()
    source, _ = ctx.store.write_source(RetrievedDoc(title='counties.csv', source_url='https://example.org/counties.csv',
        snippet='', raw_content=raw.decode()), original_bytes=raw,
        download={'original_sha256': digest, 'size': len(raw), 'filename': 'counties.csv'})
    (tmp_path / 'counties.csv').write_bytes(raw)
    ctx.execution_materials.append({'source_ref': source.ref, 'path': '/workspace/input/counties.csv',
                                    'sha256': digest, 'size': len(raw)})
    payload, ok = bash(ctx, '''import csv
from decimal import Decimal
with open('counties.csv', newline='') as f:
    for row_number, row in enumerate(csv.DictReader(f), 2):
        if Decimal(row['obesity']) >= Decimal('0.35') and Decimal(row['uninsured']) >= Decimal('0.10'):
            print(f"row={row_number}; county={row['county']}; obesity={row['obesity']} (fraction); uninsured={row['uninsured']} (fraction)")
''', source_refs=[source.ref])
    assert ok
    expected = 'row=2; county=Alpha, County; obesity=0.35 (fraction); uninsured=0.10 (fraction)'
    assert payload['stdout'].strip() == expected
    ref = payload['source_ref']
    assert ref != source.ref and source.ref not in ctx.read_exposures
    assert ctx.store.source(ref).title == '计算结果（输入：counties.csv）'
    assert cite(ctx, ref, expected)[1]
    assert not cite(ctx, source.ref, 'Beta,0.31,0.12')[1]
    assert not cite(ctx, ref, 'Gamma qualifies')[1]
    card = ctx.store.cards()[0]
    assert card.source_kind == 'computation' and card.source_url == ''
    assert card.evidence_quotes()[0].source_kind == 'computation'
    snapshot = ctx.store.source(ref)
    assert snapshot.computation['declared_source_refs'] == [source.ref]
    assert snapshot.computation['inputs'][0]['sha256'] == digest
    assert 'Decimal' in snapshot.computation['command']
    assert (ctx.store.sources_dir / f'{source.ref}.original').read_bytes() == raw


def test_long_stdout_reading_grants_only_observed_result(tmp_path):
    ctx = context(tmp_path)
    ctx.execution.preview = 20
    payload, ok = bash(ctx, "print('first row = 1'); print('.' * 1500); print('second row = 987654321')",
                       source_refs=[input_ref(ctx)])
    assert ok and payload['has_more']
    ref = payload['source_ref']
    assert cite(ctx, ref, 'first row = 1')[1]
    assert not cite(ctx, ref, 'second row = 987654321')[1]
    text, ok = ctx.execute('read_source', {'source_ref': ref, 'cursor': payload['next_cursor']})
    assert ok and json.loads(text)['source_kind'] == 'computation'
    assert cite(ctx, ref, 'second row = 987654321')[1]


def test_failed_session_inventory_never_claims_only_base_packages(tmp_path):
    ctx=context(tmp_path)
    ctx.execution.session_dependencies=True
    original=ctx.execution.tool
    def tool(name,args):
        return {**original(name,args),'dependency_inventory_error':'fixture collection failed'}
    ctx.execution.tool=tool
    payload,ok=bash(ctx,"print('value=1729')",source_refs=[input_ref(ctx)])
    assert ok
    provenance=ctx.store.source(payload['source_ref']).computation
    assert provenance['dependency_manifest'] is None
    assert provenance['dependency_inventory_error']=='fixture collection failed'


def test_utf8_preview_boundary_and_library_computation(tmp_path):
    ctx = context(tmp_path, source_mode='library')
    ctx.execution.preview = 5  # 截在第二个汉字内部。
    payload, ok = bash(ctx, "print('总数=3')", source_refs=[input_ref(ctx)])
    assert ok and payload['shown_range'] == 'chars=0-1'
    ref = payload['source_ref']
    assert not cite(ctx, ref, '总数=3')[1]
    assert ctx.execute('read_source', {'source_ref': ref, 'range': '0-'})[1]
    assert cite(ctx, ref, '总数=3')[1]


def test_failed_execution_and_empty_stdout_do_not_create_evidence(tmp_path):
    ctx = context(tmp_path)
    ref = input_ref(ctx)
    payload, ok = bash(ctx, "print('partial'); raise ValueError('bad input')", source_refs=[ref])
    assert not ok and 'source_ref' not in payload
    payload, ok = bash(ctx, 'pass', source_refs=[ref])
    assert ok and 'source_ref' not in payload
    assert [s['ref'] for s in ctx.store.state['sources']] == [ref]


def test_output_without_declared_inputs_is_work_only(tmp_path):
    """ls、调试打印等不声明输入的输出不登记为来源，也不能存证。"""
    ctx = context(tmp_path)
    payload, ok = bash(ctx, "print('debug 42')")
    assert ok and 'source_ref' not in payload and '不能存证' in payload['note']
    assert ctx.store.state['sources'] == []


def test_unknown_declared_input_is_rejected_before_running(tmp_path):
    ctx = context(tmp_path)
    marker = tmp_path / 'ran'
    payload, ok = bash(ctx, f"open({str(marker)!r}, 'w').write('x')", source_refs=['s404'])
    assert not ok and payload['error_code'] == 'unknown_source' and 's404' in payload['message']
    assert not marker.exists() and ctx.store.state['sources'] == []


def test_history_preserves_distinct_execution_and_script_record(tmp_path):
    ws, parent, store, _ = history_fixture(tmp_path)
    ctx = sr.ToolContext(store, sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    ctx.execution = LocalSession(store)
    script_args = {'path': '/workspace/work/a.py', 'content': 'print(60)'}
    ctx.receipts.append({'tool': 'write_file', 'args': script_args, 'ok': True, 'payload': '{}'})
    ref = store.state['sources'][0]['ref']
    first, _ = bash(ctx, 'print(60)', source_refs=[ref])
    second, _ = bash(ctx, 'print(sum([10,20,30]))', source_refs=[ref])
    assert cite(ctx, first['source_ref'], '60')[1]
    script_args['content'] = 'print(999)'
    child, _ = child_context(ws, parent)
    local1 = child.history.import_source(1, first['source_ref'])
    local2 = child.history.import_source(1, second['source_ref'])
    assert local1 != local2  # 同一个数字不等于同一次计算。
    saved = child.store.source(local1).computation
    assert saved['run_id'] == parent
    assert saved['workspace_steps'][0]['arguments']['content'] == 'print(60)'
    assert 'print(60)' in saved['command']
    assert child.execute('read_source', {'source_ref': local1, 'range': '0-'})[1]
    assert cite(child, local1, '60')[1]


def test_result_persistence_failure_does_not_become_retryable_tool_error(tmp_path, monkeypatch):
    from dra.llm import LLMPersistenceError
    ctx = context(tmp_path)
    ref = input_ref(ctx)
    def fail(*a, **kw):
        raise OSError('disk full')
    monkeypatch.setattr(ctx.store, 'write_source', fail)
    with pytest.raises(LLMPersistenceError, match='计算结果'):
        bash(ctx, 'print(1)', source_refs=[ref])


@pytest.mark.parametrize('stderr_first', [False, True])
def test_stderr_log_limit_does_not_mislabel_complete_stdout(tmp_path, monkeypatch, stderr_first):
    import struct
    from types import SimpleNamespace
    from dra import execution as ex
    output = (1, b'count = 3\n')
    error = (2, b'x' * (ex.MiB + 20))
    chunks = iter(struct.pack('>BxxxI', channel, len(data)) + data
                  for channel, data in ([error, output] if stderr_first else [output, error]))
    stream = SimpleNamespace(setblocking=lambda value: None, recv=lambda size: next(chunks, b''), close=lambda: None)
    session = ex.ExecutionSession(tmp_path, 'test', ExecutionConfig(enabled=True))
    session.container = SimpleNamespace(id='test', exec_run=lambda *a, **kw: SimpleNamespace(exit_code=0))
    session.client = SimpleNamespace(api=SimpleNamespace(
        exec_create=lambda *a, **kw: {'Id': 'test'}, exec_start=lambda *a, **kw: stream,
        exec_inspect=lambda token: {'Running': False, 'ExitCode': 0}))
    monkeypatch.setattr(session, '_check', lambda *a: None)
    monkeypatch.setattr(session.workspace, 'describe', lambda: {'over_quota': False})
    monkeypatch.setattr(ex.select, 'select', lambda read, write, error, timeout: (read, [], []))
    result = session._exec(['fixture'], 30)
    assert result['stdout'] == 'count = 3\n'
    assert result['log_truncated']
    assert result['stdout_log_truncated'] is stderr_first
