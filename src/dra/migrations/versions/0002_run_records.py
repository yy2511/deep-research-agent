"""建 conversations、research_tasks、run_attempts 三张表：研究记录的归属（阶段 1 第 3 步，#30）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

_BIG_ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    op.create_table('conversations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', _BIG_ID, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_conversations_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversations'))
    )
    op.create_index('ix_conversations_user_id_created_at', 'conversations', ['user_id', 'created_at'])

    op.create_table('research_tasks',
    sa.Column('id', _BIG_ID, autoincrement=True, nullable=False),
    sa.Column('user_id', _BIG_ID, nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('query', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_research_tasks_conversation_id_conversations')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_research_tasks_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_research_tasks'))
    )
    op.create_index(op.f('ix_research_tasks_conversation_id'), 'research_tasks', ['conversation_id'])
    op.create_index('ix_research_tasks_user_id_created_at', 'research_tasks', ['user_id', 'created_at'])

    op.create_table('run_attempts',
    sa.Column('id', _BIG_ID, autoincrement=True, nullable=False),
    sa.Column('task_id', _BIG_ID, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['research_tasks.id'], name=op.f('fk_run_attempts_task_id_research_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_run_attempts')),
    sa.UniqueConstraint('run_id', name=op.f('uq_run_attempts_run_id'))
    )
    op.create_index(op.f('ix_run_attempts_task_id'), 'run_attempts', ['task_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_run_attempts_task_id'), table_name='run_attempts')
    op.drop_table('run_attempts')
    op.drop_index('ix_research_tasks_user_id_created_at', table_name='research_tasks')
    op.drop_index(op.f('ix_research_tasks_conversation_id'), table_name='research_tasks')
    op.drop_table('research_tasks')
    op.drop_index('ix_conversations_user_id_created_at', table_name='conversations')
    op.drop_table('conversations')
