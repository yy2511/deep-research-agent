"""Per-HTTP-attempt telemetry, independent of prompt construction and text traces.

Raw provider usage is the evidence. Local timing/prefix fingerprints are diagnostics;
missing or conflicting cache fields never become a known zero.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from urllib.parse import urlsplit
from uuid import uuid4

from dra import events, timing


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def normalize_usage(raw):
    """Chat Completions prompt_tokens includes cached input; aliases are not additive."""
    # A malformed optional telemetry field must not invalidate the model's answer.
    # The original value remains available in raw_usage for diagnosis.
    raw = raw if isinstance(raw, dict) else {}
    incoming, outgoing = raw.get("prompt_tokens"), raw.get("completion_tokens")
    result = {"input_tokens": incoming if _count(incoming) else None,
              "output_tokens": outgoing if _count(outgoing) else None,
              "cache_read_tokens": None, "cache_miss_tokens": None,
              "cache_status": "unknown", "cache_source": None}
    details = raw.get("prompt_tokens_details") or {}
    aliases = [("prompt_cache_hit_tokens", raw.get("prompt_cache_hit_tokens")),
               ("prompt_tokens_details.cached_tokens", details.get("cached_tokens") if isinstance(details, dict) else None),
               ("cached_tokens", raw.get("cached_tokens"))]
    present = [(name, value) for name, value in aliases if value is not None]
    if not present:
        return result
    source, hit = present[0]
    result["cache_source"] = source
    miss = raw.get("prompt_cache_miss_tokens")
    if (any(not _count(value) or value != hit for _, value in present)
            or (_count(incoming) and hit > incoming)
            or (miss is not None and (not _count(miss) or (_count(incoming) and hit + miss != incoming)))):
        result["cache_status"] = "inconsistent"
        return result
    if not _count(incoming):
        return result
    result.update(cache_read_tokens=hit,
                  cache_miss_tokens=miss if miss is not None else incoming - hit,
                  cache_status="reported" if miss is not None else "derived_miss")
    return result


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class _Observation:
    def __init__(self, client, kwargs, provider, model, attempt, abort_reason, full_io=False):
        self.start = time.monotonic()
        self.finished = False
        self.abort_reason = abort_reason
        self.streamed = bool(kwargs.get("stream"))
        self.data = {"request_id": uuid4().hex, "provider": provider, "model": model,
                     "attempt": attempt, "stream": self.streamed,
                     "raw_usage": None, "provider_cost": None,
                     "balance_charge_usd": None, "cost_status": "unknown",
                     "first_token_ms": None, "first_content_ms": None,
                     "finish_reason": None,
                     **{k: timing.get_ctx().get(k) for k in (
                         "usage_role", "step", "sid", "delegation_id", "parent_call_id", "call_id", "turn", "parent_model_call_id")}}
        # 白名单只记录实际发出的参数；不保存认证头、任意 extra_body 或 URL 凭据。
        self.data["request_parameters"] = {k: _plain(kwargs[k]) for k in (
            "model", "temperature", "max_tokens", "max_completion_tokens", "top_p", "stream",
            "stream_options", "reasoning_effort", "frequency_penalty", "presence_penalty", "seed",
        ) if k in kwargs}
        extra = kwargs.get("extra_body") or {}
        allowed_extra = {k: _plain(extra[k]) for k in ("thinking", "reasoning_effort") if k in extra}
        if allowed_extra:
            self.data["request_parameters"]["extra_body"] = allowed_extra
        self.data["io_complete"] = full_io
        if full_io:
            self.data["request_messages"] = _plain(kwargs.get("messages", []))
            self.data["request_tools"] = _plain(kwargs.get("tools", []))
        url = urlsplit(str(getattr(client, "base_url", "")))
        self.data["route"] = f"{url.scheme}://{url.hostname or ''}{':' + str(url.port) if url.port else ''}{url.path}"
        self.data["tools_fingerprint"] = _fingerprint(kwargs.get("tools", []))
        self.data["request_fingerprint"] = _fingerprint({
            k: v for k, v in kwargs.items() if k not in {"timeout", "extra_headers"}})
        session = (kwargs.get("extra_headers") or {}).get("x-opencode-session")
        self.data["session_fingerprint"] = _fingerprint(session) if session else None

    def note(self, response):
        usage = _get(response, "usage")
        if usage is not None:
            self.data["raw_usage"] = _plain(usage)
        cost = _get(response, "cost")
        if cost is not None:
            self.data["provider_cost"] = _plain(cost)
            # OpenCode returns balance charge in USD, not subscription quota consumed.
            if self.data["provider"] == "opencode":
                try:
                    amount = float(cost)
                    if math.isfinite(amount) and amount >= 0:
                        self.data.update(balance_charge_usd=amount, cost_status="opencode_balance_charge")
                except (ValueError, TypeError):
                    pass
        if not self.streamed:
            return
        for choice in _get(response, "choices", []) or []:
            if reason := _get(choice, "finish_reason"):
                self.data["finish_reason"] = reason
            delta = _get(choice, "delta")
            content = _get(delta, "content")
            meaningful = bool(content or _get(delta, "reasoning_content") or _get(delta, "reasoning"))
            for tool in _get(delta, "tool_calls", []) or []:
                fn = _get(tool, "function")
                meaningful |= bool(_get(fn, "name") or _get(fn, "arguments"))
            elapsed = round((time.monotonic() - self.start) * 1000, 1)
            if meaningful and self.data["first_token_ms"] is None:
                self.data["first_token_ms"] = elapsed
            if content and self.data["first_content_ms"] is None:
                self.data["first_content_ms"] = elapsed

    def finish(self, outcome, error=None):
        if self.finished:
            return
        self.finished = True
        self.data.update(normalize_usage(self.data["raw_usage"]))
        self.data.update(outcome=outcome, error_type=type(error).__name__ if error else None,
                         duration_ms=round((time.monotonic() - self.start) * 1000, 1))
        events.emit(events.EventType.LLM_USAGE, **self.data)


class _ObservedStream:
    def __init__(self, stream, observation):
        self.stream, self.observation = stream, observation
        self.iterator = iter(stream)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self.iterator)
        except StopIteration:
            self.observation.finish("returned")
            raise
        except Exception as exc:
            self.observation.finish("error", exc)
            raise
        self.observation.note(chunk)
        return chunk

    def close(self):
        try:
            close = getattr(self.stream, "close", None)
            if close:
                close()
        finally:
            self.observation.finish(self.observation.abort_reason() or "interrupted")


def observed_create(client, kwargs, *, provider, model, attempt, abort_reason, full_io=False):
    """Wrap one SDK request without changing any outgoing parameter or response."""
    observation = _Observation(client, kwargs, provider, model, attempt, abort_reason, full_io)
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        observation.finish("error", exc)
        raise
    if kwargs.get("stream"):
        return _ObservedStream(response, observation)
    observation.note(response)
    observation.finish("returned")
    return response


def aggregate_usage(rows):
    """Coverage and hit rate have separate denominators; old logs remain unknown."""
    records = [e for e in rows if e.get("type") == "llm_usage"]
    known = [e for e in records if e.get("cache_status") in {"reported", "derived_miss"}]
    input_known = [e for e in records if _count(e.get("input_tokens"))]
    output_known = [e for e in records if _count(e.get("output_tokens"))]
    cost_known = [e for e in records if e.get("balance_charge_usd") is not None]
    hit = sum(e["cache_read_tokens"] for e in known) if known else None
    denominator = sum(e["input_tokens"] for e in known) if known else None
    first = [e["first_token_ms"] for e in records if e.get("first_token_ms") is not None]
    return {
        "requests": len(records), "failed_requests": sum(e.get("outcome") == "error" for e in records),
        "interrupted_requests": sum(e.get("outcome") not in {"returned", "error"} for e in records),
        "input_tokens": sum(e["input_tokens"] for e in input_known) if input_known else None,
        "output_tokens": sum(e["output_tokens"] for e in output_known) if output_known else None,
        "missing_usage_requests": len(records) - len(input_known),
        "cache_observed_requests": len(known), "cache_unknown_requests": len(records) - len(known),
        "cache_inconsistent_requests": sum(e.get("cache_status") == "inconsistent" for e in records),
        "cache_coverage": len(known) / len(records) if records else None,
        "cache_measured_input_tokens": denominator, "cache_read_tokens": hit,
        "cache_miss_tokens": sum(e["cache_miss_tokens"] for e in known) if known else None,
        "cache_hit_rate": hit / denominator if denominator else None,
        "first_token_observed_requests": len(first),
        "first_token_p50_ms": statistics.median(first) if first else None,
        "first_token_p95_ms": sorted(first)[math.ceil(len(first) * .95) - 1] if first else None,
        "cost_observed_requests": len(cost_known),
        "cost_unknown_requests": len(records) - len(cost_known),
        "balance_charge_usd": sum(e["balance_charge_usd"] for e in cost_known) if cost_known else None,
        "subscription_usage_usd": None,
    }
