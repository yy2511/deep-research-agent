"""建 showcases 表：公开只读的研究示例（阶段 2 第 2 步，#47）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('showcases',
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_showcases_conversation_id_conversations')),
    sa.PrimaryKeyConstraint('conversation_id', name=op.f('pk_showcases'))
    )


def downgrade() -> None:
    op.drop_table('showcases')
