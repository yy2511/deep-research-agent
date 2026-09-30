"""Web 入口的运行时模型配置。

澄清规划与研究写作两组配置的 model/provider/reasoning/effort 落盘到
``runtime_config.json``，由 Web 设置面板读写。它是 Web 入口的唯一可变来源；
自动化脚本共享这份配置；Python API 使用调用方传入配置或类默认值。
长期配置边界见 ``EXPERIMENT_PLAN.md`` 与 ``STATUS.md``。
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dra.paths import RUNTIME_CONFIG_PATH
from dra.subagent import SubAgentConfig
from dra.evidence_judge import JudgeConfig

_CONFIG_PATH = RUNTIME_CONFIG_PATH
_CONFIG_SCHEMA_VERSION = 4


class BriefConfig(BaseModel):
    """开跑前澄清/路线所需的配置，不参与研究循环调度。"""
    model_config = ConfigDict(extra="forbid")
    planner_model: str = "glm-5.2"
    planner_provider: str = "opencode"
    planner_effort: str | None = None
    max_initial_tasks: int = 8


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
    # 澄清规划恒定开思考。


class RuntimeModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[4] = _CONFIG_SCHEMA_VERSION
    planner: PlannerModelConfig
    subagent: NodeModelConfig
    call_limits_enabled: bool = False
    evidence_judge: JudgeConfig = Field(default_factory=JudgeConfig)
    compaction_enabled: bool = False
    context_window_tokens: int | None = Field(default=None, gt=12000)

    @model_validator(mode="after")
    def validate_compaction(self):
        if self.compaction_enabled and self.context_window_tokens is None:
            raise ValueError("启用会话整理必须填写模型上下文容量（token）")
        return self


def _code_defaults() -> RuntimeModelSettings:
    """代码默认值与现役配置类保持一致。"""
    planner, research = BriefConfig(), SubAgentConfig()
    return RuntimeModelSettings(
        planner=PlannerModelConfig(model=planner.planner_model,
                                   provider=planner.planner_provider, effort=planner.planner_effort),
        subagent=NodeModelConfig(model=research.model, provider=research.provider,
                                reasoning=research.reasoning, effort=research.effort),
    )


_cache: RuntimeModelSettings | None = None


def load() -> RuntimeModelSettings:
    """只为缺失文件生成默认值；已有文件仅接受当前版本，校验失败保留原文件。"""
    global _cache
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _CONFIG_PATH.exists():
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "schema_version" not in raw:
                raise ValueError("缺少 schema_version，请先转换为 v4 配置")
            _cache = RuntimeModelSettings.model_validate(raw)
            return _cache
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"runtime_config.json 校验失败，原配置已保留：{e}") from e
    _cache = _code_defaults()
    save(_cache)
    return _cache


def save(settings: RuntimeModelSettings) -> None:
    """写文件 + 更新缓存。调用方已校验现役模型参数。"""
    global _cache
    from dra.durable import atomic_json
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(_CONFIG_PATH, settings.model_dump(mode="json"))
    _cache = settings


def current() -> RuntimeModelSettings:
    if _cache is None:
        return load()
    return _cache


def to_brief_config(settings: RuntimeModelSettings, **overrides) -> BriefConfig:
    base = dict(planner_model=settings.planner.model, planner_provider=settings.planner.provider,
                planner_effort=settings.planner.effort)
    base.update(overrides)
    return BriefConfig(**base)


def to_subagent_config(settings: RuntimeModelSettings) -> SubAgentConfig:
    """主、子 Agent 共用的模型与检索配置。"""
    return SubAgentConfig(
        model=settings.subagent.model,
        provider=settings.subagent.provider,
        reasoning=settings.subagent.reasoning,
        effort=settings.subagent.effort,
    )


def to_simple_config(settings: RuntimeModelSettings, **overrides):
    """现役研究的统一配置入口；次数开关不影响模型选择和整轮时限。"""
    from dra.simple_research import SimpleConfig
    from dra.execution import ExecutionConfig
    base = dict(adapter=to_subagent_config(settings),
                execution=ExecutionConfig.from_env(),
                evidence_judge=settings.evidence_judge,
                compaction_enabled=settings.compaction_enabled,
                context_window_tokens=settings.context_window_tokens,
                max_llm_calls=20 if settings.call_limits_enabled else None,
                max_tool_calls=20 if settings.call_limits_enabled else None,
                # 显式给输出上限：不带 max_tokens 时，部分上游会按自己的默认值在 8192 截断，
                # 长交稿因此被截成半截；显式值会被照办。32768 足够一两万字的报告。
                max_output_tokens=32768)
    base.update(overrides)
    return SimpleConfig(**base)
