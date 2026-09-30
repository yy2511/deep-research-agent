"""真实 HTTP/后台线程/事件回放的插话验收；不请求外网。"""
import json
import threading

from fastapi.testclient import TestClient

from dra import research, web
from test_web_research_e2e import isolated_web, _wait_done
from test_budget_wrapup import _turn


def test_live_input_idempotency_finish_and_history(isolated_web, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = research.run_minimal_research
    def model(messages, **_):
        calls.append(messages)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return _turn(('finish_research', {'draft': '旧答案'}))
        assert any('只查齿科' in m.get('content', '') for m in messages)
        return _turn(('finish_research', {'draft': '齿科材料暂缺，无法给出确定结论。'}))
    def run(*args, **kwargs):
        return original(*args, **kwargs, model_call=model)
    monkeypatch.setattr(research, 'run_minimal_research', run)
    with TestClient(web.app) as client:
        run_id = client.post('/api/research', json={'query': '面扫比较', 'mode': 'simple'}).json()['run_id']
        assert entered.wait(2)
        url = f'/api/research/{run_id}/messages'
        try:
            body = {'message_id': 'user-one', 'text': '只查齿科'}
            first = client.post(url, json=body)
            assert first.status_code == 202
            assert first.json() == client.post(url, json=body).json()
            assert client.post(url, json={**body, 'text': '不同正文'}).status_code == 409
        finally:
            release.set()
        detail = _wait_done(client, run_id)
        assert len(calls) == 2
        assert detail['research_input']['state'] == 'closed'
        rows = [json.loads(s) for s in client.get(f'/api/runs/{run_id}/events').text.splitlines()]
        accepted = [r for r in rows if r['type'] == 'research_input' and r['kind'] == 'accepted']
        included = [r for r in rows if r['type'] == 'research_input' and r['kind'] == 'included']
        assert len(accepted) == len(included) == 1
        assert included[0]['messages'][0]['message_id'] == 'user-one'
        assert accepted[0]['event_id']
        assert client.post(url, json=body).status_code == 202
        assert client.post(url, json={'message_id': 'late', 'text': '太晚了'}).status_code == 409
        # 模拟进程重启后仅有磁盘历史：不能重开研究，同一个 ID 的重试仍可查询。
        isolated_web[1].runs.clear()
        assert client.post(url, json=body).status_code == 202
        assert client.get(f'/api/runs/{run_id}').json()['research_input']['state'] == 'closed'


def test_input_storage_failure_returns_error_not_success(isolated_web, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = research.run_minimal_research
    def model(*args, **kwargs):
        entered.set(); assert release.wait(5)
        return _turn(('finish_research', {'draft': '不能悄悄成功'}))
    monkeypatch.setattr(research, 'run_minimal_research',
                        lambda *a, **kw: original(*a, **kw, model_call=model))
    with TestClient(web.app) as client:
        run_id = client.post('/api/research', json={'query': '研究', 'mode': 'simple'}).json()['run_id']
        assert entered.wait(2)
        append = isolated_web[0].append_event
        def broken(rid, event):
            if event.get('type') == 'research_input' and event.get('kind') == 'accepted':
                raise OSError('disk full')
            return append(rid, event)
        monkeypatch.setattr(isolated_web[0], 'append_event', broken)
        try:
            response = client.post(f'/api/research/{run_id}/messages',
                json={'message_id': 'u1', 'text': '不要丢掉我的消息'})
            assert response.status_code == 503
        finally:
            release.set()
        detail = _wait_done(client, run_id)
        assert detail['meta']['status'] == 'failed'


def test_old_event_protocol_does_not_block_historical_report(isolated_web):
    store, _ = isolated_web
    run_id = store.create_run('旧研究', None, {'mode': 'simple'})
    (store._dir(run_id) / 'report.md').write_text('# 仍可回看的报告', encoding='utf-8')
    store.finalize(run_id, 'done', {})
    meta = store.get_meta(run_id)
    meta['event_schema_version'] = 1
    store._write_meta(run_id, meta)
    with TestClient(web.app) as client:
        response = client.get(f'/api/runs/{run_id}')
        assert response.status_code == 200
        assert response.json()['report_md'] == '# 仍可回看的报告'
        assert response.json()['meta']['replay_compatible'] is False
        assert response.json()['research_input']['state'] == 'closed'


def test_crash_with_partial_event_keeps_saved_input_honest(isolated_web):
    store, _ = isolated_web
    run_id = store.create_run('中断研究', None, {'mode': 'simple'})
    from dra.research_input import ResearchInput
    inbox = ResearchInput(run_id, lambda e: store.append_event(run_id, e))
    inbox.accept('before-crash', '原样保存的要求')
    with (store.root / run_id / 'events.jsonl').open('a') as handle:
        handle.write('{"type":')
    store.finalize(run_id, 'failed', {})
    with TestClient(web.app) as client:
        detail = client.get(f'/api/runs/{run_id}').json()
        assert detail['research_input']['messages'][0]['status'] == 'undelivered'
        assert detail['research_input']['error']
        response = client.post(f'/api/research/{run_id}/messages', json={
            'message_id': 'before-crash', 'text': '原样保存的要求'})
        assert response.status_code == 202
        assert response.json()['message']['status'] == 'undelivered'


def test_user_and_worker_events_reach_sse_in_persisted_order(isolated_web):
    """明确制造线程在解锁后被换走，后写事件不能抢先推送而吞掉用户消息。"""
    from queue import SimpleQueue
    store, registry = isolated_web
    run_id = store.create_run('并发事件', None, {'mode': 'simple'})
    handle = web.RunHandle(run_id)
    registry.runs[run_id] = handle
    second_allowed, second_done = threading.Event(), threading.Event()
    mutex = threading.Lock()
    class ScheduledLock:
        def __enter__(self):
            mutex.acquire()
        def __exit__(self, *_):
            mutex.release()
            if threading.current_thread().name == 'user-input':
                second_allowed.set()
                assert second_done.wait(3)
    class Loop:
        def call_soon_threadsafe(self, callback, value):
            callback(value)
    queue = SimpleQueue()
    handle.lock = ScheduledLock()
    handle.subscribers.add(queue)
    registry.loop = Loop()
    def publish_worker():
        assert second_allowed.wait(3)
        try:
            registry.publish(run_id, {'type': 'subagent_tool_call'})
        finally:
            second_done.set()
    first = threading.Thread(name='user-input', target=lambda: registry.publish(
        run_id, {'type': 'research_input', 'kind': 'accepted'}, durable=True))
    second = threading.Thread(target=publish_worker)
    first.start(); second.start(); first.join(5); second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert [queue.get_nowait()['seq'], queue.get_nowait()['seq']] == [0, 1]
