"""One request budget for research, summaries and delivery; no model-name guesses."""
from __future__ import annotations

import json


def request_chars(messages, tools=()):
    from dra.vision import text_messages
    return sum(len(json.dumps(value, ensure_ascii=False)) for value in [*text_messages(messages), *tools])


def output_limit(config, phase="research"):
    override = (config.wrapup_max_output_tokens if phase == "wrapup" else
                config.summary_max_output_tokens if phase in ("compact", "condense") else None)
    return config.max_output_tokens if override is None else override


def capacity(config, phase="research"):
    caps = config.context_capabilities
    physical = caps.get("context_window")
    work = config.context_window_tokens if config.context_window_mode == "manual" else None
    windows = [n for n in (physical, work) if n is not None]
    window = min(windows) if windows else None
    output = output_limit(config, phase)
    maximum_output = caps.get("max_output_tokens")
    if maximum_output is not None and output is not None and output > maximum_output:
        raise ValueError(f"{phase} 输出额度 {output} 超过该连接已确认的输出上限 {maximum_output}")
    # Python callers may explicitly omit max_tokens. Preserve that contract and label
    # the old reserve as an assumption, rather than silently imposing an output cap.
    reserve = output if output is not None else config.pressure_reserve_tokens
    bounds = [n for n in (window - reserve if window is not None else None,
                         caps.get("max_input_tokens")) if n is not None]
    usable = min(bounds) if bounds else None
    if usable is not None and usable <= 0:
        raise ValueError(f"{phase} 窗口不足：窗口 {window}，输出预留 {reserve}；请显式调整配置")
    return {"phase": phase, "window_tokens": window, "physical_window_tokens": physical,
            "working_window_tokens": work, "input_limit_tokens": caps.get("max_input_tokens"),
            "output_tokens": output, "output_reserve_tokens": reserve,
            "output_limit_confirmed": output is not None, "input_budget_tokens": usable,
            "capacity_confirmed": physical is not None, "capabilities": caps}


def estimated_image_tokens(ctx, messages, *, phase="research"):
    from collections import Counter
    from dra.vision import image_token_parts
    parts = image_token_parts(messages)
    pixels = sum(p['tokens'] for p in parts)
    anchor = ctx.token_calibration.get(phase, {}).get("image_anchor")
    if anchor and anchor.get('parts'):
        known = Counter(p['key'] for p in anchor['parts'])
        current = Counter(p['key'] for p in parts)
        residual = anchor['input_tokens'] - anchor['text_estimate'] - anchor['text_margin']
        if residual > 0 and all(current[key] >= count for key, count in known.items()):
            new = 0
            for part in parts:
                if known[part['key']]:
                    known[part['key']] -= 1
                else:
                    new += part['tokens']
            # A fixed aggregate contribution belongs only to the images already sent.
            # Text error must never become a multiplier for a new large image.
            return residual + new, "aggregate_usage_fixed_images"
    return pixels, "pixel_estimated"


def assess(ctx, messages, tools=(), *, phase="research"):
    result = capacity(ctx.config, phase)
    chars = request_chars(messages, tools)
    estimate, basis = ctx.estimate_request_tokens(chars, phase=phase)
    calibration = ctx.token_calibration.get(phase)
    margin = chars * calibration["error_per_char"] if calibration else 0.0
    pixels, image_basis = estimated_image_tokens(ctx, messages, phase=phase)
    total = estimate + margin + pixels
    limit = result["input_budget_tokens"]
    return {**result, "request_chars": chars, "estimated_input_tokens": estimate + pixels, "estimated_image_tokens": pixels,
            "estimation_basis": basis, "image_estimation_basis": image_basis, "error_margin_tokens": margin,
            "calibration": "calibrated" if calibration else "uncalibrated",
            "checked_input_tokens": total, "fits": limit is None or total <= limit}


def is_context_error(exc):
    return any(term in str(exc).lower() for term in (
        "context_length_exceeded", "maximum context length", "context window exceeded", "prompt is too long"))
