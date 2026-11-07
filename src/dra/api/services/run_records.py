"""研究记录的归属（业务层，阶段 1 第 3 步 #30；设计 §1、§3.2、§5）。

发起研究、追问时“先占位，再启动，失败就撤销”：
1. 先生成 run_id，在一个事务里写入 conversations / research_tasks / run_attempts（占位）；
2. 再真正启动研究（建运行目录、开后台任务）；
3. 启动失败（名额已满、参数不对……）就删掉刚才的占位（补偿），原错误照常返回。
这样不会有“没有主人的研究在跑”，也不会留下“有记录、没目录”的研究。

第 1 阶段运行状态仍以 meta.json 为准，这里只记“属于谁、在哪个对话里”。
本模块的函数都是同步的、各自开短会话；在事件循环上调用时用 asyncio.to_thread（ADR 0006 规则 3）。
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select

from dra.api.services.db_errors import database_errors
from dra.api.services.errors import Conflict, InvalidRequest, NotFound
from dra.api.services.ids import RUN_ID_RE
from dra.db import Database
from dra.db_tables import Conversation, ResearchTask, RunAttempt, User, utcnow

log = logging.getLogger(__name__)

LEGACY_NOT_IMPORTED = (
    "这条研究还没有登记到数据库，暂时不能追问。"
    "请先执行 python -m dra.admin import-legacy --owner <管理员邮箱>"
)


def reserve_new_run(db: Database, user_id: int, run_id: str, query: str) -> None:
    """新研究：新对话（id 即 run_id）+ 一个请求 + 一条执行记录，同一事务。"""
    now = utcnow()
    with database_errors(), db.session() as session:
        session.add(Conversation(id=run_id, user_id=user_id, created_at=now))
        task = ResearchTask(user_id=user_id, conversation_id=run_id, query=query, created_at=now)
        session.add(task)
        session.flush()
        session.add(RunAttempt(task_id=task.id, run_id=run_id, created_at=now))


def reserve_followup(db: Database, user_id: int, parent_run_id: str, run_id: str, query: str) -> str:
    """追问：在父研究所在的对话下加一个请求 + 一条执行记录；返回对话 id。

    父研究还没登记（旧数据未导入）时拒绝，不自动认领（用户 2026-11-07 确认）。
    对话不属于当前用户时按不存在处理（#31）；接口层的归属检查已先挡住，这里再守一道。
    """
    now = utcnow()
    with database_errors(), db.session() as session:
        conversation_id = session.scalar(
            select(ResearchTask.conversation_id)
            .join(RunAttempt, RunAttempt.task_id == ResearchTask.id)
            .where(RunAttempt.run_id == parent_run_id)
        )
        if conversation_id is None:
            raise Conflict(LEGACY_NOT_IMPORTED)
        # 第 4 步（#31）：对话必须属于当前用户；不属于与不存在同样处理。
        owner = session.scalar(select(Conversation.user_id).where(Conversation.id == conversation_id))
        if owner != user_id:
            raise NotFound("run 不存在")
        task = ResearchTask(user_id=user_id, conversation_id=conversation_id, query=query,
                            created_at=now)
        session.add(task)
        session.flush()
        session.add(RunAttempt(task_id=task.id, run_id=run_id, created_at=now))
    return conversation_id


def release(db: Database, run_id: str) -> None:
    """撤销占位：删执行记录、请求；对话里没有别的请求时连对话一起删。"""
    with database_errors(), db.session() as session:
        attempt = session.scalars(select(RunAttempt).where(RunAttempt.run_id == run_id)).first()
        if attempt is None:
            return
        task = session.get(ResearchTask, attempt.task_id)
        session.delete(attempt)
        session.flush()
        conversation_id = task.conversation_id
        session.delete(task)
        session.flush()
        remaining = session.scalar(select(func.count()).select_from(ResearchTask)
                                   .where(ResearchTask.conversation_id == conversation_id))
        if not remaining:
            session.execute(delete(Conversation).where(Conversation.id == conversation_id))


def release_quietly(db: Database, run_id: str) -> None:
    """补偿时用：撤销失败只记日志，不盖住原来的错误。"""
    try:
        release(db, run_id)
    except Exception:
        log.warning("撤销研究 %s 的数据库占位失败，留下了没有运行目录的记录", run_id, exc_info=True)


# ---- 归属查询（第 4 步 #31；设计 §3.1） -----------------------------------------------

def run_owner(db: Database, run_id: str) -> int | None:
    """研究属于谁；没登记过返回 None。"""
    with database_errors(), db.session() as session:
        return session.scalar(
            select(ResearchTask.user_id)
            .join(RunAttempt, RunAttempt.task_id == ResearchTask.id)
            .where(RunAttempt.run_id == run_id)
        )


def conversation_owner(db: Database, conversation_id: str) -> int | None:
    with database_errors(), db.session() as session:
        return session.scalar(select(Conversation.user_id).where(Conversation.id == conversation_id))


def owned_run_ids(db: Database, user_id: int) -> set[str]:
    with database_errors(), db.session() as session:
        return set(session.scalars(
            select(RunAttempt.run_id)
            .join(ResearchTask, ResearchTask.id == RunAttempt.task_id)
            .where(ResearchTask.user_id == user_id)
        ))


def owned_conversation_ids(db: Database, user_id: int) -> set[str]:
    with database_errors(), db.session() as session:
        return set(session.scalars(select(Conversation.id).where(Conversation.user_id == user_id)))


# ---- 旧数据导入（设计 §5） ---------------------------------------------------------

@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)


def _read_meta(directory: Path) -> dict:
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise TypeError("meta.json 不是对象")
    return meta


def import_legacy(db: Database, runs_root: Path, owner_email: str) -> ImportReport:
    """把 runs/ 下的旧研究全部登记到指定管理员名下。只读文件，可重复执行。"""
    from datetime import UTC, datetime

    email = (owner_email or "").strip().lower()
    with database_errors(), db.session() as session:
        owner = session.scalars(select(User).where(User.email == email)).first()
        if owner is None:
            raise NotFound(f"用户 {email} 不存在")
        if owner.role != "admin":
            raise InvalidRequest(f"{email} 不是管理员；旧数据只导入到管理员名下")
        owner_id = owner.id
        known = set(session.scalars(select(RunAttempt.run_id)))

    def at(ts: float):
        if not math.isfinite(ts):
            raise ValueError(f"started_at 不是有效时间：{ts}")
        return datetime.fromtimestamp(ts, UTC) if ts > 0 else utcnow()

    report = ImportReport()
    turns: list[tuple[str, int, datetime, str, str]] = []  # (对话, 轮次, 开始时间, run_id, 问题)
    root = Path(runs_root)
    for directory in sorted(root.iterdir()) if root.is_dir() else []:
        if not directory.is_dir() or directory.name.startswith("."):
            continue  # 执行工作区（.workspaces）等内部目录
        run_id = directory.name
        if not RUN_ID_RE.fullmatch(run_id):
            report.unreadable.append((run_id, "目录名不是研究编号"))
            continue
        if run_id in known:
            report.already.append(run_id)
            continue
        try:
            meta = _read_meta(directory)
            if meta.get("run_id", run_id) != run_id:
                raise ValueError("meta.json 里的 run_id 与目录名不一致")
            conversation_id = str(meta.get("conversation_id") or run_id)
            if not RUN_ID_RE.fullmatch(conversation_id):
                raise ValueError("conversation_id 格式不对")
            # 时间在逐目录检查时就转换好：坏值（毫秒、无穷大、NaN……）只跳过这一个目录，
            # 不在后面的整批事务里报错、把正常目录一起回滚（#36 审查）。
            started_at = at(float(meta.get("started_at") or 0))
            turns.append((conversation_id, int(meta.get("turn_index") or 1),
                          started_at, run_id, str(meta.get("query") or "")))
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            report.unreadable.append((run_id, f"{exc.__class__.__name__}: {exc}"))

    turns.sort(key=lambda t: (t[0], t[1], t[2]))
    with database_errors(), db.session() as session:
        for conversation_id, _turn, started, run_id, query in turns:
            if session.get(Conversation, conversation_id) is None:
                session.add(Conversation(id=conversation_id, user_id=owner_id, created_at=started))
                session.flush()
            task = ResearchTask(user_id=owner_id, conversation_id=conversation_id, query=query,
                                created_at=started)
            session.add(task)
            session.flush()
            session.add(RunAttempt(task_id=task.id, run_id=run_id, created_at=started))
            report.imported.append(run_id)
    return report
