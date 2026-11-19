"""管理员账号管理接口；响应不缓存，不包含私有研究内容。"""
from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import Field, StrictInt, field_validator

from dra.api.common import _StrictPayload, service_errors
from dra.api.deps import ADMIN_ONLY, SESSION_COOKIE, CurrentUserDep, ReadyDatabaseDep
from dra.api.services import github_auth, user_management as service
from dra.api.services.errors import ServiceError

router = APIRouter(dependencies=ADMIN_ONLY)
PROOF_COOKIE = 'dra_owner_transfer'
STATE_COOKIE = 'dra_owner_transfer_state'
PROOF_PATH = '/api/admin/owner-transfer'
STATE_PATH = '/api/auth/github'


def no_store(response):
    response.headers['Cache-Control'] = 'no-store'


class PolicyReq(_StrictPayload):
    @field_validator('daily_runs_limit', 'daily_briefs_limit', mode='before')
    @classmethod
    def decimal_integer(cls, value):
        # Decimal strings let browsers submit the full signed BIGINT range exactly.
        if isinstance(value, str):
            if not value or not value.isascii() or not value.isdecimal():
                raise ValueError('每日次数须为非负十进制整数')
            return int(value)
        return value

    mode: Literal['limited', 'unlimited']
    daily_runs_limit: StrictInt | None = Field(default=None, ge=0, le=service.MAX_LIMIT)
    daily_briefs_limit: StrictInt | None = Field(default=None, ge=0, le=service.MAX_LIMIT)
    expected_version: StrictInt | None = Field(default=None, ge=0)


class QuotaReq(PolicyReq):
    expected_management_version: StrictInt = Field(ge=0)
    reason: str = ''


class StatusReq(_StrictPayload):
    status: Literal['active', 'disabled']
    expected_version: StrictInt = Field(ge=0)
    reason: str = ''


class RoleReq(_StrictPayload):
    role: Literal['guest', 'user', 'admin']
    expected_version: StrictInt = Field(ge=0)
    quota: PolicyReq | None = None
    reason: str = ''


class TransferReq(_StrictPayload):
    target_id: StrictInt = Field(gt=0)
    expected_governance_version: StrictInt = Field(ge=0)
    expected_actor_version: StrictInt = Field(ge=0)
    expected_target_version: StrictInt = Field(ge=0)


class PasswordReq(TransferReq):
    password: str


class EmptyReq(_StrictPayload):
    pass


@router.get('/api/admin/users')
def users(response: Response, user: CurrentUserDep, db: ReadyDatabaseDep,
          cursor: int = 0, limit: int = 50, q: str = '', role: str | None = None, status: str | None = None):
    no_store(response)
    with service_errors():
        return service.list_users(db, user.id, cursor=cursor, limit=limit, q=q, role=role, status=status)


@router.get('/api/admin/users/{target_id}')
def user_detail(target_id: int, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        return service.get_user(db, user.id, target_id)


@router.patch('/api/admin/users/{target_id}/status')
def status(target_id: int, req: StatusReq, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        return service.update_status(db, user.id, target_id, **req.model_dump())


@router.patch('/api/admin/users/{target_id}/role')
def role(target_id: int, req: RoleReq, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        return service.update_role(db, user.id, target_id, **req.model_dump())


@router.put('/api/admin/users/{target_id}/quota')
def quota(target_id: int, req: QuotaReq, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        return service.update_quota(db, user.id, target_id, **req.model_dump())


@router.get('/api/admin/governance')
def governance(response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        return service.governance(db, user.id)


@router.get('/api/admin/audit-events')
def audit_events(response: Response, user: CurrentUserDep, db: ReadyDatabaseDep, cursor: int = 0, limit: int = 50):
    no_store(response)
    with service_errors():
        return service.audit_events(db, user.id, cursor=cursor, limit=limit)


def _set_proof(response, request, proof):
    response.set_cookie(PROOF_COOKIE, proof, httponly=True, samesite='strict', path=PROOF_PATH,
                        secure=request.url.scheme == 'https', max_age=service.proof_ttl())


@router.post('/api/admin/owner-transfer/verify')
def verify_password(req: PasswordReq, request: Request, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        result = service.prepare_transfer(db, user.id, request.cookies.get(SESSION_COOKIE),
                                          provider='password', **req.model_dump())
    _set_proof(response, request, result.pop('proof'))
    return result


@router.post('/api/admin/owner-transfer/github')
def verify_github(req: TransferReq, request: Request, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    config = github_auth.config_from_env()
    if config is None:
        raise HTTPException(404, '未配置GitHub登录')
    with service_errors():
        result = service.prepare_transfer(db, user.id, request.cookies.get(SESSION_COOKIE),
                                          provider='github', **req.model_dump())
    state = 'owner_' + result['proof']
    response.set_cookie(STATE_COOKIE, state, httponly=True, samesite='lax', path=STATE_PATH,
                        secure=request.url.scheme == 'https', max_age=service.proof_ttl())
    return {'authorize_url': github_auth.authorize_url(config, state=state,
                redirect_uri=str(request.url_for('api_github_callback')))}


def github_callback(request, db, *, code, state, error):
    """Called by the existing OAuth callback; never creates a login or transfers ownership."""
    expected = request.cookies.get(STATE_COOKIE, '')
    response = RedirectResponse('/?owner_transfer_error=verification', status_code=303)
    try:
        if (error or not expected or not secrets.compare_digest(expected, state) or not code
                or not state.startswith('owner_')):
            return response
        config = github_auth.config_from_env()
        if config is None:
            return response
        profile = github_auth.fetch_profile(config, code=code[:200],
                                            redirect_uri=str(request.url_for('api_github_callback')))
        result = service.verify_github_transfer(db, state[len('owner_'):],
                                               request.cookies.get(SESSION_COOKIE), profile)
        response = RedirectResponse(f"/?owner_transfer_ready=1&target_id={result['target_id']}", status_code=303)
        _set_proof(response, request, state[len('owner_'):])
    except (ServiceError, github_auth.GitHubLoginError):
        pass
    finally:
        response.delete_cookie(STATE_COOKIE, path=STATE_PATH, httponly=True, samesite='lax',
                               secure=request.url.scheme == 'https')
        no_store(response)
    return response


@router.get('/api/admin/owner-transfer/status')
def transfer_status(request: Request, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        return service.transfer_status(db, user.id, request.cookies.get(SESSION_COOKIE), request.cookies.get(PROOF_COOKIE))


@router.post('/api/admin/owner-transfer')
def transfer(_req: EmptyReq, request: Request, response: Response, user: CurrentUserDep, db: ReadyDatabaseDep):
    no_store(response)
    with service_errors():
        result = service.transfer_owner(db, user.id, request.cookies.get(SESSION_COOKIE), request.cookies.get(PROOF_COOKIE))
    response.delete_cookie(PROOF_COOKIE, path=PROOF_PATH)
    response.delete_cookie(SESSION_COOKIE, path='/')
    return result
