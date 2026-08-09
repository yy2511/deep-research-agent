"""Versioned research checkpoints with explicit compatibility checks.

注：本文件使用英文注释，与项目其他文件的中文风格不同。这是历史遗留，不影响功能。

The checkpoint filename is only a locator.  Resuming is permitted only when the
query, confirmed plan and semantic runtime configuration still match the
versioned envelope stored in that file.  This prevents an explicit ``run_id``
or a stale file from silently resuming a different research run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from dra.models import ResearchPlan, ResearchState

CHECKPOINT_SCHEMA_VERSION = 11

_ENVELOPE_KEYS = {
    "schema_version",
    "query_hash",
    "plan_hash",
    "config_hash",
    "state_hash",
    "state",
}


class CheckpointCompatibilityError(RuntimeError):
    """The located checkpoint exists, but is unsafe to resume."""


def _normalise_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFC", query).split())


def _resolve_run_id(query: str, run_id: str | None) -> str:
    """Use the historical MD5 filename shape over the canonical query identity."""
    if run_id:
        return run_id
    canonical_query = _normalise_query(query)
    return hashlib.md5(canonical_query.encode("utf-8")).hexdigest()[:12]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _query_hash(query: str) -> str:
    return _sha256(_normalise_query(query))


def _plan_projection(research_plan: ResearchPlan) -> dict[str, Any]:
    """Hash the confirmed semantic plan, excluding scheduler-owned runtime data.

    Scheduler-owned fields are excluded.  The confirmed DAG and its initial
    tasks are the whole plan contract.
    """
    return {
        "clarified_query": _normalise_query(research_plan.clarified_query),
        "plan_nodes": [m.model_dump(mode="json") for m in research_plan.plan_nodes],
        "initial_tasks": [
            {
                "id": sq.id,
                "node_id": sq.node_id,
                "objective": sq.objective,
                "search_query": sq.search_query,
            }
            for sq in research_plan.initial_tasks
        ],
    }


def _plan_hash(research_plan: ResearchPlan) -> str:
    return _sha256(_plan_projection(research_plan))


def _query_matches_plan(query: str, research_plan: ResearchPlan) -> bool:
    return _normalise_query(query) == _normalise_query(research_plan.clarified_query)


def _config_projection(config: Any) -> dict[str, Any]:
    """Return only runtime semantics; presentation-only streaming may drift."""
    if isinstance(config, BaseModel):
        data = config.model_dump(mode="json")
    elif isinstance(config, Mapping):
        data = dict(config)
    else:
        raise TypeError("config 必须是 Pydantic model 或 mapping")
    data.pop("stream_report", None)
    return data


def _config_hash(config: Any) -> str:
    return _sha256(_config_projection(config))


def _same_digest(actual: Any, expected: str) -> bool:
    return isinstance(actual, str) and hmac.compare_digest(actual, expected)


def save_checkpoint(
    state: ResearchState,
    checkpoint_dir: str | Path,
    *,
    config: Any,
    run_id: str | None = None,
) -> Path:
    """Atomically save a typed state inside a versioned compatibility envelope."""
    research_plan = state.research_plan
    if research_plan is None:
        raise CheckpointCompatibilityError("checkpoint state 缺少 confirmed research_plan")
    if not _query_matches_plan(state.query, research_plan):
        raise CheckpointCompatibilityError("checkpoint query 与 research_plan identity 不一致")

    state_payload = state.model_dump(mode="json")
    envelope = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "query_hash": _query_hash(state.query),
        "plan_hash": _plan_hash(research_plan),
        "config_hash": _config_hash(config),
        "state_hash": _sha256(state_payload),
        "state": state_payload,
    }

    directory = Path(checkpoint_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"checkpoint-{_resolve_run_id(state.query, run_id)}.json"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(envelope, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return path


def load_checkpoint(
    checkpoint_dir: str | Path,
    query: str,
    *,
    config: Any,
    expected_plan: ResearchPlan | None = None,
    run_id: str | None = None,
) -> ResearchState | None:
    """Load a checkpoint only when every resume identity check succeeds.

    A missing file means a fresh run and returns ``None``.  Any file that is
    present but malformed, legacy or drifted raises a stable compatibility
    error; callers must not silently fall back to a fresh run in that case.
    """
    expected_config_hash = _config_hash(config)
    rid = _resolve_run_id(query, run_id)
    path = Path(checkpoint_dir) / f"checkpoint-{rid}.json"
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckpointCompatibilityError(
            f"checkpoint JSON 无法读取或解析：{path}"
        ) from exc
    if not isinstance(raw, dict):
        raise CheckpointCompatibilityError("checkpoint schema 不是 JSON object")
    if "schema_version" not in raw:
        raise CheckpointCompatibilityError(
            "发现旧版 legacy 裸 checkpoint；无法证明计划与配置兼容，请重新运行"
        )
    if raw["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointCompatibilityError(
            "checkpoint schema_version 不兼容："
            f"{raw['schema_version']!r} != {CHECKPOINT_SCHEMA_VERSION}"
        )
    if set(raw) != _ENVELOPE_KEYS:
        missing = sorted(_ENVELOPE_KEYS - set(raw))
        extra = sorted(set(raw) - _ENVELOPE_KEYS)
        raise CheckpointCompatibilityError(
            f"checkpoint schema 字段不匹配（missing={missing}, extra={extra}）"
        )
    if not _same_digest(raw["query_hash"], _query_hash(query)):
        raise CheckpointCompatibilityError("checkpoint query identity drift")
    if not _same_digest(raw["config_hash"], expected_config_hash):
        raise CheckpointCompatibilityError("checkpoint config drift")
    if not _same_digest(raw["state_hash"], _sha256(raw["state"])):
        raise CheckpointCompatibilityError(
            "checkpoint state integrity drift（state/research_plan 可能已被篡改）"
        )

    try:
        state = ResearchState.model_validate(raw["state"])
    except (ValidationError, TypeError) as exc:
        raise CheckpointCompatibilityError("checkpoint state schema 校验失败") from exc
    if not _same_digest(raw["query_hash"], _query_hash(state.query)):
        raise CheckpointCompatibilityError("checkpoint internal query drift")

    research_plan = state.research_plan
    if research_plan is None:
        raise CheckpointCompatibilityError("checkpoint state 缺少 research_plan")
    if not _query_matches_plan(state.query, research_plan):
        raise CheckpointCompatibilityError(
            "checkpoint internal query 与 research_plan identity 不一致"
        )
    if not _same_digest(raw["plan_hash"], _plan_hash(research_plan)):
        raise CheckpointCompatibilityError("checkpoint internal research_plan drift")
    if expected_plan is not None and not _same_digest(
        raw["plan_hash"], _plan_hash(expected_plan)
    ):
        raise CheckpointCompatibilityError("checkpoint expected research_plan drift")
    return state
