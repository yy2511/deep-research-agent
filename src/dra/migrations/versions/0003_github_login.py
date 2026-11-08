"""GitHub 登录（阶段 2 第 1 步，#45）：访客角色、外部登录身份。

- ``users.email``、``password_hash`` 允许为空：GitHub 登录的访客没有邮箱和密码。
- ``users`` 新增 ``display_name``、``avatar_url``；角色加 ``guest``。
- 新表 ``user_identities``：外部系统里不变的编号 → 本站用户。

用 ``batch_alter_table``：SQLite 改列的可空性和 CHECK 约束要整表重建，PostgreSQL 上是普通 ALTER。

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

_BIG_ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    op.create_table('user_identities',
    sa.Column('id', _BIG_ID, autoincrement=True, nullable=False),
    sa.Column('user_id', _BIG_ID, nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('subject', sa.String(length=64), nullable=False),
    sa.Column('login', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_identities_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_identities')),
    sa.UniqueConstraint('provider', 'subject', name='uq_user_identities_provider_subject')
    )
    op.create_index(op.f('ix_user_identities_user_id'), 'user_identities', ['user_id'], unique=False)

    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('display_name', sa.String(length=100), nullable=True))
        batch.add_column(sa.Column('avatar_url', sa.String(length=500), nullable=True))
        batch.alter_column('email', existing_type=sa.String(length=320), nullable=True)
        batch.alter_column('password_hash', existing_type=sa.String(length=255), nullable=True)
        batch.drop_constraint(op.f('ck_users_role'), type_='check')
        batch.create_check_constraint(op.f('ck_users_role'), "role IN ('guest', 'user', 'admin')")


def downgrade() -> None:
    # 回退前须先删除访客和没有邮箱密码的账号，否则 NOT NULL 与角色约束会失败。
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint(op.f('ck_users_role'), type_='check')
        batch.create_check_constraint(op.f('ck_users_role'), "role IN ('user', 'admin')")
        batch.alter_column('password_hash', existing_type=sa.String(length=255), nullable=False)
        batch.alter_column('email', existing_type=sa.String(length=320), nullable=False)
        batch.drop_column('avatar_url')
        batch.drop_column('display_name')
    op.drop_index(op.f('ix_user_identities_user_id'), table_name='user_identities')
    op.drop_table('user_identities')
