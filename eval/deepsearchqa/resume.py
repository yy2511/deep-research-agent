"""Versioned continuation with quota-aware sleeps and bounded infrastructure retries.

只续跑已经建立的旧批次（concise-v2-20261003），按该批次冻结的协议继续：提示词、研究配置、
评分方式和复用的参考结果都取自批次记录，不在这里写死。它不能新建批次——旧协议沿用 JEV 开启、
GPT-5.5 抽取评分和旧参考答案，与 2026-10-06 选定的产品提示词（原提示词 + Claude 修改）及现行
评分口径不一致。新的全量评测需另建使用选定提示词的入口。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time

from dra.durable import atomic_json
from normalize import NormalizedGrader, VERSION
from prepare import NOTEBOOK_SHA256, extract_prompt
from quota import MAX_CALLS, THRESHOLDS, credit_allowed, fetch_usage, guarded_worker, pause_for_usage
from run_full import (archive_judge_errors, batch_lock, fingerprint, now, prepare_batch, read, run_queue)
from run_pilot import HERE, load_questions


def ready(batch, *, fetch=fetch_usage, clock=time.time):
    """Read-only preflight, including after restarts. Unknown quota fails closed."""
    path = batch / 'quota-pause.json'
    previous = read(path) if path.exists() else None
    if previous and (previous.get('retry_at') is None or previous['retry_at'] > clock()):
        return False
    try:
        usage = fetch()
        pause = pause_for_usage(usage, now=clock(), use_credit=credit_allowed(batch))
    except Exception as exc:
        pause = {'reason': 'quota_check_unavailable', 'error': type(exc).__name__,
                 'at': now(), 'retry_at': clock() + 900}
    if pause:
        atomic_json(path, pause)
        return False
    if previous:
        with (batch / 'quota-history.jsonl').open('a') as stream:
            stream.write(json.dumps({**previous, 'resumed_at': now()}) + '\n')
        path.unlink()
    return True


class KeepAwake:
    """Keep the overnight run awake; release the assertion during multi-day quota waits."""
    def __init__(self):
        self.process = None

    def start(self):
        if sys.platform == 'darwin' and (self.process is None or self.process.poll() is not None):
            self.process = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-s', '-w', str(os.getpid())],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()


def frozen_profile(batch):
    """已建批次冻结的提示词；没有批次记录时拒绝运行，不用旧协议新开批次。"""
    path = batch / 'manifest.json'
    if not path.exists():
        raise SystemExit(f'批次 {batch.name} 不存在。resume.py 只续跑已建立的旧批次（concise-v2-20261003），'
                         '不能新建批次：旧协议沿用 JEV 开启、GPT-5.5 抽取评分和旧参考答案，'
                         '与选定的产品提示词（原提示词 + Claude 修改）及现行评分口径不一致。'
                         '新的全量评测请另建使用选定提示词的入口。')
    profile = read(path)['protocol']['config'].get('prompt_profile')
    if not profile:
        raise SystemExit(f'批次 {batch.name} 没有记录续跑提示词，不能用 resume.py 续跑。')
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', default='concise-v2-20261003')
    parser.add_argument('--reference-batch', default='concise-20261003')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--judge-workers', type=int, default=4)
    parser.add_argument('--max-new-questions', type=int)
    parser.add_argument('--watch', action='store_true', help='Wait for quota reset, then continue automatically')
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    import re
    if (any(not re.fullmatch(r'[A-Za-z0-9_-]+', v) for v in (args.batch, args.reference_batch))
            or args.batch == args.reference_batch or min(args.workers, args.judge_workers) < 1
            or (args.max_new_questions is not None and args.max_new_questions < 0)):
        parser.error('Invalid batch or worker count')
    rows = load_questions()
    identity = {'model': 'gpt-5.5', 'provider': 'codex521', 'official_judge_model': False,
                'notebook_sha256': NOTEBOOK_SHA256, 'normalization': VERSION}
    notebook = (HERE / '.official/starter.ipynb').read_text()
    if fingerprint(notebook) != NOTEBOOK_SHA256:
        raise ValueError('Official notebook checksum mismatch')
    templates = extract_prompt(notebook)
    batch = HERE / 'runs' / args.batch
    profile = frozen_profile(batch)
    print(json.dumps({'questions': len(rows), 'profile': profile, 'judge': identity,
                      'workers': args.workers, 'judge_workers': args.judge_workers,
                      'run_live': args.run_live}), flush=True)
    if not args.run_live:
        return
    with batch_lock(batch), KeepAwake() as awake:
        protocol = prepare_batch(batch, HERE / 'runs' / args.reference_batch, rows, identity,
                                 allow_partial=True, prompt_profile=profile)
        policy = {'quota_threshold_percent': THRESHOLDS, 'max_opencode_calls': MAX_CALLS,
                  'account_billing_settings_changed': False,
                  'billing_hard_cap': False, 'automatic_recharge_authorized': False,
                  'retry_policy': 'quota interruption only; all quality failures preserved; up to 2 judge retries per item'}
        policy_path = batch / 'execution-policy.json'
        if policy_path.exists() and read(policy_path) != policy:
            raise ValueError('Execution policy differs from the frozen batch policy')
        atomic_json(policy_path, policy)
        judge_retry_path = batch / 'judge-retry-count.json'
        while not (batch / 'STOP').exists():
            awake.start()
            ready(batch)
            state = run_queue(batch, protocol, rows, templates, identity, workers=args.workers,
                              judge_workers=args.judge_workers, worker_fn=guarded_worker,
                              grade_fn=NormalizedGrader(batch), max_new_questions=args.max_new_questions)
            pause = read(batch / 'quota-pause.json') if (batch / 'quota-pause.json').exists() else None
            if state['status'] == 'completed':
                return
            if args.max_new_questions is not None or not args.watch or (batch / 'STOP').exists():
                return
            if state['judge_errors'] and (pause or state['status'] == 'needs_judge_retry'
                                         or state['stop_reason'] == 'repeated_judge_errors'):
                retries = read(judge_retry_path).get('questions', {}) if judge_retry_path.exists() else {}
                retry_ids = {str(qid) for qid in state['judge_errors'] if retries.get(str(qid), 0) < 2}
                if retry_ids:
                    for qid in retry_ids:
                        retries[qid] = retries.get(qid, 0) + 1
                    atomic_json(judge_retry_path, {'questions': retries, 'at': now()})
                    archive_judge_errors(batch / 'judge/results.json', retry_ids)
                    until = time.monotonic() + 120
                    while time.monotonic() < until:
                        if (batch / 'STOP').exists():
                            return
                        time.sleep(5)
                    continue
            if pause and pause.get('retry_at'):
                if pause['retry_at'] - time.time() > 6 * 3600:
                    awake.stop()
                # Persist the deadline; restarting this supervisor cannot shorten the wait.
                atomic_json(batch / 'status.json', {**state, 'status': 'waiting_for_quota',
                           'resume_at': datetime.fromtimestamp(pause['retry_at'], timezone.utc).isoformat(),
                           'quota': pause})
                print(json.dumps({'event': 'waiting_for_quota', **pause}), flush=True)
                while time.time() < pause['retry_at']:
                    if (batch / 'STOP').exists():
                        atomic_json(batch / 'status.json', {**state, 'status': 'paused', 'stop_reason': 'stop_file'})
                        return
                    time.sleep(min(30, pause['retry_at'] - time.time()))
                continue
            return


if __name__ == '__main__':
    main()
