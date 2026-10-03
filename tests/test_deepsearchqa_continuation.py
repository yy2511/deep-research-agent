import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.fixture
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'eval/deepsearchqa'))
    return [importlib.import_module(name) for name in ('normalize', 'quota', 'resume', 'run_full')]


def test_explicit_answers_need_no_extra_model_and_verbatim_extraction_cannot_invent(mods):
    norm = mods[0]
    fail = lambda *a, **k: pytest.fail('Tagged answers do not need a formatting model')
    result = norm.normalize('Question', '<final_answer>Alpha [c1]\nBeta [c4]</final_answer>',
                            model='test', provider='test', call=fail)
    assert result['answer'] == 'Alpha \nBeta'
    assert result['method'] == 'explicit_container'
    seen = []
    def extract(messages, **kwargs):
        seen.extend(messages)
        return json.dumps({'answer_spans': ['Alpha', 'Beta']})
    result = norm.normalize('Question', 'Answer: Alpha and Beta. Excluded: Gamma.',
                            model='test', provider='test', call=extract)
    assert result['answer'] == 'Alpha\nBeta' and 'reference answer' not in seen[1]['content']
    with pytest.raises(norm.NormalizationError) as error:
        norm.normalize('Question', 'Alpha', model='test', provider='test',
                       call=lambda *a, **k: '{"answer_spans":["Invented"]}')
    assert 'Invented' in error.value.artifact['raw_output']


def usage(rolling=10, monthly=10):
    return {name: {'status': 'ok', 'percent': value, 'resetsAt': reset} for name, value, reset in (
        ('rolling', rolling, '2030-01-01T05:00:00Z'), ('weekly', 10, '2030-01-07T00:00:00Z'),
        ('monthly', monthly, '2030-02-01T00:00:00Z'))}


def test_guard_stops_before_request_and_retains_latest_reset(mods, tmp_path):
    quota = mods[1]
    blocked = usage(rolling=99, monthly=99)
    cancel = threading.Event()
    guard = quota.Guard(tmp_path, cancel, fetch=lambda: blocked)
    with pytest.raises(quota.QuotaPause):
        guard.wrap(lambda *a, **k: pytest.fail('No inference after quota limit'))(provider='opencode')
    assert cancel.is_set()
    pause = json.loads((tmp_path / 'quota-pause.json').read_text())
    assert pause['windows'] == ['rolling', 'monthly']
    assert pause['retry_at'] == quota.epoch(blocked['monthly']['resetsAt']) + 60
    # Another worker observes the shared pause without checking usage or calling inference.
    second = quota.Guard(tmp_path, threading.Event(), fetch=lambda: pytest.fail('Must not probe early'))
    with pytest.raises(quota.QuotaPause):
        second.acquire()


def test_failed_quota_check_fails_closed_and_reset_is_respected(mods, tmp_path):
    _, quota, resume, _ = mods
    now = 1800000000.0
    fail = lambda: (_ for _ in ()).throw(TimeoutError())
    assert not resume.ready(tmp_path, fetch=fail, clock=lambda: now)
    assert not resume.ready(tmp_path, fetch=lambda: pytest.fail('Must wait'), clock=lambda: now + 10)
    assert resume.ready(tmp_path, fetch=lambda: usage(), clock=lambda: now + 1000)
    assert not (tmp_path / 'quota-pause.json').exists()
    assert (tmp_path / 'quota-history.jsonl').exists()


@pytest.mark.parametrize('status', [401, 402])
def test_only_authorized_existing_credit_can_cross_quota_and_credits_error_disables_it(mods, tmp_path, status):
    quota, runner = mods[1], mods[3]
    assert not quota.credit_allowed(tmp_path)
    runner.atomic_json(tmp_path / 'budget-authorization.json', {
        'allow_existing_credit_usd': 5, 'auto_recharge_verified_disabled': True, 'allow_recharge': False})
    assert quota.credit_allowed(tmp_path)
    blocked = usage(rolling=100, monthly=100)
    assert quota.pause_for_usage(blocked, use_credit=True) is None
    class PaymentRequired(RuntimeError):
        status_code = status
        body = {'error': {'type': 'CreditsError'}}
        response = None
    def no_balance(**kwargs):
        raise PaymentRequired('balance exhausted')
    guard = quota.Guard(tmp_path, threading.Event(), fetch=lambda: blocked)
    with pytest.raises(quota.QuotaPause):
        guard.wrap(no_balance)(provider='opencode')
    assert not quota.credit_allowed(tmp_path)
    pause = runner.read(tmp_path / 'quota-pause.json')
    assert pause['reason'] == 'subscription_and_credit_exhausted'
    assert pause['retry_at'] == quota.epoch(blocked['monthly']['resetsAt']) + 60
    assert runner.read(tmp_path / 'quota-state.json')['active'] == {}


def test_auth_error_is_not_treated_as_quota_reset(mods, tmp_path):
    quota, runner = mods[1], mods[3]
    runner.atomic_json(tmp_path / 'budget-authorization.json', {
        'allow_existing_credit_usd': 5, 'auto_recharge_verified_disabled': True, 'allow_recharge': False})
    class Unauthorized(RuntimeError):
        status_code = 401
        response = None
        body = {'error': {'type': 'AuthError'}}
    def denied(**kwargs):
        raise Unauthorized()
    with pytest.raises(quota.QuotaPause):
        quota.Guard(tmp_path, threading.Event(), fetch=lambda: usage()).wrap(denied)(provider='opencode')
    pause = runner.read(tmp_path / 'quota-pause.json')
    assert pause['reason'] == 'provider_account_error' and pause['retry_at'] is None
    assert not (tmp_path / 'credit-exhausted.json').exists()


def test_interruption_is_archived_ungraded_and_success_is_reused(mods, tmp_path):
    runner = mods[3]
    q = {'id': 1, 'problem': 'Question', 'problem_category': 'Test'}
    rows = [{**q, 'answer': 'GOLD', 'answer_type': 'Single Answer'}]
    protocol = {'questions': [q], 'config': {}}
    identity = {'model': 'test', 'provider': 'test'}
    def interrupted(question, out, config):
        out.mkdir()
        record = {**question, 'run_id': 'attempt1', 'response': 'partial retained for audit',
                  'status': 'infrastructure_interrupted', 'interruption': {'reason': 'provider_limit'}}
        runner.atomic_json(out / 'result.json', record)
        return record
    options = dict(workers=1, judge_workers=1, research_executor=ThreadPoolExecutor,
                   poll_seconds=.01, min_free_bytes=0)
    state = runner.run_queue(tmp_path, protocol, rows, {}, identity, worker_fn=interrupted,
                             grade_fn=lambda *a, **k: pytest.fail('Interrupted answer must not be scored'), **options)
    assert state['research_completed'] == 0 and state['remaining_research'] == 1
    assert (tmp_path / 'attempts/1/attempt1/result.json').exists()
    assert not (tmp_path / '1/result.json').exists()
    def completed(question, out, config):
        out.mkdir()
        record = {**question, 'response': 'Alpha', 'status': 'completed', 'wall_seconds': 1, 'searches': 1}
        runner.atomic_json(out / 'result.json', record)
        return record
    state = runner.run_queue(tmp_path, protocol, rows, {}, identity, worker_fn=completed,
                             grade_fn=lambda *a, **k: {'status': 'graded', 'f1': 0, 'category': 'fully_incorrect'}, **options)
    assert state['status'] == 'completed'
    runner.run_queue(tmp_path, protocol, rows, {}, identity,
                     worker_fn=lambda *a: pytest.fail('No retry for wrong answers'), **options)


def test_partial_continuation_preserves_source_and_separates_protocol(mods, tmp_path, monkeypatch):
    runner = mods[3]
    reference, batch = tmp_path / 'source', tmp_path / 'new'
    reference.mkdir(); batch.mkdir()
    questions = [{'id': i, 'problem': str(i), 'problem_category': 'Test'} for i in range(3)]
    runner.atomic_json(reference / 'manifest.json', {'protocol': {'questions': questions, 'config': {}, 'seed': 1}})
    (reference / '1').mkdir()
    old = {**questions[1], 'response': 'Keep this answer', 'status': 'completed'}
    runner.atomic_json(reference / '1/result.json', old)
    original = (reference / '1/result.json').read_bytes()
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *a, **k: 'commit' if k.get('text') else b'diff')
    protocol = runner.prepare_batch(batch, reference, questions, {}, allow_partial=True, prompt_profile='concise_qa_v1')
    assert protocol['continuation']['mixed_research_protocol']
    assert protocol['config']['prompt_profile'] == 'concise_qa_v1'
    assert runner.read(batch / 'seed_results.json')['ids'] == [1]
    assert (reference / '1/result.json').read_bytes() == original
    assert runner.read(batch / '1/result.json') == old


def test_identical_normalized_grades_are_imported_including_zero(mods, tmp_path, monkeypatch):
    runner = mods[3]
    reference, batch = tmp_path / 'source', tmp_path / 'new'
    reference.mkdir(); batch.mkdir()
    question = {'id': 1, 'problem': 'Question', 'problem_category': 'Test'}
    identity = {'model': 'test', 'normalization': 'extractive-v1'}
    source = {'protocol': {'questions': [question], 'config': {}, 'seed': 1, 'judge': identity}}
    runner.atomic_json(reference / 'manifest.json', source)
    (reference / '1').mkdir(); (reference / 'judge').mkdir()
    record = {**question, 'response': 'Alpha [c1]', 'status': 'completed'}
    runner.atomic_json(reference / '1/result.json', record)
    normalized = {'answer': 'Alpha', 'raw_sha256': runner.fingerprint(record['response'])}
    runner.atomic_json(reference / '1/normalized.json', normalized)
    grade = {'status': 'graded', 'f1': 0, 'response_sha256': normalized['raw_sha256'],
             'normalized_sha256': runner.fingerprint('Alpha')}
    runner.atomic_json(reference / 'judge/results.json', {'judge': identity, 'grades': {'1': grade}})
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *a, **k: 'commit' if k.get('text') else b'diff')
    protocol = runner.prepare_batch(batch, reference, [question], identity, allow_partial=True, prompt_profile='concise_qa_v2')
    assert runner.read(batch / 'judge/results.json')['grades']['1'] == grade
    runner.atomic_json(batch / '1/normalized.json', {**normalized, 'answer': 'Edited after scoring'})
    with pytest.raises(ValueError, match='Normalized answer changed'):
        runner.run_queue(batch, protocol, [question], {}, identity, workers=1, judge_workers=1)
