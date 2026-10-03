"""离线验证并发评测不会重复跑题、丢失败题或把 gold 交给研究端。"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import threading
import time

import pytest


@pytest.fixture
def runner(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / 'eval/deepsearchqa'
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location('dsqa_full_test', directory / 'run_full.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_run(tmp_path, runner):
    rows = [{'id': i, 'problem': f'Question {i}', 'problem_category': 'Test',
             'answer': 'SECRET GOLD', 'answer_type': 'Single Answer'} for i in range(4)]
    questions = [{k: r[k] for k in ('id', 'problem', 'problem_category')} for r in rows]
    protocol = {'questions': questions, 'config': {}}
    identity = {'model': 'offline', 'provider': 'offline'}
    return rows, questions, protocol, identity


def test_parallel_results_are_saved_once_and_resume_does_no_work(tmp_path, runner):
    rows, questions, protocol, identity = setup_run(tmp_path, runner)
    lock = threading.Lock()
    active = peak = 0
    researched, graded = [], []

    def worker(q, out, config):
        nonlocal active, peak
        assert set(q) == {'id', 'problem', 'problem_category'}
        with lock:
            active += 1
            peak = max(peak, active)
            researched.append(q['id'])
        time.sleep(.03)
        result = {**q, 'response': '' if q['id'] == 1 else 'Answer',
                  'status': 'no_answer' if q['id'] == 1 else 'completed',
                  'wall_seconds': .03, 'searches': 1, 'usage': {}}
        out.mkdir(exist_ok=True)
        runner.atomic_json(out / 'result.json', result)
        with lock:
            active -= 1
        return result

    def grade(q, response, templates, **kwargs):
        assert q['answer'] == 'SECRET GOLD'
        graded.append(q['id'])
        return {'status': 'graded' if response else 'empty_answer', 'f1': float(bool(response)),
                'category': 'fully_correct' if response else 'fully_incorrect'}

    options = dict(workers=2, judge_workers=2, worker_fn=worker, grade_fn=grade,
                   research_executor=ThreadPoolExecutor, poll_seconds=.01, min_free_bytes=0)
    state = runner.run_queue(tmp_path, protocol, rows, {}, identity, **options)
    assert state['status'] == 'completed'
    assert peak == 2 and sorted(researched) == list(range(4)) and sorted(graded) == list(range(4))
    summary = runner.read(tmp_path / 'summary.json')
    assert summary['sample_size'] == 4 and summary['mean_f1'] == .75
    assert summary['answers_produced'] == 3
    researched.clear()
    graded.clear()
    runner.run_queue(tmp_path, protocol, rows, {}, identity, **options)
    assert researched == graded == []
    record = runner.read(tmp_path / '0/result.json')
    record['response'] = 'Edited later'
    runner.atomic_json(tmp_path / '0/result.json', record)
    with pytest.raises(ValueError, match='Answer changed'):
        runner.run_queue(tmp_path, protocol, rows, {}, identity, **options)


def test_account_failure_stops_new_dispatch_and_preserves_denominator(tmp_path, runner):
    rows, questions, protocol, identity = setup_run(tmp_path, runner)
    def worker(q, out, config):
        out.mkdir(exist_ok=True)
        result = {**q, 'response': '', 'status': 'no_answer', 'wall_seconds': .01,
                  'searches': 0, 'provider_balance_error': True}
        runner.atomic_json(out / 'result.json', result)
        return result
    state = runner.run_queue(tmp_path, protocol, rows, {}, identity, workers=1, judge_workers=1,
                             worker_fn=worker, research_executor=ThreadPoolExecutor,
                             grade_fn=lambda *a, **k: pytest.fail('Should stop before new grading'),
                             poll_seconds=.01, min_free_bytes=0)
    assert state['status'] == 'paused' and state['stop_reason'] == 'provider_account_error'
    assert state['research_completed'] == 1 and state['remaining_research'] == 3
    assert runner.read(tmp_path / 'summary.json')['mean_f1'] is None


@pytest.mark.parametrize('errors,expected_completed', [
    (['LLMDeadlineExceeded'] * 3, 4),
    (['LLMRequestTimeout', 'LLMDeadlineExceeded', 'LLMDeadlineExceeded'], 4),
    (['LLMRequestTimeout'] * 3, 3),
    ([None] * 3, 3),  # Missing diagnostics must not hide a possible outage.
])
def test_run_deadlines_keep_zero_scores_without_stopping_remaining_questions(
        tmp_path, runner, monkeypatch, errors, expected_completed):
    rows, questions, protocol, identity = setup_run(tmp_path, runner)
    batch = tmp_path / 'batch'
    batch.mkdir()
    runs = tmp_path / 'traces'
    monkeypatch.setattr(runner, 'RUNS_DIR', runs)
    researched = []

    def worker(q, out, config):
        researched.append(q['id'])
        failed = q['id'] < 3
        result = {**q, 'run_id': f"run-{q['id']}", 'response': '' if failed else 'Answer',
                  'status': 'no_answer' if failed else 'completed',
                  'stop_reason': 'request_timeout' if failed else 'finished',
                  'wall_seconds': 600, 'searches': 0, 'usage': {}}
        out.mkdir(exist_ok=True)
        runner.atomic_json(out / 'result.json', result)
        if failed and errors[q['id']]:
            trace = runs / result['run_id']
            trace.mkdir(parents=True)
            runner.atomic_json(trace / 'run.json', {'calls': [
                {'role': 'lead', 'status': 'failed', 'error': errors[q['id']]}]})
        return result

    def grade(q, response, templates, **kwargs):
        return {'status': 'graded' if response else 'empty_answer', 'f1': float(bool(response)),
                'category': 'fully_correct' if response else 'fully_incorrect'}

    options = dict(workers=1, judge_workers=1, worker_fn=worker, grade_fn=grade,
                   research_executor=ThreadPoolExecutor, poll_seconds=.01, min_free_bytes=0)
    state = runner.run_queue(batch, protocol, rows, {}, identity, **options)
    assert researched == list(range(expected_completed))
    assert state['research_completed'] == expected_completed
    if expected_completed == 4:
        assert state['status'] == 'completed' and state['stop_reason'] is None
        grades = runner.read(batch / 'judge/results.json')['grades']
        assert [grades[str(i)]['f1'] for i in range(4)] == [0., 0., 0., 1.]
        assert runner.read(batch / 'summary.json')['mean_f1'] == .25
        researched.clear()
        runner.run_queue(batch, protocol, rows, {}, identity, **options)
        assert researched == []  # Budget failures remain final; they are never re-researched.
    else:
        assert state['status'] == 'paused' and state['stop_reason'] == 'repeated_research_errors'
        assert state['remaining_research'] == 1


def test_batch_lock_rejects_duplicate_runner(tmp_path, runner):
    with runner.batch_lock(tmp_path):
        with pytest.raises(SystemExit, match='active runner'):
            with runner.batch_lock(tmp_path):
                pass


def test_judge_retry_archives_errors_and_preserves_scores(tmp_path, runner):
    path = tmp_path / 'results.json'
    saved = {'judge': {'model': 'offline'}, 'grades': {
        '1': {'status': 'graded', 'f1': 0.0, 'response_sha256': 'keep'},
        '2': {'status': 'judge_error', 'errors': ['TimeoutError'], 'response_sha256': 'retry'}}}
    runner.atomic_json(path, saved)
    runner.archive_judge_errors(path)
    assert runner.read(path)['grades'] == {'1': saved['grades']['1']}
    history = [json.loads(line) for line in (tmp_path / 'retry-history.jsonl').read_text().splitlines()]
    assert history[0]['grades'] == {'2': saved['grades']['2']}


def test_seed_import_keeps_failed_records_and_cannot_overwrite(tmp_path, runner, monkeypatch):
    rows, questions, protocol, identity = setup_run(tmp_path, runner)
    reference, batch = tmp_path / 'reference', tmp_path / 'full'
    reference.mkdir()
    batch.mkdir()
    source = {**protocol, 'questions': questions[:2], 'seed': 42}
    runner.atomic_json(reference / 'manifest.json', {'protocol': source})
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *a, **k: 'commit' if k.get('text') else b'diff')
    for q in questions[:2]:
        path = reference / str(q['id'])
        path.mkdir()
        runner.atomic_json(path / 'result.json', {**q, 'response': '', 'status': 'no_answer'})
    result = runner.prepare_batch(batch, reference, rows, identity)
    assert len(result['questions']) == 4
    assert runner.read(batch / '1/result.json')['status'] == 'no_answer'
    assert runner.read(batch / 'seed_results.json')['ids'] == [0, 1]
    runner.atomic_json(batch / '1/result.json', {'response': 'Different answer'})
    with pytest.raises(ValueError, match='must be preserved'):
        runner.prepare_batch(batch, reference, rows, identity)
