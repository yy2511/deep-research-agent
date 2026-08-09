"""Web 入口的运行时模型配置。

三节点（planner/subagent/writer）的 model/provider/reasoning/effort 落盘到
``runtime_config.json``，由 Web 设置面板读写。它是 Web 入口的唯一可变来源；
CLI 仍在 ``__main__.py`` 显式选择实验档，Python API 则使用调用方传入配置或类默认值。
长期配置边界见 ``EXPERIMENT_PLAN.md`` 与 ``STATUS.md``。
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from dra.orchestrator import OrchestratorConfig
from dra.paths import RUNTIME_CONFIG_PATH
from dra.subagent import SubAgentConfig

_CONFIG_PATH = RUNTIME_CONFIG_PATH
_CONFIG_SCHEMA_VERSION = 2


class NodeModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str
    reasoning: bool = False
    effort: str | None = None


class PlannerModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str
    effort: str | None = None
    # 无 reasoning 字段：OrchestratorConfig 也没有 planner_reasoning——planner 档节点
    # （build_research_plan/write_report_plan/跨 Worker 审查）架构上恒定开思考，不是运行时开关。


class RuntimeModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = _CONFIG_SCHEMA_VERSION
    planner: PlannerModelConfig
    subagent: NodeModelConfig
    writer: NodeModelConfig
    # 与 OrchestratorConfig 对齐；一次性文件迁移后只接受新键。
    enable_cross_worker_audit: bool = False


def _migrate_file_payload(data: object) -> tuple[dict, bool]:
    """把本机 v1 配置一次性改写为 v2；运行时模型本身不接受旧键。"""
    if not isinstance(data, dict):
        raise ValueError("runtime_config.json 顶层必须是 object")
    migrated = dict(data)
    version = migrated.get("schema_version")
    if version not in (None, 1, _CONFIG_SCHEMA_VERSION):
        raise ValueError(f"不支持 runtime config schema_version={version!r}")
    changed = version != _CONFIG_SCHEMA_VERSION
    if "enable_global_audit" in migrated:
        if "enable_cross_worker_audit" in migrated:
            raise ValueError("runtime config 同时含新旧审查开关")
        migrated["enable_cross_worker_audit"] = migrated.pop("enable_global_audit")
        changed = True
    migrated["schema_version"] = _CONFIG_SCHEMA_VERSION
    return migrated, changed


def _code_defaults() -> RuntimeModelSettings:
    """出厂默认值 = OrchestratorConfig/SubAgentConfig 的 dataclass 默认（单一真相源）。"""
    base = OrchestratorConfig()
    return RuntimeModelSettings(
        planner=PlannerModelConfig(
            model=base.planner_model, provider=base.planner_provider,
            effort=base.planner_effort,
        ),
        subagent=NodeModelConfig(
            model=base.subagent.model, provider=base.subagent.provider,
            reasoning=base.subagent.reasoning, effort=base.subagent.effort,
        ),
        writer=NodeModelConfig(
            model=base.writer_model, provider=base.writer_provider,
            reasoning=base.writer_reasoning, effort=base.writer_effort,
        ),
        enable_cross_worker_audit=base.enable_cross_worker_audit,
    )


_cache: RuntimeModelSettings | None = None


def load() -> RuntimeModelSettings:
    """文件不存在/解析失败 → 回退代码默认值并落一份（fail-soft）。"""
    global _cache
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _CONFIG_PATH.exists():
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            data, migrated = _migrate_file_payload(raw)
            _cache = RuntimeModelSettings.model_validate(data)
            if migrated:
                _CONFIG_PATH.write_text(_cache.model_dump_json(indent=2), encoding="utf-8")
            return _cache
        except (json.JSONDecodeError, ValueError) as e:
            print(f"⚠️ runtime_config.json 解析失败（{e}），回退代码默认值")
    _cache = _code_defaults()
    _CONFIG_PATH.write_text(_cache.model_dump_json(indent=2), encoding="utf-8")
    return _cache


def save(settings: RuntimeModelSettings) -> None:
    """写文件 + 更新缓存。不做跨字段校验——调用方须先过一遍 to_orchestrator_config。"""
    global _cache
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    _cache = settings


def current() -> RuntimeModelSettings:
    if _cache is None:
        return load()
    return _cache


def to_orchestrator_config(
    settings: RuntimeModelSettings, **overrides
) -> OrchestratorConfig:
    """转成 OrchestratorConfig 实际吃的形状；构造期触发现成 model_validator
    （effort 需要 reasoning=True），非法组合在这里抛 pydantic.ValidationError。
    """
    subagent_kwargs = dict(
        model=settings.subagent.model, provider=settings.subagent.provider,
        reasoning=settings.subagent.reasoning, effort=settings.subagent.effort,
    )
    base = dict(
        planner_model=settings.planner.model,
        planner_provider=settings.planner.provider,
        planner_effort=settings.planner.effort,
        writer_model=settings.writer.model,
        writer_provider=settings.writer.provider,
        writer_reasoning=settings.writer.reasoning,
        writer_effort=settings.writer.effort,
        enable_cross_worker_audit=settings.enable_cross_worker_audit,
        subagent=SubAgentConfig(**subagent_kwargs),
    )
    base.update(overrides)
    return OrchestratorConfig(**base)
