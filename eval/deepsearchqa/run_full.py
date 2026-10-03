"""全量队列：独立研究进程 + 并发裁判，逐题落盘、续跑、防止重复启动。"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import time

from dra.durable import atomic_json
from dra.paths import RUNS_DIR
from judge import error_info, grade_one, summarize
from prepare import NOTEBOOK_SHA256, extract_prompt
from run_pilot import HERE, _worker, load_questions


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_text())


def fingerprint(response):
    return hashlib.sha256(response.encode()).hexdigest()


def archive_judge_errors(path, ids=None):
    if not path.exists():
        return
    saved = read(path)
    failed = {k: g for k, g in saved['grades'].items()
              if g['status'] == 'judge_error' and (ids is None or k in ids)}
    if failed:
        with (path.parent / 'retry-history.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': now(), 'judge': saved['judge'], 'grades': failed}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        saved['grades'] = {k: g for k, g in saved['grades'].items() if k not in failed}
        atomic_json(path, saved)


@contextmanager
def batch_lock(batch):
    with (batch / 'runner.lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('This batch already has an active runner')
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield


def health(record):
    path = RUNS_DIR / record.get('run_id', '_missing') / 'run.json'
    calls = read(path).get('calls', []) if path.exists() else []
    lead_errors = [c.get('error') for c in calls if c.get('role') == 'lead' and c.get('status') == 'failed']
    codes = Counter()
    for call in calls:
        codes.update(re.findall(r'(?:Error code: |status code[ :]+|HTTP )(\d{3})', call.get('error') or '', re.I))
    if record.get('http_status'):
        codes[str(record['http_status'])] += 1
    return {'failed_calls': sum(c.get('status') == 'failed' for c in calls),
            'retried_calls': sum((c.get('attempts') or 1) > 1 for c in calls),
            # The product reports both request timeouts and exhausted run budgets as
            # request_timeout. A recorded run deadline is a scored delivery failure,
            # not evidence of a provider outage that should stop the whole queue.
            'deadline_exhausted': bool(lead_errors and lead_errors[-1] == 'LLMDeadlineExceeded'),
            'http_errors': dict(codes),
            'account_error': bool(record.get('provider_balance_error') or set(codes) & {'401', '402', '403'})}


def prepare_batch(batch, reference, rows, judge_identity, *, allow_partial=False, prompt_profile=None):
    source = read(reference / 'manifest.json')['protocol']
    questions = [{k: r[k] for k in ('id', 'problem', 'problem_category')} for r in rows]
    random.Random(source['seed']).shuffle(questions)
    protocol = {**source, 'questions': questions, 'excluded_ids': [],
                'selection': {'method': 'all', 'count': len(questions)},
                'reference_batch': reference.name, 'judge': judge_identity}
    if prompt_profile:
        from dra import research_prompts
        from normalize import VERSION, PROMPT
        protocol['config'] = {**source['config'], 'prompt_profile': prompt_profile}
        protocol['continuation'] = {
            'policy': 'reuse every saved reference result, without score selection; new research uses selected profile',
            'reference_manifest_sha256': hashlib.sha256((reference / 'manifest.json').read_bytes()).hexdigest(),
            'prompt_profile': prompt_profile,
            'profile_source_sha256': hashlib.sha256(Path(research_prompts.__file__).read_bytes()).hexdigest(),
            'normalization': VERSION, 'normalizer_prompt_sha256': fingerprint(PROMPT),
            'mixed_research_protocol': True}
    path = batch / 'manifest.json'
    if path.exists():
        if read(path)['protocol'] != protocol:
            raise ValueError('Existing full batch protocol differs')
    else:
        atomic_json(path, {'protocol': protocol, 'started_at': now(),
                          'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=HERE, text=True).strip(),
                          'tracked_diff_sha256': hashlib.sha256(subprocess.check_output(
                              ['git', 'diff', '--binary'], cwd=HERE)).hexdigest(),
                          'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    # 导入整个技术试跑批次，包括失败题；不挑高分、不择优重试。
    by_id = {q['id']: q for q in questions}
    seed_ids = []
    for question in source['questions']:
        qid = question['id']
        if qid not in by_id or question != by_id[qid]:
            raise ValueError('Seed question differs from the dataset')
        source_result = reference / str(qid) / 'result.json'
        if not source_result.exists():
            if allow_partial:
                continue
            raise ValueError('Concurrency test is not complete')
        target = batch / str(qid)
        record = read(source_result)
        if (target / 'result.json').exists():
            if read(target / 'result.json') != record:
                raise ValueError('Seed result differs; original result must be preserved')
        else:
            target.mkdir(exist_ok=True)
            for name in ('answer.md', 'report.md', 'progress.json', 'normalized.json', 'submitted-answer.md'):
                if (reference / str(qid) / name).exists():
                    shutil.copy2(reference / str(qid) / name, target / name)
            atomic_json(target / 'result.json', record)
        seed_ids.append(qid)
    atomic_json(batch / 'seed_results.json', {'batch': reference.name, 'ids': seed_ids,
                                              'policy': 'all reference results, including failures'})
    # If formatting and judge configuration are identical, reuse every already-scored input.
    # Old raw-report grades (no normalization protocol) cannot enter this path.
    reference_grades = reference / 'judge/results.json'
    target_grades = batch / 'judge/results.json'
    if prompt_profile and reference_grades.exists() and not target_grades.exists():
        saved = read(reference_grades)
        if saved['judge'] == judge_identity:
            for qid, grade in saved['grades'].items():
                record = read(batch / qid / 'result.json')
                if grade['response_sha256'] != fingerprint(record['response']):
                    raise ValueError('Reference grade answer fingerprint differs')
                if grade['status'] != 'judge_error':
                    normalized = read(batch / qid / 'normalized.json')
                    if (normalized['raw_sha256'] != fingerprint(record['response'])
                            or grade['normalized_sha256'] != fingerprint(normalized['answer'])):
                        raise ValueError('Reference normalized input differs')
            target_grades.parent.mkdir(exist_ok=True)
            atomic_json(target_grades, saved)
            atomic_json(batch / 'imported-grades.json', {'batch': reference.name,
                        'ids': list(saved['grades']), 'policy': 'identical judge and normalized input; no score selection'})
    return protocol


def archive_interrupted(batch, qid, record):
    """Only explicit infrastructure interruptions may be researched again; preserve every attempt."""
    target = batch / 'attempts' / str(qid)
    target.mkdir(parents=True, exist_ok=True)
    destination = target / record.get('run_id', f'attempt-{time.time_ns()}')
    (batch / str(qid)).rename(destination)
    return destination


def run_queue(batch, protocol, rows, templates, judge_identity, *, workers, judge_workers,
              worker_fn=_worker, grade_fn=grade_one, research_executor=ProcessPoolExecutor,
              poll_seconds=5, min_free_bytes=2 * 1024**3, max_new_questions=None):
    questions = protocol['questions']
    expected = {q['id']: q for q in questions}
    records = {}
    for qid, question in expected.items():
        path = batch / str(qid) / 'result.json'
        if path.exists():
            record = read(path)
            if any(record.get(k) != v for k, v in question.items()):
                raise ValueError('Saved research result identity differs')
            if record['status'] == 'infrastructure_interrupted':
                archive_interrupted(batch, qid, record)
                continue
            records[qid] = record
    judge_dir = batch / 'judge'
    judge_dir.mkdir(exist_ok=True)
    judge_path = judge_dir / 'results.json'
    saved = read(judge_path) if judge_path.exists() else {'judge': judge_identity, 'grades': {}}
    if saved['judge'] != judge_identity:
        raise ValueError('Existing judge differs')
    grades = saved['grades']
    for qid, grade in grades.items():
        if int(qid) not in records or grade.get('response_sha256') != fingerprint(records[int(qid)]['response']):
            raise ValueError('Answer changed after grading')
        if 'normalized_sha256' in grade:
            normalized = read(batch / qid / 'normalized.json')
            if (normalized['raw_sha256'] != grade['response_sha256']
                    or fingerprint(normalized['answer']) != grade['normalized_sha256']):
                raise ValueError('Normalized answer changed after grading')
    gold = {r['id']: r for r in rows}
    todo = [q for q in questions if q['id'] not in records]
    if max_new_questions is not None:
        todo = todo[:max_new_questions]
    research_pending, judge_pending = {}, {}
    diagnostics = {str(qid): health(r) for qid, r in records.items()}
    start = time.monotonic()
    stop_reason = None
    judge_failures = 0
    research_failures = 0
    log_at = 0
    executor_options = {'max_workers': workers}
    if research_executor is ProcessPoolExecutor:
        executor_options['mp_context'] = multiprocessing.get_context('spawn')
    session = {'pid': os.getpid(), 'started_at': now(), 'workers': workers, 'judge_workers': judge_workers,
               'initial_completed': len(records)}
    with (batch / 'sessions.jsonl').open('a') as stream:
        stream.write(json.dumps(session) + '\n')
    with research_executor(**executor_options) as pool, ThreadPoolExecutor(max_workers=judge_workers) as judges:
        while True:
            if (batch / 'STOP').exists():
                stop_reason = stop_reason or 'stop_file'
            if (batch / 'quota-pause.json').exists():
                stop_reason = stop_reason or read(batch / 'quota-pause.json')['reason']
            if shutil.disk_usage(batch).free < min_free_bytes:
                stop_reason = stop_reason or 'low_disk_space'
            if not stop_reason:
                while todo and len(research_pending) < workers:
                    question = todo.pop(0)
                    future = pool.submit(worker_fn, question, batch / str(question['id']), protocol['config'])
                    research_pending[future] = (question, time.monotonic())
            if not stop_reason or stop_reason in {'subscription_headroom', 'provider_limit', 'quota_check_unavailable',
                                                  'subscription_and_credit_exhausted'}:
                queued = set(judge_pending.values())
                for qid, record in records.items():
                    if len(judge_pending) >= judge_workers:
                        break
                    if str(qid) not in grades and qid not in queued:
                        future = judges.submit(grade_fn, gold[qid], record['response'], templates,
                                               model=judge_identity['model'], provider=judge_identity['provider'])
                        judge_pending[future] = qid
            active = [*research_pending, *judge_pending]
            done, _ = wait(active, timeout=poll_seconds, return_when=FIRST_COMPLETED) if active else ([], [])
            for future in done:
                if future in research_pending:
                    question, dispatched = research_pending.pop(future)
                    try:
                        record = future.result()
                    except Exception as exc:
                        record = {**question, 'response': '', 'status': 'error', 'searches': 0,
                                  'wall_seconds': round(time.monotonic() - dispatched, 2),
                                  'error': type(exc).__name__, 'usage': {}}
                        (batch / str(question['id'])).mkdir(exist_ok=True)
                        atomic_json(batch / str(question['id']) / 'result.json', record)
                        stop_reason = stop_reason or 'research_worker_crashed'
                    if record['status'] == 'infrastructure_interrupted':
                        archive_interrupted(batch, question['id'], record)
                        stop_reason = stop_reason or record['interruption']['reason']
                        print(json.dumps({'event': 'research_interrupted', 'id': question['id'],
                                          'reason': stop_reason}), flush=True)
                        continue
                    records[question['id']] = record
                    diagnostic = health(record)
                    diagnostics[str(question['id'])] = diagnostic
                    if diagnostic['account_error']:
                        stop_reason = stop_reason or 'provider_account_error'
                    infrastructure_failure = record['status'] == 'error' or (
                        not record['response'] and (
                            record.get('stop_reason') in {'failed', 'protocol_error'} or (
                                record.get('stop_reason') == 'request_timeout'
                                and not diagnostic['deadline_exhausted'])))
                    research_failures = research_failures + 1 if infrastructure_failure else 0
                    if research_failures >= 3:
                        stop_reason = stop_reason or 'repeated_research_errors'
                    print(json.dumps({'event': 'research_done', 'id': question['id'],
                                      'status': record['status'], 'seconds': record['wall_seconds'],
                                      'health': diagnostic}), flush=True)
                else:
                    qid = judge_pending.pop(future)
                    try:
                        grade = future.result()
                    except Exception as exc:
                        grade = {'status': 'judge_error', 'errors': error_info(exc)}
                    grade['response_sha256'] = fingerprint(records[qid]['response'])
                    grades[str(qid)] = grade
                    atomic_json(judge_path, saved)
                    judge_failures = judge_failures + 1 if grade['status'] == 'judge_error' else 0
                    if judge_failures >= 3:
                        stop_reason = stop_reason or 'repeated_judge_errors'
                    print(json.dumps({'event': 'judge_done', 'id': qid, 'status': grade['status'],
                                      'f1': grade.get('f1')}), flush=True)
            complete = len(records) == len(questions) and len(grades) == len(questions)
            idle = not research_pending and not judge_pending
            if (idle and not todo and not complete and max_new_questions is not None
                    and all(str(qid) in grades for qid in records)):
                stop_reason = stop_reason or 'research_batch_limit'
            state = {'pid': os.getpid(), 'updated_at': now(), 'expected_questions': len(questions),
                     'research_completed': len(records), 'answers_produced': sum(bool(r['response']) for r in records.values()),
                     'graded': sum('f1' in g for g in grades.values()),
                     'judge_errors': [int(k) for k, g in grades.items() if g['status'] == 'judge_error'],
                     'active_research_ids': [q['id'] for q, _ in research_pending.values()],
                     'active_judge_ids': list(judge_pending.values()),
                     'remaining_research': len(questions) - len(records) - len(research_pending),
                     'workers': workers, 'judge_workers': judge_workers,
                     'status': 'completed' if complete and idle else 'stopping' if stop_reason else 'running',
                     'stop_reason': stop_reason, 'session_seconds': round(time.monotonic() - start, 2)}
            if complete and state['judge_errors']:
                state['status'] = 'needs_judge_retry'
            if stop_reason and idle:
                state['status'] = 'paused'
            atomic_json(batch / 'status.json', state)
            atomic_json(batch / 'health.json', diagnostics)
            if records:
                summary = {'judge': judge_identity, **summarize(list(records.values()), grades),
                           'expected_questions': len(questions), 'research_complete': len(records) == len(questions)}
                if len(records) != len(questions):
                    summary['mean_f1'] = summary['fully_correct_rate'] = None
                scored = [g for g in grades.values() if 'f1' in g]
                summary['scored_subset_count'] = len(scored)
                summary['scored_subset_mean_f1'] = sum(g['f1'] for g in scored) / len(scored) if scored else None
                summary['cohorts'] = {}
                for profile in {r.get('prompt_profile', 'product') for r in records.values()}:
                    group = [r for r in records.values() if r.get('prompt_profile', 'product') == profile]
                    group_grades = [grades[str(r['id'])] for r in group if 'f1' in grades.get(str(r['id']), {})]
                    summary['cohorts'][profile] = {'research_completed': len(group), 'graded': len(group_grades),
                        'scored_subset_mean_f1': sum(g['f1'] for g in group_grades) / len(group_grades) if group_grades else None}
                atomic_json(batch / 'summary.json', summary)
            if time.monotonic() - log_at >= 30:
                print(json.dumps({'event': 'progress', **state}), flush=True)
                log_at = time.monotonic()
            pending_grades = any(str(qid) not in grades for qid in records)
            can_grade = stop_reason in {'subscription_headroom', 'provider_limit', 'quota_check_unavailable',
                                       'subscription_and_credit_exhausted'}
            if idle and (complete or stop_reason) and not (can_grade and pending_grades):
                return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', default='full-20261002')
    parser.add_argument('--reference-batch', default='parallel8-20261002')
    parser.add_argument('--workers', type=int, default=16)
    parser.add_argument('--judge-workers', type=int, default=4)
    parser.add_argument('--judge-model', default='gpt-5.5')
    parser.add_argument('--judge-provider', default='codex521')
    parser.add_argument('--retry-judge-errors', action='store_true',
                        help='只重试裁判故障；先归档原始错误，不重评已有分数')
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', v) for v in (args.batch, args.reference_batch)):
        parser.error('Invalid batch name')
    if args.workers < 1 or args.judge_workers < 1 or args.batch == args.reference_batch:
        parser.error('Invalid workers or identical batches')
    rows = load_questions()
    identity = {'model': args.judge_model, 'provider': args.judge_provider,
                'official_judge_model': args.judge_model == 'gemini-2.5-flash',
                'notebook_sha256': NOTEBOOK_SHA256}
    notebook = (HERE / '.official/starter.ipynb').read_text()
    if fingerprint(notebook) != NOTEBOOK_SHA256:
        raise ValueError('Official notebook checksum mismatch')
    templates = extract_prompt(notebook)
    print(json.dumps({'questions': len(rows), 'workers': args.workers, 'judge_workers': args.judge_workers,
                      'judge': identity, 'run_live': args.run_live}), flush=True)
    if not args.run_live:
        return
    batch = HERE / 'runs' / args.batch
    batch.mkdir(parents=True, exist_ok=True)
    with batch_lock(batch):
        protocol = prepare_batch(batch, HERE / 'runs' / args.reference_batch, rows, identity)
        if args.retry_judge_errors:
            path = batch / 'judge/results.json'
            if path.exists() and read(path)['judge'] != identity:
                raise ValueError('Cannot retry with a different judge')
            archive_judge_errors(path)
        final = run_queue(batch, protocol, rows, templates, identity,
                          workers=args.workers, judge_workers=args.judge_workers)
        print(json.dumps(final), flush=True)


if __name__ == '__main__':
    main()
