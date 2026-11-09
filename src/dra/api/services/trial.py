"""公开试用：GitHub 访客的每日次数与功能限制（阶段 2 第 3 步，#49）。

设计：docs/design/阶段2-公开试用.md §1、§3；决定：ADR 0007。只作用于 ``guest``；邀请码用户与管理员不受影响。

- **次数**：研究（追问也算）在“先占位”的同一事务里扣，规划在调用模型之前扣。每次先扣个人
  （``user:<id>``）再扣全部访客（``guests``），用一条带条件的 upsert：没到上限才加 1，没返回行就是
  用完了，整个事务回滚。两个人同时抢最后一个名额，只有一个成功。
- **退回**：研究没能启动（撤销占位）时退回；已经开始、之后失败或被取消的不退（已经花了钱）。
  规划失败不退。
- **“一天”**：按试用设置的时区 0 点切换。
- **收紧**：访客的研究固定用试用模型与更紧的预算，只有网页来源，关闭运行代码与浏览器。请求里
  带了不允许的选项直接拒绝并说明，不静默改掉。

不依赖 FastAPI；每个函数自己开短会话（ADR 0006）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from dra.api.services.auth import UserInfo
from dra.api.services.db_errors import database_errors
from dra.api.services.errors import CapacityFull, InvalidRequest, Unavailable
from dra.db import Database
from dra.db_tables import DailyCounter, utcnow

RUN, BRIEF = "run", "brief"
GUESTS_SCOPE = "guests"
NOT_OPEN = "试用暂未开放，请先看看示例，或按 README 在本机运行。"
_LOCAL_HINT = "可以先看看示例，或按 README 在本机运行。"


class QuotaExceeded(CapacityFull):
    """今天的次数用完了（429）。``str(exc)`` 说明是个人还是全站，以及何时恢复。"""


@dataclass(frozen=True)
class Trial:
    """一个访客这一刻适用的试用规则（设置快照 + 今天是哪天）。"""

    user_id: int
    settings: object  # runtime_config.TrialSettings
    day: str
    resets_at: datetime

    def limit(self, kind: str, scope: str) -> int:
        s = self.settings
        if kind == RUN:
            return s.per_user_daily_runs if scope != GUESTS_SCOPE else s.global_daily_runs
        return s.per_user_daily_briefs if scope != GUESTS_SCOPE else s.global_daily_briefs

    @property
    def user_scope(self) -> str:
        return f"user:{self.user_id}"


def for_user(user: UserInfo, *, now: datetime | None = None) -> Trial | None:
    """访客返回试用规则；其他角色返回 None（不受限）。试用模型没设置时访客一律 503。"""
    if user.role != "guest":
        return None
    from dra import runtime_config

    settings = runtime_config.current().trial
    if settings.model is None or settings.provider is None:
        raise Unavailable(NOT_OPEN)
    day, resets_at = _today(settings.timezone, now or utcnow())
    return Trial(user.id, settings, day, resets_at)


def _today(timezone: str, now: datetime) -> tuple[str, datetime]:
    local = now.astimezone(ZoneInfo(timezone))
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.date().isoformat(), midnight


# ---- 扣次数与退回 --------------------------------------------------------------------

def _take(session, scope: str, day: str, kind: str, limit: int) -> bool:
    """没到上限就加 1 并返回 True；到了返回 False。PostgreSQL 与 SQLite（3.35+）都支持。"""
    if limit <= 0:
        return False
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(DailyCounter).values(scope=scope, day=day, kind=kind, count=1)
    statement = statement.on_conflict_do_update(
        index_elements=[DailyCounter.scope, DailyCounter.day, DailyCounter.kind],
        set_={"count": DailyCounter.count + 1},
        where=DailyCounter.count < limit,
    ).returning(DailyCounter.count)
    return session.execute(statement).first() is not None


def take_in_session(session, trial: Trial, kind: str) -> None:
    """在调用方的事务里扣个人再扣全站；任一用完抛 QuotaExceeded（调用方的事务随之回滚）。"""
    if not _take(session, trial.user_scope, trial.day, kind, trial.limit(kind, trial.user_scope)):
        raise QuotaExceeded(_exhausted(trial, kind, personal=True))
    if not _take(session, GUESTS_SCOPE, trial.day, kind, trial.limit(kind, GUESTS_SCOPE)):
        raise QuotaExceeded(_exhausted(trial, kind, personal=False))


def give_back_in_session(session, user_id: int, day: str, kind: str = RUN) -> None:
    """退回一次（撤销占位时）。计数不会低于 0。"""
    for scope in (f"user:{user_id}", GUESTS_SCOPE):
        session.execute(
            update(DailyCounter)
            .where(DailyCounter.scope == scope, DailyCounter.day == day, DailyCounter.kind == kind,
                   DailyCounter.count > 0)
            .values(count=DailyCounter.count - 1)
            .execution_options(synchronize_session=False)
        )


def take_brief(db: Database, trial: Trial) -> None:
    """规划之前扣一次规划次数。当天研究次数已用完时规划也拒绝：规划完也没法开始研究。"""
    with database_errors(), db.session() as session:
        used = _counts(session, trial, RUN)
        if used[trial.user_scope] >= trial.limit(RUN, trial.user_scope):
            raise QuotaExceeded(_exhausted(trial, RUN, personal=True))
        if used[GUESTS_SCOPE] >= trial.limit(RUN, GUESTS_SCOPE):
            raise QuotaExceeded(_exhausted(trial, RUN, personal=False))
        take_in_session(session, trial, BRIEF)


def _counts(session, trial: Trial, kind: str) -> dict[str, int]:
    rows = session.execute(
        select(DailyCounter.scope, DailyCounter.count)
        .where(DailyCounter.scope.in_([trial.user_scope, GUESTS_SCOPE]),
               DailyCounter.day == trial.day, DailyCounter.kind == kind)
    ).all()
    found = dict(rows)
    return {trial.user_scope: found.get(trial.user_scope, 0), GUESTS_SCOPE: found.get(GUESTS_SCOPE, 0)}


def status(db: Database, user: UserInfo) -> dict | None:
    """``GET /api/me`` 的 ``trial``：今天还能发起几次研究、上限、何时恢复。非访客为 None。"""
    if user.role != "guest":
        return None
    try:
        trial = for_user(user)
    except Unavailable:
        return {"open": False, "runs_left": 0, "runs_limit": 0, "resets_at": None}
    with database_errors(), db.session() as session:
        used = _counts(session, trial, RUN)
    personal_left = trial.limit(RUN, trial.user_scope) - used[trial.user_scope]
    global_left = trial.limit(RUN, GUESTS_SCOPE) - used[GUESTS_SCOPE]
    return {"open": True, "runs_left": max(0, min(personal_left, global_left)),
            "runs_limit": trial.limit(RUN, trial.user_scope), "global_exhausted": global_left <= 0,
            "resets_at": trial.resets_at.isoformat()}


def _exhausted(trial: Trial, kind: str, *, personal: bool) -> str:
    what = "研究" if kind == RUN else "规划"
    when = trial.resets_at.strftime("%m 月 %d 日 %H:%M")
    if personal:
        return f"你今天的试用{what}次数用完了，{when}恢复。{_LOCAL_HINT}"
    return f"今天的试用{what}名额已经用完了，{when}恢复。{_LOCAL_HINT}"


# ---- 访客研究的收紧 ------------------------------------------------------------------

def check_options(*, source_mode: str, document_ids, mcp_server_ids) -> None:
    """访客只能用网页来源；带了资料库或外部资料服务直接拒绝并说明。"""
    if source_mode != "web" or document_ids:
        raise InvalidRequest("试用账号只能用网页搜索，不能使用资料库")
    if mcp_server_ids:
        raise InvalidRequest("试用账号不能使用外部资料服务")


def restrict_config(config, trial: Trial):
    """访客研究的配置：试用模型与预算；关闭运行代码、浏览器与视觉兜底模型。"""
    s = trial.settings
    adapter = config.adapter.model_copy(update={"model": s.model, "provider": s.provider,
                                                "reasoning": False, "effort": None})
    return config.model_copy(update={
        "adapter": adapter,
        "execution": config.execution.model_copy(update={"enabled": False}),
        "browser": config.browser.model_copy(update={"enabled": False}),
        "vision": config.vision.model_copy(update={"provider": ""}),  # 没有备用视觉连接：不花另一家的钱
        "wall_timeout_s": s.wall_timeout_s,
        "max_llm_calls": s.max_llm_calls,
        "max_tool_calls": s.max_tool_calls,
        "mcp_servers": [],
    })


def planner_override(trial: Trial | None) -> dict:
    """访客规划用试用模型；其他人沿用全站设置。传给 runtime_config.to_brief_config。"""
    if trial is None:
        return {}
    return {"planner_model": trial.settings.model, "planner_provider": trial.settings.provider,
            "planner_effort": None}
