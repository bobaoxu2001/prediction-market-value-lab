"""jobrun idempotency key + market rule uniqueness

Revision ID: f1a2b3c4d5e6
Revises: 3d35414f0703
Create Date: 2026-08-15 22:00:00.000000

Two database-level idempotency guarantees the upsert paths previously only
promised in code:

* job_runs.idempotency_key persists the RunRecord identity and a partial
  unique index allows at most one RUNNING row per key, so a scheduler and a
  manual CLI cannot start "the same work" twice concurrently.
* market_rules gets a unique constraint on market_id so concurrent ingests
  converge on one rule row instead of accumulating duplicates.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import pmvl_shared.db.base  # noqa: F401  - column types referenced by path

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('idempotency_key', sa.String(length=32), nullable=True))
        batch_op.create_index('ix_jobrun_idempotency_key', ['idempotency_key'])

    op.create_index(
        'uq_jobrun_active_key',
        'job_runs',
        ['idempotency_key'],
        unique=True,
        sqlite_where=sa.text("status = 'running' AND idempotency_key IS NOT NULL"),
        postgresql_where=sa.text("status = 'running' AND idempotency_key IS NOT NULL"),
    )

    with op.batch_alter_table('market_rules', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_marketrule_market', ['market_id'])


def downgrade() -> None:
    with op.batch_alter_table('market_rules', schema=None) as batch_op:
        batch_op.drop_constraint('uq_marketrule_market', type_='unique')

    op.drop_index('uq_jobrun_active_key', table_name='job_runs')

    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_jobrun_idempotency_key')
        batch_op.drop_column('idempotency_key')
