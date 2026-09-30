"""用户输入的接收、持久化和交稿边界；全部离线。"""
import threading

import pytest

from dra.llm import LLMPersistenceError
from dra.research_input import ResearchInput


def test_idempotency_survives_close_and_keeps_original_text():
    log = []
    inbox = ResearchInput('r', log.append)
    first = inbox.accept('u1', '  只查齿科\n价格不用查  ')
    assert first['message']['text'] == '  只查齿科\n价格不用查  '
    assert inbox.accept('u1', first['message']['text']) == first
    with pytest.raises(ValueError, match='重复'):
        inbox.accept('u1', '不同的要求')
    rows, revision = inbox.begin_request('call1')
    assert [r['message_id'] for r in rows] == ['u1']
    assert inbox.seal(revision)
    assert inbox.accept('u1', first['message']['text'])['accepted']
    with pytest.raises(ValueError, match='收尾|结束'):
        inbox.accept('u2', '再查价格')
    assert len([e for e in log if e.get('kind') == 'accepted']) == 1


@pytest.mark.parametrize('accept_first', [True, False])
def test_send_and_finish_have_one_atomic_order(accept_first):
    inbox = ResearchInput('r', lambda _: None)
    _, revision = inbox.begin_request('call1')
    first_done = threading.Event()
    outcomes = []
    def send():
        if not accept_first:
            assert first_done.wait(2)
        try:
            inbox.accept('u1', '改看软件兼容')
            outcomes.append('accepted')
        except ValueError:
            outcomes.append('rejected')
        finally:
            if accept_first:
                first_done.set()
    def finish():
        if accept_first:
            assert first_done.wait(2)
        outcomes.append(inbox.seal(revision))
        if not accept_first:
            first_done.set()
    a = threading.Thread(target=send); b = threading.Thread(target=finish)
    a.start(); b.start(); a.join(3); b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert set(outcomes) == ({'accepted', False} if accept_first else {'rejected', True})


def test_close_reports_unprocessed_and_durable_failure_never_accepts():
    log = []
    inbox = ResearchInput('r', log.append)
    inbox.accept('u1', '新的要求')
    inbox.close('cancelled')
    assert inbox.snapshot()['messages'][0]['status'] == 'undelivered'
    assert inbox.snapshot()['messages'][0]['reason'] == 'cancelled'
    def broken(_):
        raise OSError('disk full')
    failed = ResearchInput('r', broken)
    with pytest.raises(LLMPersistenceError):
        failed.accept('u1', '不能假装接收')
    assert failed.snapshot()['messages'] == []
    with pytest.raises(LLMPersistenceError):
        failed.begin_request('call1')


def test_wrapup_claims_every_accepted_message_and_closes_admission():
    inbox = ResearchInput('r', lambda _: None)
    inbox.accept('u1', '只查齿科')
    inbox.accept('u2', '还是加上医美')
    rows, revision = inbox.begin_request('wrapup', close=True)
    assert revision == 2
    assert [r['text'] for r in rows] == ['只查齿科', '还是加上医美']
    with pytest.raises(ValueError):
        inbox.accept('u3', '太晚到达')
