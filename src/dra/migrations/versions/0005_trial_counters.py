"""试用次数（阶段 2 第 3 步，#49）：daily_counters 表；run_attempts.quota_day。

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('daily_counters',
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('scope', 'day', 'kind', name=op.f('pk_daily_counters'))
    )
    with op.batch_alter_table('run_attempts') as batch:
        batch.add_column(sa.Column('quota_day', sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('run_attempts') as batch:
        batch.drop_column('quota_day')
    op.drop_table('daily_counters')
