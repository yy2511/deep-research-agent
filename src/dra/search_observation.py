"""搜索服务诊断旁路；不把运行诊断加入模型的搜索回执。"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager


_current = contextvars.ContextVar("dra_search_observation", default=None)
_log = logging.getLogger(__name__)


def error_details(exc: Exception) -> dict:
    """只保存状态码、异常类型和固定分类，绝不持久化含凭据的上游错误原文。"""
    chain = []
    cause = exc
    while cause is not None and len(chain) < 8 and all(cause is not e for e in chain):
        chain.append(cause)
        cause = cause.__cause__ or cause.__context__
    status = None
    for item in chain:
        response = getattr(item, "response", None)
        code = getattr(response, "status_code", None)
        if not isinstance(code, int):
            code = getattr(item, "status_code", None)
        if isinstance(code, int):
            status = code
            break
    kinds = " ".join(type(item).__name__ for item in chain)
    category = {400: "bad_request", 401: "authentication", 402: "payment_required",
                403: "permission_denied", 429: "rate_limited"}.get(status)
    if category is None:
        if status is not None:
            category = "server_error" if status >= 500 else "http_error"
        elif any("缺少" in str(item) and "KEY" in str(item) for item in chain):
            category = "missing_credentials"
        elif "UsageLimit" in kinds:
            category = "quota_exceeded"
        elif "InvalidAPIKey" in kinds or "MissingAPIKey" in kinds:
            category = "authentication"
        elif "Timeout" in kinds:
            category = "timeout"
        elif "Connection" in kinds or "SSLError" in kinds:
            category = "connection_error"
        elif "JSONDecode" in kinds:
            category = "invalid_response"
        else:
            category = "provider_error"
    return {"error_type": type(exc).__name__, "cause_types": [type(e).__name__ for e in chain],
            "error_category": category, "http_status": status}


class SearchCall:
    def __init__(self, observer, provider: str, query: str, top_k: int):
        self.observer = observer
        self.fields = dict(search_call_id=uuid.uuid4().hex, provider=provider, query=query, top_k=top_k)
        self.started = time.monotonic()
        self.attempts = 0
        self.last_http_status = None
        self.response_received = False

    def emit(self, phase: str, **fields):
        if self.observer is None:
            return
        try:
            self.observer({**self.fields, "at": time.time(), "phase": phase, **fields})
        except Exception as exc:
            # 诊断旁路不能把已经成功的检索变成失败；保存失败必须在服务日志可见。
            _log.error("search_calls.jsonl 写入失败（%s）", type(exc).__name__)

    @contextmanager
    def scope(self):
        token = _current.set(self)
        self.emit("started")
        try:
            yield self
        finally:
            _current.reset(token)

    def finish(self, *, result_count=0, error=None):
        detail = error_details(error) if error is not None else {}
        self.emit("done", status="failed" if error is not None else "ok",
                  elapsed_ms=round((time.monotonic() - self.started) * 1000, 3),
                  result_count=result_count, attempts=self.attempts,
                  response_received=self.response_received,
                  **{**detail, "http_status": detail.get("http_status") or self.last_http_status})


class SearchAttempt:
    def __init__(self):
        self.http_status = None
        self.response_received = False

    def received(self, http_status=None):
        self.response_received = True
        self.http_status = http_status


@contextmanager
def request_attempt(number: int):
    """包住一次实际 HTTP/SDK 尝试；缺配置时不会生成 request_started。"""
    call = _current.get()
    attempt = SearchAttempt()
    started = time.monotonic()
    if call is not None:
        call.attempts += 1
        call.emit("request_started", attempt=number)
    error = None
    try:
        yield attempt
    except Exception as exc:
        error = exc
        raise
    finally:
        if call is not None:
            detail = error_details(error) if error is not None else {}
            status = detail.get("http_status") or attempt.http_status
            received = attempt.response_received or status is not None
            call.last_http_status = status
            call.response_received |= received
            call.emit("request_done", attempt=number, status="failed" if error else "ok",
                      elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                      response_received=received, **{**detail, "http_status": status})


def journal_observer(store, *, agent_id: str, tool_call_id: str, model_call_id: str):
    """捕获工具身份；主/子 Agent 的并发追加共用 RunStore 的锁。"""
    identity = dict(schema_version=1, run_id=store.run_id, agent_id=agent_id,
                    tool_call_id=tool_call_id, model_call_id=model_call_id)

    def record(row):
        with store._lock:
            with (store.dir / "search_calls.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**identity, **row}, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    return record
