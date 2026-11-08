"""技能接口：技能列表、启用、准备、保存，以及某次运行的技能记录。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from dra.api.common import _require_valid_run_id, service_errors
from dra.api.deps import ADMIN_ONLY, READABLE_RUN, StoreDep
from dra.api.services import skills as skill_service

router = APIRouter()
# 带研究编号的查看接口：属于自己或属于示例（#31、#47）。
read_router = APIRouter(dependencies=READABLE_RUN)
# 开关技能、改技能包：分组上统一要求管理员（#32）。准备技能（prepare）发起研究要用，所有登录用户可调。
admin_router = APIRouter(dependencies=ADMIN_ONLY)


class SkillWriteReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=40000)
    references: dict[str, str] = Field(default_factory=dict, max_length=24)


class SkillEnabledReq(BaseModel):
    enabled: list[str] = Field(max_length=32)


class SkillPrepareReq(BaseModel):
    selected: list[str] = Field(default_factory=list, max_length=32)


@router.get("/api/skills")
def api_skills():
    with service_errors():
        return skill_service.list_skills()


@admin_router.put("/api/skills/enabled")
def api_skills_enabled(req: SkillEnabledReq):
    with service_errors():
        skill_service.set_enabled(req.enabled)
    return api_skills()


@router.post("/api/skills/prepare")
def api_skills_prepare(req: SkillPrepareReq):
    with service_errors():
        return skill_service.prepare(req.selected)


@admin_router.put("/api/skills/packages/{name}")
def api_skills_write(name: str, req: SkillWriteReq):
    with service_errors():
        return skill_service.save(name, req.text, req.references)


@read_router.get("/api/runs/{run_id}/skills")
def api_run_skills(run_id: str, *, store: StoreDep):
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None:
        raise HTTPException(404, "run 不存在")
    try:
        bundle = store.read_skill_snapshot(run_id)
        state = store.read_ledger(run_id) or {}
        # catalog_recorded=False：开始时没有可用技能，或是早于技能记录的运行；不等于“目录为空”。
        return {"catalog": bundle if bundle is not None else {"skills": {}, "selected": []},
                "catalog_recorded": bundle is not None,
                "loaded": state.get("skills", {}).get("loaded", [])}
    except (OSError, ValueError):
        raise HTTPException(503, "本轮技能记录读取失败") from None
