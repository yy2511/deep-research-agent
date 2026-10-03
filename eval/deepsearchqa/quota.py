"""Evaluation-only OpenCode quota guard. No account or billing settings are changed."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import threading
import time
import uuid

import httpx
from dra import llm
from dra.durable import atomic_json
from dra.provider_registry import credentials

# Leave headroom for already-running requests and usage reporting latency.
# These are a conservative client guard, not an atomic provider spending cap.
THRESHOLDS = {'rolling': 80, 'weekly': 90, 'monthly': 90}
MAX_CALLS = 4


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def fetch_usage():
    key, base = credentials('opencode')
    if base != 'https://opencode.ai/zen/go/v1':
        raise ValueError('Quota guard requires the known OpenCode Go endpoint')
    response = httpx.get(base + '/usage', headers={'Authorization': 'Bearer ' + key}, timeout=15)
    response.raise_for_status()
    data = response.json()['usage']
    for name in THRESHOLDS:
        if not isinstance(data[name]['percent'], (int, float)) or data[name]['status'] not in ('ok', 'rate-limited'):
            raise ValueError('Invalid quota response')
        epoch(data[name]['resetsAt'])
    return data


def credit_allowed(batch):
    path = Path(batch) / 'budget-authorization.json'
    if not path.exists() or (Path(batch) / 'credit-exhausted.json').exists():
        return False
    policy = json.loads(path.read_text())
    return (policy.get('allow_existing_credit_usd') == 5
            and policy.get('auto_recharge_verified_disabled') is True
            and policy.get('allow_recharge') is False)


def pause_for_usage(usage, now=None, *, use_credit=False):
    now = time.time() if now is None else now
    if use_credit:
        # Explicitly authorized existing wallet balance, auto-recharge verified disabled.
        # OpenCode may use the balance after Go limits; an actual 402 disables this override.
        return None
    blocked = [name for name, limit in THRESHOLDS.items()
               if usage[name]['status'] == 'rate-limited' or usage[name]['percent'] >= limit]
    if not blocked:
        return None
    return {'reason': 'subscription_headroom', 'windows': blocked,
            'retry_at': max(max(epoch(usage[name]['resetsAt']) for name in blocked) + 60, now + 60),
            'usage': usage, 'at': timestamp()}


class QuotaPause(RuntimeError):
    pass


class Guard:
    def __init__(self, batch, cancelled, fetch=fetch_usage):
        self.batch, self.cancelled, self.fetch = Path(batch), cancelled, fetch
        self.triggered = None
        self.local_lock = threading.Lock()

    @contextmanager
    def locked(self):
        # flock also protects threads via separate file descriptions; the local lock keeps it explicit.
        with self.local_lock, (self.batch / 'quota.lock').open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read_state(self):
        path = self.batch / 'quota-state.json'
        return json.loads(path.read_text()) if path.exists() else {'active': {}, 'checked_at': 0}

    def stop(self, reason):
        self.triggered = reason
        self.cancelled.set()
        raise QuotaPause(reason['reason'])

    def acquire(self):
        while True:
            with self.locked():
                if (self.batch / 'STOP').exists():
                    self.stop({'reason': 'stop_file', 'at': timestamp()})
                pause = self.batch / 'quota-pause.json'
                if pause.exists():
                    self.stop(json.loads(pause.read_text()))
                state = self.read_state()
                state['active'] = {k: v for k, v in state['active'].items() if alive(v['pid'])}
                if time.time() - state['checked_at'] >= 5:
                    try:
                        usage = self.fetch()
                        reason = pause_for_usage(usage, use_credit=credit_allowed(self.batch))
                        state.update(usage=usage, checked_at=time.time())
                    except Exception as exc:
                        # A failed check must not silently permit potentially billable inference.
                        reason = {'reason': 'quota_check_unavailable', 'error': type(exc).__name__,
                                  'retry_at': time.time() + 900, 'at': timestamp()}
                    atomic_json(self.batch / 'quota-state.json', state)
                    if reason:
                        atomic_json(pause, reason)
                        self.stop(reason)
                if len(state['active']) < MAX_CALLS:
                    token = uuid.uuid4().hex
                    state['active'][token] = {'pid': os.getpid(), 'at': timestamp()}
                    atomic_json(self.batch / 'quota-state.json', state)
                    return token
            if self.cancelled.wait(.1) or llm.cancellation_requested():
                raise QuotaPause('cancelled')

    def release(self, token):
        with self.locked():
            state = self.read_state()
            state['active'].pop(token, None)
            atomic_json(self.batch / 'quota-state.json', state)

    def wrap(self, call):
        def guarded(*args, **kwargs):
            if kwargs.get('provider') != 'opencode':
                return call(*args, **kwargs)
            token = self.acquire()
            try:
                return call(*args, **{**kwargs, 'max_retries': 0})
            except Exception as exc:
                current = exc
                while current is not None:
                    if getattr(current, 'status_code', None) in (401, 402, 403, 429):
                        code = current.status_code
                        response = getattr(current, 'response', None)
                        body = getattr(current, 'body', None)
                        if not isinstance(body, dict) and response is not None:
                            try:
                                body = response.json()
                            except ValueError:
                                body = None
                        error_node = body.get('error', body) if isinstance(body, dict) else {}
                        error_type = error_node.get('type') if isinstance(error_node, dict) else None
                        retry = response.headers.get('retry-after') if response is not None else None
                        seconds = float(retry) if retry and retry.replace('.', '', 1).isdigit() else 5 * 3600
                        reason = {'reason': 'provider_limit' if code == 429 else 'provider_account_error',
                                  'http_status': code, 'at': timestamp(),
                                  'retry_at': time.time() + max(60, seconds + 60) if code == 429 else None}
                        with self.locked():
                            # OpenCode's public handler returns CreditsError as HTTP 401;
                            # distinguish it from real authentication errors, and also handle 402.
                            if (code == 402 or (code == 401 and error_type == 'CreditsError')) and credit_allowed(self.batch):
                                atomic_json(self.batch / 'credit-exhausted.json', {
                                    'at': timestamp(), 'http_status': code, 'provider_error_type': error_type})
                                try:
                                    usage = self.fetch()
                                    exhausted = [name for name in THRESHOLDS
                                                 if usage[name]['status'] == 'rate-limited' or usage[name]['percent'] >= 100]
                                    if exhausted:
                                        reason.update(reason='subscription_and_credit_exhausted', usage=usage,
                                            retry_at=max(epoch(usage[name]['resetsAt']) for name in exhausted) + 60)
                                except Exception:
                                    pass  # Unknown account state remains a manual stop.
                            atomic_json(self.batch / 'quota-pause.json', reason)
                        self.stop(reason)
                    current = current.__cause__
                raise
            finally:
                self.release(token)
        return guarded


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def guarded_worker(question, out, config_data):
    from dra.simple_research import SimpleConfig
    from run_pilot import run_one
    cancelled = threading.Event()
    guard = Guard(Path(out).parent, cancelled)
    old_tools, old_chat = llm.call_tools, llm.chat
    llm.call_tools, llm.chat = guard.wrap(old_tools), guard.wrap(old_chat)
    try:
        with llm.execution_scope(cancel_event=cancelled):
            record = run_one(question, out, SimpleConfig.model_validate(config_data))
        if guard.triggered:
            record.update(status='infrastructure_interrupted', interruption=guard.triggered)
            atomic_json(Path(out) / 'result.json', record)
        return record
    finally:
        llm.call_tools, llm.chat = old_tools, old_chat
