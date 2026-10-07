"""Raw file -> computation input -> observed quotation. No network or Docker."""
import csv
import hashlib
import io
import json
import socket

import pytest

from dra import execution_tools as et, source_download as dl, simple_research as sr
from dra.execution import ExecutionConfig, ExecutionError

URL = 'https://data.example.org/counties.csv'
RAW = b'\xef\xbb\xbfcounty,obesity,uninsured,note\r\nAlpha,0.35,0.10,"comma, intact"\r\nBeta,0.34,0.09,"two\r\nlines"\r\n'


class Response:
    def __init__(self, data=RAW, status=200, headers=None):
        self.status = status
        self.headers = {'Content-Type': 'text/csv; charset=utf-8', **(headers or {})}
        self.body = io.BytesIO(data)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.body.read(size)


class Connection:
    sock = None

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def fake_http(monkeypatch, responses):
    opened = []

    def connect(url, timeout):
        conn = Connection(responses.pop(0))
        opened.append((url, conn))
        return conn

    monkeypatch.setattr(dl, '_connection', connect)
    return opened


def test_original_bytes_bom_crlf_quoted_commas_and_multiline_fields(monkeypatch):
    opened = fake_http(monkeypatch, [Response(headers={'Content-Length': str(len(RAW))})])
    data, text, meta = dl.fetch(URL)
    assert data == RAW and text == RAW.decode('utf-8-sig')
    assert meta['original_sha256'] == hashlib.sha256(RAW).hexdigest()
    assert meta['encoding'] == 'utf-8-sig'
    rows = list(csv.DictReader(io.StringIO(text, newline='')))
    assert len(rows) == 2
    assert rows[0]['note'] == 'comma, intact' and rows[1]['note'] == 'two\r\nlines'
    assert opened[0][1].closed


@pytest.mark.parametrize(('response', 'code'), [
    (Response(status=403), 'download_http_error'),
    (Response(data=b'<html>login</html>'), 'unexpected_html'),
    (Response(headers={'Content-Type': 'text/html'}), 'unexpected_html'),
    (Response(headers={'Content-Length': '999999999'}), 'incomplete_download'),
    (Response(headers={'Content-Length': '900'}), 'incomplete_download'),
    (Response(data=b''), 'invalid_text'),
    (Response(headers={'Content-Encoding': 'gzip'}), 'unsupported_encoding'),
])
def test_failed_download_is_explicit_and_closes_connection(monkeypatch, response, code):
    opened = fake_http(monkeypatch, [response])
    with pytest.raises(ExecutionError) as err:
        dl.fetch(URL)
    assert err.value.code == code
    assert opened[0][1].closed


@pytest.mark.parametrize('with_length', [True, False])
def test_download_larger_than_old_24_mib_limit_is_complete(monkeypatch, with_length):
    raw = b'county,value\r\n' + b'Alpha,1\r\n' * (25 * 1024 * 1024 // 9)
    headers = {'Content-Length': str(len(raw))} if with_length else {}
    opened = fake_http(monkeypatch, [Response(data=raw, headers=headers)])
    data, text, meta = dl.fetch(URL)
    assert len(raw) > 24 * 1024 * 1024
    assert data == raw and text == raw.decode('utf-8')
    assert meta['size'] == len(raw)
    assert meta['original_sha256'] == hashlib.sha256(raw).hexdigest()
    assert opened[0][1].closed


@pytest.mark.parametrize('target', ['http://127.0.0.1/data.csv', 'https://blocked.example.org/data.csv'])
def test_redirect_checked_before_second_request(monkeypatch, target):
    opened = fake_http(monkeypatch, [Response(status=302, headers={'Location': target})])
    with pytest.raises(ExecutionError):
        dl.fetch(URL, blocked_sites=['blocked.example.org'])
    assert len(opened) == 1 and opened[0][1].closed


def test_redirect_provenance_and_utf16(monkeypatch):
    raw = '名字,值\r\n甲,3\r\n'.encode('utf-16')
    fake_http(monkeypatch, [Response(status=302, headers={'Location': '/real.csv'}), Response(data=raw)])
    data, text, meta = dl.fetch(URL)
    assert data == raw and text == raw.decode('utf-16')
    assert meta['requested_url'] == URL and meta['final_url'].endswith('/real.csv')
    assert meta['redirects'] == [{'url': URL, 'status': 302}]


def test_deadline_cancellation_and_blocklist_prevent_requests(monkeypatch):
    monkeypatch.setattr(dl, '_connection', lambda *a: pytest.fail('must not request'))
    for kwargs in ({'deadline': 0}, {'cancelled': lambda: True}, {'blocked_sites': ['data.example.org']}):
        with pytest.raises(ExecutionError):
            dl.fetch(URL, **kwargs)


def test_dns_is_public_and_connection_is_pinned_with_original_tls_host(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ExecutionError, match='非公网'):
        dl._connection(URL, 3)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('1.1.1.1', 443))])
    calls = []
    monkeypatch.setattr(socket, 'create_connection', lambda *a: calls.append(a))
    conn = dl._connection(URL, 3)
    conn._create_connection(('data.example.org', 443), 3)
    assert calls[0][0] == ('1.1.1.1', 443) and conn.host == 'data.example.org'


def test_fake_dns_uses_existing_loopback_proxy_without_relaxing_private_hosts(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('198.18.4.35', 443))])
    monkeypatch.setattr(dl, 'getproxies', lambda: {'https': 'http://127.0.0.1:7897'})
    conn = dl._connection(URL, 3)
    assert conn.host == '127.0.0.1' and conn._tunnel_host == 'data.example.org'
    monkeypatch.setattr(dl, 'getproxies', lambda: {})
    with pytest.raises(ExecutionError):
        dl._connection(URL, 3)
    monkeypatch.setattr(dl, 'getproxies', lambda: {'https': 'http://127.0.0.1:7897'})
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('10.0.0.1', 443))])
    with pytest.raises(ExecutionError):
        dl._connection(URL, 3)


class Session:
    state = 'ready'

    def __init__(self):
        self.files = {}

    def import_file(self, name, content):
        path = '/workspace/input/' + name
        self.files[path] = content
        return path


def context(tmp_path, **kw):
    store = sr.RunStore(tmp_path, run_id='r', query='计算表格')
    ctx = sr.ToolContext(store, sr.SimpleConfig(execution=ExecutionConfig(enabled=True, network_mode='bridge'), **kw))
    ctx.execution = Session()
    return ctx


def test_large_source_index_copies_only_bounded_windows():
    class TrackedText(str):
        copied = 0
        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                self.copied += len(result)
            return result
    raw = TrackedText('county,value\r\n' + 'Alpha,1\r\n' * 100_000)
    rows = sr._excerpt_table(raw)
    assert raw.copied <= 3 * len(raw)
    assert ''.join(row[2] for row in rows) == raw
    assert all(raw[start:end] == text for start, end, text in rows)


def test_download_compute_read_and_cite_use_same_original_without_extractor(tmp_path, monkeypatch):
    opened = fake_http(monkeypatch, [Response()])
    monkeypatch.setattr(sr, 'fetch_url_content', lambda *a, **kw: pytest.fail('must not extract'))
    ctx = context(tmp_path)
    receipt, ok = ctx.execute('download_source', {'url': URL})
    assert ok, receipt
    result = json.loads(receipt)
    ref = result['source_ref']
    assert ctx.execution.files[result['path']] == RAW and result['path'].endswith('.csv')
    assert (ctx.store.sources_dir / f'{ref}.original').read_bytes() == RAW
    assert ctx.read_exposures == {}
    claim = {'source_ref': ref, 'quote': 'Alpha,0.35,0.10,"comma, intact"', 'claim': 'Alpha 数值为 0.35 和 0.10'}
    assert not ctx.execute('save_evidence', {'cards': [claim]})[1]
    text, ok = ctx.execute('read_source', {'url': URL, 'range': '0-1000'})
    assert ok, text
    assert json.loads(text)['source_ref'] == ref
    receipt, ok = ctx.execute('save_evidence', {'cards': [claim]})
    assert ok, receipt
    receipt, ok = ctx.execute('download_source', {'url': URL})
    assert ok and json.loads(receipt)['reused'] and len(opened) == 1
    receipt, ok = ctx.execute('export_research_materials', {'source_refs': [ref]})
    assert ok and json.loads(receipt)['files'][0]['path'] == result['path']
    assert ctx.store.state['actions'][0]['payload']['ok']
    (ctx.store.sources_dir / f'{ref}.original').write_bytes(b'changed')
    receipt, ok = ctx.execute('download_source', {'url': URL})
    assert not ok and json.loads(receipt)['error_code'] == 'source_changed' and len(opened) == 1


def test_download_failure_logged_without_registering_source(tmp_path, monkeypatch):
    fake_http(monkeypatch, [Response(status=403)])
    ctx = context(tmp_path)
    receipt, ok = ctx.execute('download_source', {'url': URL})
    assert not ok and 'HTTP 403' in receipt and ctx.store.state['sources'] == []
    assert not ctx.store.state['actions'][0]['payload']['ok']
    assert not ctx.execution.files


def test_download_persistence_failure_is_fatal(tmp_path, monkeypatch):
    from dra.llm import LLMPersistenceError
    fake_http(monkeypatch, [Response()])
    def fail(*a):
        raise OSError('disk full')
    monkeypatch.setattr('dra.durable.atomic_bytes', fail)
    ctx = context(tmp_path)
    with pytest.raises(LLMPersistenceError):
        ctx.execute('download_source', {'url': URL})
    assert ctx.store.state['sources'] == [] and not ctx.execution.files


def test_download_never_truncates_to_storage_limit(tmp_path, monkeypatch):
    fake_http(monkeypatch, [Response()])
    ctx = context(tmp_path, storage_max_chars=20)
    receipt, ok = ctx.execute('download_source', {'url': URL})
    assert not ok and json.loads(receipt)['error_code'] == 'source_too_large'
    assert not ctx.store.state['sources']


@pytest.mark.parametrize('source_mode,network_mode', [('web', 'none'), ('library', 'bridge')])
def test_network_disabled_or_library_cannot_advertise_or_download(tmp_path, monkeypatch, source_mode, network_mode):
    ctx = context(tmp_path, source_mode=source_mode)
    ctx.config = ctx.config.model_copy(update={'execution': ExecutionConfig(enabled=True, network_mode=network_mode)})
    monkeypatch.setattr(dl, '_connection', lambda *a: pytest.fail('must not request'))
    assert 'download_source' not in {s['function']['name'] for s in et.schemas_for(ctx)}
    assert '执行环境无网络' in et.instructions_for(ctx)
    receipt, ok = ctx.execute('download_source', {'url': URL})
    assert not ok and json.loads(receipt)['error_code'] == 'network_disabled'


def test_download_available_prompt_and_child_scope(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    fake_http(monkeypatch, [Response()])
    assert 'download_source' in {s['function']['name'] for s in et.schemas_for(ctx)}
    assert '执行环境无网络' not in et.instructions_for(ctx)
    assert 'urllib.request' in et.instructions_for(ctx)
    child = ctx.fork_for_subagent(ctx.config, budget=sr.Budget(ctx.config, None), delegation_id='sub1', parent_call_id='p')
    receipt, ok = child.execute('download_source', {'url': URL})
    assert ok and child.execution is ctx.execution
    assert ctx.execution.files[json.loads(receipt)['path']] == RAW


def test_history_keeps_original_and_rejects_tampering(tmp_path, monkeypatch):
    from test_research_history import history_fixture, child_context
    from dra.research_history import HistoryError
    ws, parent, store, _ = history_fixture(tmp_path)
    fake_http(monkeypatch, [Response()])
    data, text, meta = dl.fetch(URL)
    source, _ = store.write_source(sr.RetrievedDoc(title='CSV', source_url=URL, snippet='', raw_content=text),
                                  original_bytes=data, download=meta)
    ctx, _ = child_context(ws, parent)
    ref = ctx.history.import_source(1, source.ref)
    assert ctx.store.source(ref).download == meta
    assert (ctx.store.sources_dir / f'{ref}.original').read_bytes() == RAW
    assert ctx.read_exposures == {}
    (store.sources_dir / f'{source.ref}.original').write_bytes(b'bad')
    with pytest.raises(HistoryError, match='校验失败'):
        ctx.history.import_source(1, source.ref)


def test_web_original_download_matches_bytes_and_checks_hash(tmp_path, monkeypatch):
    from dra import web
    from dra.runstore import RunStore
    from fastapi import HTTPException
    ws = RunStore(tmp_path / 'runs')
    monkeypatch.setattr(web, 'STORE', ws)
    run_id = ws.create_run('q', None, {'mode': 'simple'})
    ctx = context(ws._dir(run_id))
    fake_http(monkeypatch, [Response()])
    receipt, ok = ctx.execute('download_source', {'url': URL})
    assert ok
    ref = json.loads(receipt)['source_ref']
    response = web.api_run_original(run_id, ref)
    assert response.body == RAW and response.headers['content-disposition'].startswith('attachment;')
    (ctx.store.sources_dir / f'{ref}.original').write_bytes(b'bad')
    with pytest.raises(HTTPException):
        web.api_run_original(run_id, ref)
