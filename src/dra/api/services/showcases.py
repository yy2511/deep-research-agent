"""示例：管理员把已完成的纯网页研究对话设为示例，所有人（含未登录）只读可见。

设计：docs/design/阶段2-公开试用.md §4；决定：ADR 0007。阶段 2 第 2 步（#47）。

- 设为示例的条件：对话属于管理员；数据库里登记的每一轮都有运行目录且已结束（没有占位了还没
  结束的追问）；每一轮都是纯网页研究（没有选资料库、外部资料服务）。
- 示例期间对话冻结：``run_records.reserve_followup`` 拒绝任何人追问，包括原主人。
- ``add`` 与 ``reserve_followup`` 都先锁住 ``conversations`` 里这一行，同时发生时排队：
  先发布则追问被拒；先追问则发布时发现有未结束的轮次而拒绝。公开的始终是发布时确认过的那几轮。
- 研究文件不复制；示例看的就是原来那份。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, select

from dra.api.services.db_errors import database_errors
from dra.api.services.errors import Conflict, InvalidRequest, NotFound
from dra.api.services.ids import RUN_ID_RE
from dra.api.services.run_records import conversation_run_ids
from dra.db import Database
from dra.db_tables import Conversation, ResearchTask, RunAttempt, Showcase, User, utcnow
from dra.runstore import RunStore

TITLE_MAX = 200
_UNFINISHED = {"running", "preparing"}


@dataclass
class ShowcaseReview:
    """设为示例前的检查结果：会公开哪些轮次、附件和执行记录。"""

    conversation_id: str
    turns: list[dict] = field(default_factory=list)   # 每轮的 run_id、问题、状态
    artifacts: list[str] = field(default_factory=list)  # “run_id/文件名”
    executions: int = 0


def _check(session, store: RunStore, conversation_id: str) -> ShowcaseReview:
    """在调用方的会话（已锁对话行）里检查能否设为示例；不能时抛 InvalidRequest 说明原因。"""
    owner = session.execute(
        select(User.role).join(Conversation, Conversation.user_id == User.id)
        .where(Conversation.id == conversation_id)
    ).first()
    if owner is None:
        raise NotFound("对话不存在")
    if owner.role != "admin":
        raise InvalidRequest("只能把管理员自己的对话设为示例")
    review = ShowcaseReview(conversation_id)
    run_ids = conversation_run_ids(session, conversation_id)
    if not run_ids:
        raise InvalidRequest("对话里没有登记的研究")
    for run_id in run_ids:
        meta = store.get_meta(run_id)
        if meta is None:
            # 占位了还没建目录（追问刚提交），或文件已删除。
            raise InvalidRequest(f"第 {run_id} 轮没有可读的运行记录（可能还在启动），不能设为示例")
        if meta.get("status") in _UNFINISHED or meta.get("finished_at") is None:
            raise InvalidRequest(f"研究 {run_id} 还没有结束，不能设为示例")
        scope = meta.get("config_summary") or {}
        if (scope.get("source_mode", "web") != "web" or (scope.get("library_selection") or {}).get("version_ids")
                or scope.get("mcp_server_ids")):
            raise InvalidRequest(f"研究 {run_id} 用了私域资料库或外部资料服务，示例只能是纯网页研究")
        review.turns.append({"run_id": run_id, "query": meta.get("query") or "",
                             "status": meta.get("status"), "turn_index": meta.get("turn_index", 1)})
        review.artifacts += [f"{run_id}/{record.get('filename') or record.get('artifact_id')}"
                             for record in _artifacts(store, run_id)]
        review.executions += _execution_count(store, run_id)
    review.turns.sort(key=lambda t: t["turn_index"])
    return review


def _artifacts(store: RunStore, run_id: str) -> list[dict]:
    """与 ``GET /api/runs/{run_id}/artifacts`` 同一口径：已提交、会对外提供下载的附件。"""
    from dra.artifacts import committed_records
    return list(committed_records(store.read_simple_result(run_id)))


def _execution_count(store: RunStore, run_id: str) -> int:
    """执行日志（每次执行一个 id，stdout / stderr 各一个文件）。"""
    logs = store._dir(run_id) / "execution" / "logs"
    return len({p.name.rsplit(".", 1)[0] for p in logs.iterdir()}) if logs.is_dir() else 0


def _lock_conversation(session, conversation_id: str) -> None:
    session.execute(select(Conversation.id).where(Conversation.id == conversation_id).with_for_update())


def review(db: Database, store: RunStore, conversation_id: str) -> ShowcaseReview:
    """只检查、不写入：给管理命令在确认前列出将公开的内容。"""
    _valid_id(conversation_id)
    with database_errors(), db.session() as session:
        return _check(session, store, conversation_id)


def add(db: Database, store: RunStore, conversation_id: str, title: str, *, sort_order: int = 0) -> ShowcaseReview:
    """设为示例。锁住对话行后重新检查一遍再写入（确认期间可能有人追问）。"""
    _valid_id(conversation_id)
    title = (title or "").strip()
    if not title or len(title) > TITLE_MAX:
        raise InvalidRequest(f"标题须为 1–{TITLE_MAX} 个字符")
    with database_errors(), db.session() as session:
        _lock_conversation(session, conversation_id)
        if session.get(Showcase, conversation_id) is not None:
            raise Conflict("该对话已经是示例")
        result = _check(session, store, conversation_id)
        session.add(Showcase(conversation_id=conversation_id, title=title, sort_order=sort_order,
                             created_at=utcnow()))
    return result


def remove(db: Database, conversation_id: str) -> None:
    with database_errors(), db.session() as session:
        if session.execute(delete(Showcase).where(Showcase.conversation_id == conversation_id)).rowcount != 1:
            raise NotFound("该对话不是示例")


def list_public(db: Database, store: RunStore) -> list[dict]:
    """示例列表（不要求登录）。只用数据库登记的轮次；文件已删的标为不可用。"""
    with database_errors(), db.session() as session:
        rows = session.scalars(select(Showcase).order_by(Showcase.sort_order, Showcase.created_at)).all()
        items = [(row.conversation_id, row.title, conversation_run_ids(session, row.conversation_id))
                 for row in rows]
    result = []
    for conversation_id, title, run_ids in items:
        turns = store.conversation_turns(conversation_id, counted=run_ids.__contains__)
        result.append({
            "conversation_id": conversation_id, "title": title,
            "query": turns[0]["query"] if turns else "",
            "run_ids": [t["run_id"] for t in turns],
            "latest_run_id": turns[-1]["run_id"] if turns else None,
            "turn_count": len(turns),
            "finished_at": turns[-1].get("finished_at") if turns else None,
            "available": bool(turns) and len(turns) == len(run_ids),
        })
    return result


# ---- 读取权限（读取组接口用） --------------------------------------------------------

def run_access(db: Database, run_id: str) -> tuple[int | None, bool]:
    """（主人 id，是否属于某个示例）。没登记过返回 (None, False)。"""
    with database_errors(), db.session() as session:
        row = session.execute(
            select(ResearchTask.user_id, Showcase.conversation_id)
            .join(RunAttempt, RunAttempt.task_id == ResearchTask.id)
            .outerjoin(Showcase, Showcase.conversation_id == ResearchTask.conversation_id)
            .where(RunAttempt.run_id == run_id)
        ).first()
    return (None, False) if row is None else (row[0], row[1] is not None)


def conversation_access(db: Database, conversation_id: str) -> tuple[int | None, bool]:
    with database_errors(), db.session() as session:
        row = session.execute(
            select(Conversation.user_id, Showcase.conversation_id)
            .outerjoin(Showcase, Showcase.conversation_id == Conversation.id)
            .where(Conversation.id == conversation_id)
        ).first()
    return (None, False) if row is None else (row[0], row[1] is not None)


def _valid_id(conversation_id: str) -> None:
    if not RUN_ID_RE.fullmatch(conversation_id or ""):
        raise InvalidRequest("对话编号格式不对")
