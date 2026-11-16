"""四种身份、负责人治理、个人额度与审计。

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None
B = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')
D = sa.DateTime(timezone=True)


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        connection = op.get_bind()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql('BEGIN')
        op.execute(sa.text('PRAGMA defer_foreign_keys=ON'))
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('management_version', sa.Integer(), server_default='0', nullable=False))
        batch.drop_constraint(op.f('ck_users_role'), type_='check')
        batch.create_check_constraint(op.f('ck_users_role'), "role IN ('guest','user','admin','owner')")
        batch.create_check_constraint(op.f('ck_users_owner_active'), "role != 'owner' OR status = 'active'")
        batch.create_check_constraint(op.f('ck_users_management_version'), 'management_version >= 0')
    op.create_index('uq_users_one_owner', 'users', ['role'], unique=True,
                    sqlite_where=sa.text("role = 'owner'"), postgresql_where=sa.text("role = 'owner'"))
    with op.batch_alter_table('daily_counters') as batch:
        batch.alter_column('count', existing_type=sa.Integer(), type_=sa.BigInteger(), nullable=False)
    op.create_table('platform_governance',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('owner_user_id', B, sa.ForeignKey('users.id')),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('quota_timezone', sa.String(64)),
        sa.Column('pending_timezone', sa.String(64)),
        sa.Column('timezone_effective_at', D),
        sa.Column('tracking_started_at', D, nullable=False),
        sa.CheckConstraint('id = 1', name=op.f('ck_platform_governance_singleton')))
    op.create_table('user_quota_policies',
        sa.Column('user_id', B, sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('daily_runs_limit', sa.BigInteger()),
        sa.Column('daily_briefs_limit', sa.BigInteger()),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_by', B, sa.ForeignKey('users.id')),
        sa.Column('updated_at', D, nullable=False),
        sa.CheckConstraint("mode IN ('limited','unlimited')", name=op.f('ck_user_quota_policies_mode')),
        sa.CheckConstraint("(mode = 'unlimited' AND daily_runs_limit IS NULL AND daily_briefs_limit IS NULL) OR "
                           "(mode = 'limited' AND daily_runs_limit IS NOT NULL AND daily_briefs_limit IS NOT NULL "
                           "AND daily_runs_limit >= 0 AND daily_briefs_limit >= 0)",
                           name=op.f('ck_user_quota_policies_limits')),
        sa.CheckConstraint('version >= 0', name=op.f('ck_user_quota_policies_version')))
    op.create_table('quota_reservations',
        sa.Column('id', sa.String(128), primary_key=True),
        sa.Column('user_id', B, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('quota_day', sa.String(10), nullable=False),
        sa.Column('scopes_json', sa.Text(), nullable=False),
        sa.Column('policy_version', sa.Integer()),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('created_at', D, nullable=False),
        sa.CheckConstraint("kind IN ('run','brief')", name=op.f('ck_quota_reservations_kind')),
        sa.CheckConstraint("state IN ('reserved','started','refunded')", name=op.f('ck_quota_reservations_state')))
    op.create_index('ix_quota_reservations_user_id', 'quota_reservations', ['user_id'])
    op.create_table('management_audits',
        sa.Column('id', B, primary_key=True, autoincrement=True),
        sa.Column('actor_id', B, sa.ForeignKey('users.id')),
        sa.Column('target_id', B, sa.ForeignKey('users.id')),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('before_json', sa.Text(), nullable=False),
        sa.Column('after_json', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_at', D, nullable=False))
    op.create_index('ix_management_audits_actor_id', 'management_audits', ['actor_id'])
    op.create_index('ix_management_audits_target_id', 'management_audits', ['target_id'])
    op.create_table('owner_transfer_proofs',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('session_id', B, sa.ForeignKey('sessions.id'), nullable=False),
        sa.Column('actor_id', B, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('target_id', B, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('governance_version', sa.Integer(), nullable=False),
        sa.Column('actor_version', sa.Integer(), nullable=False),
        sa.Column('target_version', sa.Integer(), nullable=False),
        sa.Column('expires_at', D, nullable=False),
        sa.Column('verified_at', D),
        sa.Column('consumed_at', D),
        sa.Column('provider', sa.String(32), nullable=False))
    now = datetime.now(UTC)
    gov = sa.table('platform_governance', sa.column('id'), sa.column('version'), sa.column('tracking_started_at'))
    op.bulk_insert(gov, [{'id': 1, 'version': 0, 'tracking_started_at': now}])
    op.execute(sa.text("INSERT INTO user_quota_policies (user_id, mode, version, updated_at) "
                       "SELECT id, 'unlimited', 0, CURRENT_TIMESTAMP FROM users WHERE role = 'user'"))
    if op.get_bind().dialect.name == 'sqlite':
        # SQLite remembers deferred violations from the temporary DROP performed by
        # Alembic batch recreation even after the replacement table is valid. Check
        # the completed schema/data explicitly before clearing that stale counter.
        connection = op.get_bind()
        if connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('用户治理迁移后外键校验失败，迁移已中止')
        connection.exec_driver_sql('PRAGMA defer_foreign_keys=OFF')


def downgrade():
    # Destructive policy/governance rollback needs an explicit backup/reconciliation; never silently drop it.
    raise RuntimeError('用户治理迁移不能自动回退；请先备份并人工核对负责人、额度与审计数据')
