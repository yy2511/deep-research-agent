"""平台账号治理；不读取研究内容。写操作锁治理行后按用户 ID 加锁。"""
from __future__ import annotations

import json
import os
import secrets
from datetime import timedelta

from sqlalchemy import or_, select, text

from dra.api.services import auth
from dra.api.services.db_errors import database_errors
from dra.api.services.errors import Conflict, Forbidden, InvalidRequest, NotFound, Unauthorized, Unavailable
from dra.db_tables import (LoginSession, ManagementAudit, OwnerTransferProof, PlatformGovernance,
                           User, UserIdentity, UserQuotaPolicy, utcnow)

MAX_LIMIT = 2**63 - 1


def json_count(value):
    """Preserve BigInteger precision in JavaScript while retaining small-number compatibility."""
    return str(value) if value is not None and value > 2**53 - 1 else value


def begin_write(session):
    if session.get_bind().dialect.name == 'sqlite':
        session.execute(text('BEGIN IMMEDIATE'))


def lock_governance(session):
    g = session.scalars(select(PlatformGovernance).where(PlatformGovernance.id == 1).with_for_update()).first()
    if g is None:
        raise Unavailable('平台治理未初始化，请先运行数据库迁移')
    return g


def _users(session, ids):
    return {u.id: u for u in session.scalars(select(User).where(User.id.in_(set(ids)))
                                             .order_by(User.id).with_for_update()).all()}


def _actor(session, actor_id):
    u = session.get(User, actor_id)
    if u is None or u.status != 'active' or u.role not in ('admin', 'owner'):
        raise Forbidden('需要平台管理权限')
    return u


def _target(actor, users, target_id):
    u = users.get(target_id)
    if u is None:
        raise NotFound('用户不存在')
    if u.id == actor.id or u.role == 'owner':
        raise Forbidden('不能通过此操作修改本人或平台负责人')
    if actor.role == 'admin' and u.role not in ('user', 'guest'):
        raise Forbidden('平台管理员只能管理普通用户和访客')
    return u


def _version(actual, expected):
    if actual != expected:
        raise Conflict('资料已被其他操作修改，请刷新后重试')


def _audit(session, actor_id, target_id, action, before, after, reason=''):
    session.add(ManagementAudit(actor_id=actor_id, target_id=target_id, action=action,
        before_json=json.dumps(before, ensure_ascii=False, default=lambda x: x.isoformat()),
        after_json=json.dumps(after, ensure_ascii=False, default=lambda x: x.isoformat()), reason=reason))


def audit_timezone(session, actor_id, before, after):
    _audit(session, actor_id, None, 'quota_timezone', before, after)


def policy_dict(p):
    return None if p is None else {'mode': p.mode, 'daily_runs_limit': json_count(p.daily_runs_limit),
                                   'daily_briefs_limit': json_count(p.daily_briefs_limit), 'version': p.version}


def _row(session, u):
    github = session.scalars(select(UserIdentity.login).where(UserIdentity.user_id == u.id,
                                                             UserIdentity.provider == 'github')).first()
    return {'id': u.id, 'email': u.email, 'display_name': u.display_name, 'github_login': github,
            'role': u.role, 'status': u.status, 'management_version': u.management_version,
            'created_at': u.created_at.isoformat(), 'quota': policy_dict(session.get(UserQuotaPolicy, u.id))}


def _add_usage(db, row):
    from dra.api.services import quotas
    row['usage'] = quotas.status(db, auth.UserInfo(row['id'], row['email'], row['role']), allow_disabled=True)
    return row


def list_users(db, actor_id, *, cursor=0, limit=50, q='', role=None, status=None):
    if cursor < 0 or limit < 1:
        raise InvalidRequest('分页参数无效')
    if role is not None and role not in ('owner', 'admin', 'user', 'guest'):
        raise InvalidRequest('角色无效')
    if status is not None and status not in ('active', 'disabled'):
        raise InvalidRequest('状态无效')
    with database_errors(), db.session() as s:
        _actor(s, actor_id)
        query = select(User).where(User.id > cursor)
        if role:
            query = query.where(User.role == role)
        if status:
            query = query.where(User.status == status)
        if q.strip():
            # Autoescape LIKE special characters; no raw SQL or research-content search.
            needle = q.strip()
            identities = select(UserIdentity.user_id).where(UserIdentity.login.icontains(needle, autoescape=True))
            query = query.where(or_(User.email.icontains(needle, autoescape=True),
                                   User.display_name.icontains(needle, autoescape=True), User.id.in_(identities)))
        users = list(s.scalars(query.order_by(User.id).limit(limit + 1)))
        rows = [_row(s, u) for u in users[:limit]]
        g = s.get(PlatformGovernance, 1)
        result = {'items': rows, 'next_cursor': rows[-1]['id'] if len(users) > limit else None,
                  'governance': {'owner_user_id': g.owner_user_id, 'version': g.version}}
    for r in rows:
        _add_usage(db, r)
    return result


def get_user(db, actor_id, target_id):
    with database_errors(), db.session() as s:
        _actor(s, actor_id)
        u = s.get(User, target_id)
        if u is None:
            raise NotFound('用户不存在')
        row = _row(s, u)
    return _add_usage(db, row)


def _policy(s, actor, target, data):
    mode = data.get('mode')
    runs, briefs = data.get('daily_runs_limit'), data.get('daily_briefs_limit')
    if mode not in ('limited', 'unlimited'):
        raise InvalidRequest('额度模式无效')
    if mode == 'unlimited':
        if actor is not None and actor.role != 'owner':
            raise Forbidden('只有负责人可以授予不限次数')
        if runs is not None or briefs is not None:
            raise InvalidRequest('不限次数时不可填写限额')
    elif any(type(x) is not int or not 0 <= x <= MAX_LIMIT for x in (runs, briefs)):
        raise InvalidRequest(f'每日次数须为0到{MAX_LIMIT}的整数')
    p = s.get(UserQuotaPolicy, target.id)
    _version(None if p is None else p.version, data.get('expected_version'))
    before = policy_dict(p)
    if p is None:
        p = UserQuotaPolicy(user_id=target.id, mode=mode, version=0)
        s.add(p)
    else:
        p.version += 1
    p.mode, p.daily_runs_limit, p.daily_briefs_limit = mode, runs, briefs
    p.updated_by, p.updated_at = actor.id if actor else None, utcnow()
    return before, policy_dict(p)


def update_quota(db, actor_id, target_id, *, expected_management_version, reason='', **data):
    with database_errors(), db.session() as s:
        begin_write(s)
        lock_governance(s)
        users = _users(s, [actor_id, target_id])
        actor = _actor(s, actor_id)
        target = _target(actor, users, target_id)
        _version(target.management_version, expected_management_version)
        if target.role != 'user':
            raise InvalidRequest('只能为普通用户设置个人额度；访客使用平台通用配置')
        before, after = _policy(s, actor, target, data)
        _audit(s, actor.id, target.id, 'quota', before, after, reason)
    return get_user(db, actor_id, target_id)


def update_status(db, actor_id, target_id, *, status, expected_version, reason=''):
    if status not in ('active', 'disabled'):
        raise InvalidRequest('状态无效')
    with database_errors(), db.session() as s:
        begin_write(s)
        lock_governance(s)
        users = _users(s, [actor_id, target_id])
        actor = _actor(s, actor_id)
        target = _target(actor, users, target_id)
        _version(target.management_version, expected_version)
        before = {'role': target.role, 'status': target.status}
        target.status, target.updated_at = status, utcnow()
        target.management_version += 1
        auth._revoke_user_sessions(s, target.id, utcnow())
        _audit(s, actor.id, target.id, 'status', before, {'role': target.role, 'status': status}, reason)
    return get_user(db, actor_id, target_id)


def update_role(db, actor_id, target_id, *, role, expected_version, quota=None, reason=''):
    if role not in ('admin', 'user', 'guest'):
        raise InvalidRequest('负责人只能通过移交流程设置')
    with database_errors(), db.session() as s:
        begin_write(s)
        lock_governance(s)
        users = _users(s, [actor_id, target_id])
        actor = _actor(s, actor_id)
        target = _target(actor, users, target_id)
        _version(target.management_version, expected_version)
        if role == 'admin' and actor.role != 'owner':
            raise Forbidden('只有负责人可以任命管理员')
        before = {'role': target.role, 'quota': policy_dict(s.get(UserQuotaPolicy, target.id))}
        if role == 'user':
            if quota is None:
                raise InvalidRequest('转为普通用户必须明确个人额度')
            _policy(s, actor, target, quota)
        elif quota is not None:
            raise InvalidRequest('该角色不接受个人额度')
        target.role, target.updated_at = role, utcnow()
        target.management_version += 1
        auth._revoke_user_sessions(s, target.id, utcnow())
        _audit(s, actor.id, target.id, 'role', before,
               {'role': role, 'quota': policy_dict(s.get(UserQuotaPolicy, target.id))}, reason)
    return get_user(db, actor_id, target_id)


def governance(db, actor_id):
    with database_errors(), db.session() as s:
        actor = _actor(s, actor_id)
        g = s.get(PlatformGovernance, 1)
        methods = []
        if actor.password_hash:
            methods.append('password')
        if s.scalars(select(UserIdentity).where(UserIdentity.user_id == actor.id,
                                                UserIdentity.provider == 'github')).first():
            methods.append('github')
        return {'owner_user_id': g.owner_user_id, 'version': g.version,
                'reauth_methods': methods, 'reauth_ttl_seconds': proof_ttl()}


def proof_ttl():
    try:
        value = int(os.environ.get('DRA_OWNER_REAUTH_TTL_SECONDS', '300'))
        if value <= 0:
            raise ValueError
        return value
    except ValueError as exc:
        raise Unavailable('负责人验证有效期配置无效') from exc


def _session(s, token, actor_id):
    login = s.scalars(select(LoginSession).where(LoginSession.token_hash == auth.hash_secret(token or ''),
        LoginSession.user_id == actor_id, LoginSession.revoked_at.is_(None),
        LoginSession.expires_at > utcnow())).first()
    if login is None:
        raise Unauthorized('登录已失效，请重新登录')
    return login


def prepare_transfer(db, actor_id, token, *, target_id, expected_governance_version,
                     expected_actor_version, expected_target_version, provider, password=None):
    # Password verification is slow; do it before the locked transaction, then compare the hash again.
    verified_hash = None
    if provider == 'password':
        with database_errors(), db.session() as s:
            a = _actor(s, actor_id)
            verified_hash = a.password_hash
        if (verified_hash is None or not isinstance(password, str) or len(password) > auth.PASSWORD_MAX
                or not auth._verify(verified_hash, password)):
            raise Unauthorized('当前密码不正确')
    elif provider != 'github':
        raise InvalidRequest('验证方式无效')
    secret = secrets.token_urlsafe(32)
    with database_errors(), db.session() as s:
        begin_write(s)
        g = lock_governance(s)
        users = _users(s, [actor_id, target_id])
        actor = _actor(s, actor_id)
        target = users.get(target_id)
        if actor.role != 'owner' or g.owner_user_id != actor.id:
            raise Forbidden('只有当前负责人可以移交')
        if target is None or target.id == actor.id or target.role != 'admin' or target.status != 'active':
            raise InvalidRequest('移交目标必须是另一位有效管理员')
        _version(g.version, expected_governance_version)
        _version(actor.management_version, expected_actor_version)
        _version(target.management_version, expected_target_version)
        login = _session(s, token, actor_id)
        if provider == 'password' and actor.password_hash != verified_hash:
            raise Unauthorized('密码已变化，请重新验证')
        if provider == 'github' and not s.scalars(select(UserIdentity).where(
                UserIdentity.user_id == actor_id, UserIdentity.provider == 'github')).first():
            raise InvalidRequest('当前负责人未绑定GitHub身份')
        expires = utcnow() + timedelta(seconds=proof_ttl())
        s.add(OwnerTransferProof(token_hash=auth.hash_secret(secret), session_id=login.id,
             actor_id=actor.id, target_id=target.id, governance_version=g.version,
             actor_version=actor.management_version, target_version=target.management_version,
             expires_at=expires, provider=provider, verified_at=utcnow() if provider == 'password' else None))
    return {'proof': secret, 'expires_at': expires.isoformat(), 'target_id': target_id}


def _proof(s, secret, token, *, require_verified=True):
    p = s.get(OwnerTransferProof, auth.hash_secret(secret or ''))
    if p is None or p.expires_at <= utcnow() or p.consumed_at is not None:
        raise Unauthorized('移交验证已失效，请重新验证')
    if require_verified and p.verified_at is None:
        raise Unauthorized('尚未完成身份验证')
    login = _session(s, token, p.actor_id)
    if login.id != p.session_id:
        raise Unauthorized('移交验证不属于当前登录')
    return p


def verify_github_transfer(db, secret, token, profile):
    with database_errors(), db.session() as s:
        begin_write(s)
        g = lock_governance(s)
        p = _proof(s, secret, token, require_verified=False)
        users = _users(s, [p.actor_id, p.target_id])
        _validate_transfer(g, users, p)
        if p.provider != 'github' or p.verified_at is not None:
            raise Unauthorized('此验证不可重复使用')
        identity = s.scalars(select(UserIdentity).where(UserIdentity.user_id == p.actor_id,
            UserIdentity.provider == 'github', UserIdentity.subject == profile.subject)).first()
        if identity is None or profile.provider != 'github':
            raise Unauthorized('请使用当前负责人绑定的GitHub账号验证')
        p.verified_at = utcnow()
        return {'expires_at': p.expires_at.isoformat(), 'target_id': p.target_id}


def _validate_transfer(g, users, p):
    actor, target = users.get(p.actor_id), users.get(p.target_id)
    if (actor is None or target is None or actor.role != 'owner' or actor.status != 'active'
            or target.role != 'admin' or target.status != 'active' or g.owner_user_id != actor.id):
        raise Conflict('负责人或目标账号已变化，请重新验证')
    _version(g.version, p.governance_version)
    _version(actor.management_version, p.actor_version)
    _version(target.management_version, p.target_version)
    return actor, target


def transfer_status(db, actor_id, token, secret):
    if not secret:
        return {'ready': False}
    try:
        with database_errors(), db.session() as s:
            g = s.get(PlatformGovernance, 1)
            p = _proof(s, secret, token)
            if p.actor_id != actor_id:
                return {'ready': False}
            users = {u.id: u for u in s.scalars(select(User).where(User.id.in_([p.actor_id, p.target_id])))}
            _validate_transfer(g, users, p)
            return {'ready': True, 'target_id': p.target_id, 'expires_at': p.expires_at.isoformat()}
    except (Unauthorized, Conflict):
        return {'ready': False}


def transfer_owner(db, actor_id, token, secret):
    with database_errors(), db.session() as s:
        begin_write(s)
        g = lock_governance(s)
        p = _proof(s, secret, token)
        if p.actor_id != actor_id:
            raise Forbidden('移交验证不属于当前账号')
        users = _users(s, [p.actor_id, p.target_id])
        actor, target = _validate_transfer(g, users, p)
        _move_owner(s, g, actor, target, actor.id, 'owner_transfer', '')
        p.consumed_at = utcnow()
    return {'transferred': True}


def _move_owner(s, g, old, target, actor_id, action, reason):
    old_id = old.id if old else None
    if old:
        old.role = 'admin'
        old.management_version += 1
        s.flush()  # release unique owner index before promoting successor
        auth._revoke_user_sessions(s, old.id, utcnow())
    target.role = 'owner'
    target.management_version += 1
    g.owner_user_id = target.id
    g.version += 1
    auth._revoke_user_sessions(s, target.id, utcnow())
    _audit(s, actor_id, target.id, action, {'owner_user_id': old_id}, {'owner_user_id': target.id}, reason)


def bootstrap_owner(db, target_id):
    with database_errors(), db.session() as s:
        begin_write(s)
        g = lock_governance(s)
        users = _users(s, [target_id])
        if g.owner_user_id is not None or s.scalars(select(User.id).where(User.role == 'owner')).first():
            raise Conflict('负责人已存在，请使用移交或恢复流程')
        target = users.get(target_id)
        if target is None or target.role != 'admin' or target.status != 'active':
            raise InvalidRequest('初始负责人必须是现有有效管理员')
        _move_owner(s, g, None, target, None, 'bootstrap_owner', '部署维护者显式初始化')


def recover_owner(db, old_id, target_id, reason):
    if not reason.strip():
        raise InvalidRequest('恢复操作必须填写原因')
    with database_errors(), db.session() as s:
        begin_write(s)
        g = lock_governance(s)
        users = _users(s, [old_id, target_id])
        old, target = users.get(old_id), users.get(target_id)
        if g.owner_user_id != old_id or old is None or old.role != 'owner':
            raise Conflict('当前负责人不匹配，请重新核对')
        if target is None or target.id == old.id or target.role != 'admin' or target.status != 'active':
            raise InvalidRequest('新负责人必须是另一位有效管理员')
        _move_owner(s, g, old, target, None, 'recover_owner', reason)


def cli_users(db):
    with database_errors(), db.session() as s:
        return [{'id': u.id, 'email': u.email, 'display_name': u.display_name,
                 'role': u.role, 'status': u.status} for u in s.scalars(select(User).order_by(User.id))]


def audit_events(db, actor_id, *, cursor=0, limit=50):
    if cursor < 0 or limit < 1:
        raise InvalidRequest('分页参数无效')
    with database_errors(), db.session() as s:
        actor = _actor(s, actor_id)
        query = select(ManagementAudit).where(ManagementAudit.id > cursor)
        if actor.role != 'owner':
            targets = select(User.id).where(User.role.in_(['user', 'guest']))
            query = query.where(ManagementAudit.target_id.in_(targets),
                                ManagementAudit.action.in_(['status', 'quota', 'role']))
        rows = list(s.scalars(query.order_by(ManagementAudit.id).limit(limit + 1)))
        items = [{'id': a.id, 'actor_id': a.actor_id, 'target_id': a.target_id,
                  'action': a.action, 'before': json.loads(a.before_json), 'after': json.loads(a.after_json),
                  'reason': a.reason, 'created_at': a.created_at.isoformat()} for a in rows[:limit]]
        return {'items': items, 'next_cursor': items[-1]['id'] if len(rows) > limit else None}
