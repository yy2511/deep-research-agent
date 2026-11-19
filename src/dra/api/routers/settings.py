"""设置接口：模型配置、MCP 配置、模型连通测试、执行环境状态。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ValidationError

from dra import llm, mcp_client
from dra.api.common import _StrictPayload, service_errors
from dra.api.deps import ADMIN_ONLY, CurrentUserDep, ReadyDatabaseDep

router = APIRouter()
# 改全站设置的接口：分组上统一要求管理员（#32）。
admin_router = APIRouter(dependencies=ADMIN_ONLY)


@router.get("/api/config")
def api_config() -> dict:
    """当前模型配置与自主研究的真实预算摘要。"""
    from dra import runtime_config

    settings = runtime_config.current()
    simple = runtime_config.to_simple_config(settings)
    from dra.evidence_judge import readiness
    from dra.context_budget import capacity
    return {
        **settings.model_dump(),
        "evidence_judge_status": readiness(),
        # 启动前确认投入范围用：数字全部来自服务端实际 SimpleConfig，前端不手写。
        "simple_budget": {
            "max_llm_calls": simple.max_llm_calls,
            "max_tool_calls": simple.max_tool_calls,
            "wall_timeout_s": simple.wall_timeout_s,
            "reserve_wrapup_calls": simple.reserve_wrapup_calls if simple.max_llm_calls is not None else 0,
            "storage_max_chars": simple.storage_max_chars,
        },
        "context_budgets": {phase: capacity(simple, phase) for phase in ("research", "compact", "condense", "wrapup")},
        "simple_model": {"model": simple.adapter.model, "provider": simple.adapter.provider,
                          "reasoning": simple.adapter.reasoning},
        # 只读：开关的实际取值与来源（不探测 Docker）；可用性见 /api/execution/status。
        "execution_effective": _execution_effective(settings, simple.execution),
        "retry_policy": runtime_config.research_retry_policy(settings),
    }


def _execution_effective(settings, config) -> dict:
    from dra.execution import ExecutionConfig
    return {"enabled": config.enabled, "network_mode": config.network_mode,
            "source": "environment" if settings.execution_enabled is None else "settings",
            "default_enabled": ExecutionConfig.from_env().enabled,
            "backend": config.backend, "default_backend": ExecutionConfig.from_env().backend,
            "backend_source": "environment" if settings.execution_backend is None else "settings"}


@router.get("/api/mcp/config")
def api_mcp_config() -> dict:
    try:
        return mcp_client.load_settings().model_dump(mode="json")
    except mcp_client.MCPError as exc:
        raise HTTPException(503, str(exc)) from None


@admin_router.put("/api/mcp/config")
def api_save_mcp_config(body: dict) -> dict:
    try:
        settings = mcp_client.MCPSettings.model_validate(body)
    except ValidationError:
        # pydantic 的默认错误包含输入值；配置错误不回显用户误粘贴的密钥。
        raise HTTPException(400, "MCP 配置不合法：检查服务 ID、HTTPS 地址、环境变量名称及重复工具；不能填写密钥值") from None
    try:
        mcp_client.save_settings(settings)
    except OSError:
        raise HTTPException(503, "MCP 配置保存失败，原配置未被替换") from None
    return settings.model_dump(mode="json")


@admin_router.post("/api/mcp/test")
def api_test_mcp(body: dict) -> dict:
    try:
        server = mcp_client.MCPServerConfig.model_validate(body)
    except ValidationError:
        raise HTTPException(400, "服务配置不合法；填写 HTTP MCP 地址和凭据环境变量名称") from None
    try:
        tools = mcp_client.discover(server)
    except mcp_client.MCPError as exc:
        raise HTTPException(502, str(exc)) from None
    return {"ok": True, "tools": [{"name": t["name"], "description": str(t.get("description") or "")[:1500],
                                   "inputSchema": t.get("inputSchema"), "annotations": t.get("annotations") or {}}
                                  for t in tools]}


@admin_router.put("/api/config")
def api_config_put(body: dict) -> dict:
    """body 收原始 dict 而非 runtime_config.RuntimeModelSettings 类型注解——runtime_config
    只在函数体内加载配置，module-level 没有这个
    名字可让 FastAPI 在路由注册期解析类型注解，故手动构造+校验，两处失败都转 400。"""
    from pydantic import ValidationError

    from dra import runtime_config

    try:
        if 'browser' not in body:
            body = {**body, 'browser': runtime_config.current().browser.model_dump()}
        elif isinstance(body['browser'], dict):
            # 旧网页只提交 enabled，保存开关不应把已选浏览器模式/配置重置。
            body = {**body, 'browser': {**runtime_config.current().browser.model_dump(), **body['browser']}}
        if 'vision' not in body:
            body = {**body, 'vision': runtime_config.current().vision.model_dump()}
        if 'trial' not in body:
            # 不认识试用设置的旧客户端保存别的设置时，不重置试用设置（#49）。
            body = {**body, 'trial': runtime_config.current().trial.model_dump()}
        if 'execution_backend' not in body:
            body = {**body, 'execution_backend': runtime_config.current().execution_backend}
        if 'execution_enabled' not in body:
            # 不认识此字段的旧客户端保存别的设置时，不改变当前执行权限。
            body = {**body, 'execution_enabled': runtime_config.current().execution_enabled}
        settings = runtime_config.RuntimeModelSettings(**body)
        from dra import provider_registry
        if settings.vision.provider and not provider_registry.exists(settings.vision.provider):
            raise ValueError('备用视觉连接不存在，请选择已保存的连接')
        if settings.trial.provider and not provider_registry.exists(settings.trial.provider):
            raise ValueError('试用模型的连接不存在，请选择已保存的连接')
        runtime_config.to_simple_config(settings)
    except (ValidationError, ValueError) as e:
        raise HTTPException(400, str(e))
    if settings.trial.timezone != runtime_config.current().trial.timezone:
        raise HTTPException(403, "额度时区只能由平台负责人在负责人管理中安排变更")
    runtime_config.save(settings)
    return api_config()


class TestConfigReq(BaseModel):
    model: str
    provider: str
    reasoning: bool = False
    effort: str | None = None


@admin_router.post("/api/config/test")
def api_config_test(req: TestConfigReq) -> dict:
    try:
        llm.chat(
            [{"role": "user", "content": "回复 OK"}],
            model=req.model, provider=req.provider,
            reasoning=req.reasoning, effort=req.effort,
            max_tokens=5, max_retries=0,
        )
        return {"ok": True}
    except Exception as e:
        from dra.provider_registry import safe_error
        return {"ok": False, "error": safe_error(e, operation='chat')}


@router.get("/api/execution/status")
def api_execution_status():
    """新研究将使用的执行配置及其可用性；与 ``POST /api/research`` 同一解析入口。"""
    from dra import runtime_config
    from dra.execution import availability
    settings = runtime_config.current()
    config = runtime_config.execution_config(settings)
    return {**availability(config), **_execution_effective(settings, config), "image": config.image if config.backend == "docker" else None}


class QuotaTimezoneReq(_StrictPayload):
    timezone: str
    expected_version: int


@admin_router.get("/api/admin/quota-timezone")
def api_quota_timezone(response: Response, user: CurrentUserDep, db: ReadyDatabaseDep) -> dict:
    from dra.api.services import quota_timezone
    response.headers["Cache-Control"] = "no-store"
    with service_errors():
        return quota_timezone.status(db, user.id)


@admin_router.put("/api/admin/quota-timezone")
def api_quota_timezone_put(req: QuotaTimezoneReq, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep) -> dict:
    from dra.api.services import quota_timezone
    response.headers["Cache-Control"] = "no-store"
    with service_errors():
        return quota_timezone.schedule(db, user.id, req.timezone, req.expected_version)
