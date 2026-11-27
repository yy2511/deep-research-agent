"""任务队列状态机、尝试扩展与跨进程补充消息表（阶段 3 第 1 步，#84）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-28
"""
from __future__ import annotations

from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_BIG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_DT = sa.DateTime(timezone=True)
_TASK_STATUS_SQL = "status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled', 'interrupted')"
_DELIVERY_STATUS_SQL = "delivery_status IS NULL OR delivery_status IN ('done', 'partial', 'failed', 'cancelled')"
_INPUT_STATE_SQL = "input_state IN ('open', 'closing', 'closed')"
_MESSAGE_STATUS_SQL = "status IN ('queued', 'included', 'undelivered')"


def _backfill_existing_records(bind) -> None:
    tasks = sa.table(
        "research_tasks",
        sa.column("id", _BIG_ID),
        sa.column("conversation_id", sa.String(64)),
        sa.column("created_at", _DT),
        sa.column("turn_index", sa.Integer()),
        sa.column("status", sa.String(16)),
        sa.column("current_run_id", sa.String(64)),
        sa.column("updated_at", _DT),
    )
    attempts = sa.table(
        "run_attempts",
        sa.column("id", _BIG_ID),
        sa.column("task_id", _BIG_ID),
        sa.column("run_id", sa.String(64)),
        sa.column("created_at", _DT),
        sa.column("attempt_no", sa.Integer()),
        sa.column("status", sa.String(16)),
        sa.column("finished_at", _DT),
        sa.column("input_state", sa.String(16)),
    )

    attempt_rows = bind.execute(
        sa.select(attempts.c.id, attempts.c.task_id, attempts.c.run_id, attempts.c.created_at)
        .order_by(attempts.c.task_id, attempts.c.created_at, attempts.c.id)
    ).all()
    latest_run_by_task: dict[int, str] = {}
    attempts_by_task: dict[int, int] = defaultdict(int)
    for row in attempt_rows:
        attempts_by_task[row.task_id] += 1
        attempt_no = attempts_by_task[row.task_id]
        latest_run_by_task[row.task_id] = row.run_id
        bind.execute(
            sa.update(attempts)
            .where(attempts.c.id == row.id)
            .values(
                attempt_no=attempt_no,
                status="succeeded",
                finished_at=row.created_at,
                input_state="closed",
            )
        )

    task_rows = bind.execute(
        sa.select(tasks.c.id, tasks.c.conversation_id, tasks.c.created_at)
        .order_by(tasks.c.conversation_id, tasks.c.created_at, tasks.c.id)
    ).all()
    turns_by_conversation: dict[str, int] = defaultdict(int)
    for row in task_rows:
        turns_by_conversation[row.conversation_id] += 1
        turn_index = turns_by_conversation[row.conversation_id]
        bind.execute(
            sa.update(tasks)
            .where(tasks.c.id == row.id)
            .values(
                turn_index=turn_index,
                status="succeeded",
                current_run_id=latest_run_by_task.get(row.id),
                updated_at=row.created_at,
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        if not bind.connection.driver_connection.in_transaction:
            bind.exec_driver_sql("BEGIN")
        op.execute(sa.text("PRAGMA defer_foreign_keys=ON"))

    # 1. 先加可空过渡列，为存量记录按对话与时间回填序号与终态，再收紧 NOT NULL 与唯一/检查约束。
    op.add_column("research_tasks", sa.Column("turn_index", sa.Integer(), nullable=True))
    op.add_column("research_tasks", sa.Column("status", sa.String(length=16), nullable=True))
    op.add_column("research_tasks", sa.Column("idempotency_key", sa.String(length=160), nullable=True))
    op.add_column("research_tasks", sa.Column("request_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("research_tasks", sa.Column("config_snapshot", sa.Text(), nullable=True))
    op.add_column("research_tasks", sa.Column("current_run_id", sa.String(length=64), nullable=True))
    op.add_column(
        "research_tasks",
        sa.Column("free_interrupted_retries_used", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("research_tasks", sa.Column("cancel_requested_at", _DT, nullable=True))
    op.add_column("research_tasks", sa.Column("updated_at", _DT, nullable=True))

    op.add_column(
        "run_attempts",
        sa.Column("attempt_no", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("run_attempts", sa.Column("retry_of_run_id", sa.String(length=64), nullable=True))
    op.add_column("run_attempts", sa.Column("idempotency_key", sa.String(length=160), nullable=True))
    op.add_column("run_attempts", sa.Column("status", sa.String(length=16), nullable=True))
    op.add_column("run_attempts", sa.Column("delivery_status", sa.String(length=16), nullable=True))
    op.add_column("run_attempts", sa.Column("worker_id", sa.String(length=128), nullable=True))
    op.add_column("run_attempts", sa.Column("heartbeat_at", _DT, nullable=True))
    op.add_column("run_attempts", sa.Column("started_at", _DT, nullable=True))
    op.add_column("run_attempts", sa.Column("finished_at", _DT, nullable=True))
    op.add_column("run_attempts", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "run_attempts",
        sa.Column("last_event_seq", sa.Integer(), server_default="-1", nullable=False),
    )
    op.add_column("run_attempts", sa.Column("input_state", sa.String(length=16), nullable=True))
    op.add_column(
        "run_attempts",
        sa.Column("input_revision", sa.Integer(), server_default="0", nullable=False),
    )

    _backfill_existing_records(bind)

    with op.batch_alter_table("research_tasks") as batch:
        batch.alter_column("turn_index", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("status", existing_type=sa.String(length=16), nullable=False)
        batch.alter_column("updated_at", existing_type=_DT, nullable=False)
        batch.create_unique_constraint("uq_research_tasks_conversation_turn", ["conversation_id", "turn_index"])
        batch.create_unique_constraint("uq_research_tasks_user_idempotency", ["user_id", "idempotency_key"])
        batch.create_check_constraint(op.f("ck_research_tasks_turn_index_pos"), "turn_index >= 1")
        batch.create_check_constraint(op.f("ck_research_tasks_status"), _TASK_STATUS_SQL)
        batch.create_check_constraint(
            op.f("ck_research_tasks_free_retries_nonneg"),
            "free_interrupted_retries_used >= 0",
        )
    op.create_index(op.f("ix_research_tasks_current_run_id"), "research_tasks", ["current_run_id"])
    op.create_index("ix_research_tasks_status_created_at", "research_tasks", ["status", "created_at"])

    with op.batch_alter_table("run_attempts") as batch:
        batch.alter_column("status", existing_type=sa.String(length=16), nullable=False)
        batch.alter_column("input_state", existing_type=sa.String(length=16), nullable=False)
        batch.create_unique_constraint("uq_run_attempts_task_attempt_no", ["task_id", "attempt_no"])
        batch.create_unique_constraint("uq_run_attempts_task_idempotency", ["task_id", "idempotency_key"])
        batch.create_check_constraint(op.f("ck_run_attempts_attempt_no_pos"), "attempt_no >= 1")
        batch.create_check_constraint(op.f("ck_run_attempts_status"), _TASK_STATUS_SQL)
        batch.create_check_constraint(op.f("ck_run_attempts_delivery_status"), _DELIVERY_STATUS_SQL)
        batch.create_check_constraint(op.f("ck_run_attempts_last_event_seq_min"), "last_event_seq >= -1")
        batch.create_check_constraint(op.f("ck_run_attempts_input_state"), _INPUT_STATE_SQL)
        batch.create_check_constraint(op.f("ck_run_attempts_input_revision_nonneg"), "input_revision >= 0")
    op.create_index("ix_run_attempts_status_heartbeat_at", "run_attempts", ["status", "heartbeat_at"])

    op.create_table(
        "run_messages",
        sa.Column("id", _BIG_ID, autoincrement=True, nullable=False),
        sa.Column("attempt_id", _BIG_ID, nullable=False),
        sa.Column("message_id", sa.String(length=100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", _DT, nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_run_messages_revision_pos")),
        sa.CheckConstraint(_MESSAGE_STATUS_SQL, name=op.f("ck_run_messages_status")),
        sa.ForeignKeyConstraint(["attempt_id"], ["run_attempts.id"], name=op.f("fk_run_messages_attempt_id_run_attempts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_messages")),
        sa.UniqueConstraint("attempt_id", "message_id", name="uq_run_messages_attempt_message_id"),
        sa.UniqueConstraint("attempt_id", "revision", name="uq_run_messages_attempt_revision"),
    )
    op.create_index(op.f("ix_run_messages_attempt_id"), "run_messages", ["attempt_id"])

    if is_sqlite:
        if bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("任务队列状态机迁移后外键校验失败，迁移已中止")
        bind.exec_driver_sql("PRAGMA defer_foreign_keys=OFF")


def downgrade() -> None:
    raise RuntimeError("任务队列状态机迁移（0007）不能自动回退；请先备份并人工处理执行尝试与补充消息数据")
