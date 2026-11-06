"""模型服务商接口：列表、增删改、刷新模型目录。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from dra.api.deps import RegistryDep
from dra.api.services.runs import RunRegistry

router = APIRouter()


@router.get("/api/providers")
def api_providers() -> list[dict]:
    from dra import provider_registry
    return [p.model_dump() for p in provider_registry.list_status()]


@router.post("/api/providers/refresh")
def api_providers_refresh(*, registry: RegistryDep) -> list[dict]:
    _provider_edit_guard(registry)
    from concurrent.futures import ThreadPoolExecutor
    from dra import provider_registry as pr
    providers=[p.name for p in pr.list_status()]
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [p.model_dump() for p in pool.map(pr.refresh_provider,providers)]


def _provider_edit_guard(registry: "RunRegistry"):
    if registry.active_count():
        raise HTTPException(409,"研究正在运行，请结束后修改 API 连接，避免影响正在发送的请求。")


def _provider_input(model, body):
    try:return model.model_validate(body)
    except ValidationError as exc:
        # ValidationError.__str__ 会包含输入（可能是 API Key）；这里只返回字段与错误类型。
        details='；'.join('.'.join(map(str,e['loc']))+': '+e['msg'] for e in exc.errors(include_input=False))
        raise HTTPException(400,'连接参数不合法：'+details)


@router.post('/api/providers')
def api_provider_create(body: dict, *, registry: RegistryDep):
    from dra import provider_registry as pr
    _provider_edit_guard(registry)
    try:return pr.save_connection(_provider_input(pr.ConnectionInput,body)).model_dump()
    except ValueError as exc:raise HTTPException(400,str(exc))


@router.put('/api/providers/{provider}')
def api_provider_update(provider: str, body: dict, *, registry: RegistryDep):
    from dra import provider_registry as pr
    _provider_edit_guard(registry)
    try:return pr.save_connection(_provider_input(pr.ConnectionInput,body),provider).model_dump()
    except ValueError as exc:raise HTTPException(400,str(exc))


@router.delete('/api/providers/{provider}')
def api_provider_delete(provider: str, *, registry: RegistryDep):
    from dra import provider_registry as pr, runtime_config
    _provider_edit_guard(registry)
    used=[name for name in ('planner','subagent','vision') if getattr(runtime_config.current(),name).provider==provider]
    if used:raise HTTPException(409,'该连接仍被模型设置使用（'+', '.join(used)+'），请先更换并保存模型设置。')
    try:pr.delete_connection(provider)
    except ValueError as exc:raise HTTPException(400,str(exc))
    return {'ok':True}


@router.post('/api/providers/{provider}/refresh')
def api_provider_refresh(provider: str, *, registry: RegistryDep):
    from dra import provider_registry as pr
    _provider_edit_guard(registry)
    try:return pr.refresh_provider(provider).model_dump()
    except ValueError as exc:raise HTTPException(400,str(exc))


@router.put('/api/providers/{provider}/model-metadata')
def api_provider_model_metadata(provider: str, body: dict):
    from dra import provider_registry as pr
    try:return pr.save_override(provider,_provider_input(pr.ModelOverride,body)).model_dump()
    except ValueError as exc:raise HTTPException(400,str(exc))


@router.post('/api/model-catalog/refresh')
def api_model_catalog_refresh(body: dict):
    from dra import provider_registry as pr
    return pr.refresh_catalog(force=body.get('force') is True)
