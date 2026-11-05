"""Durable per-observation validity, and lineage for raw re-derivation.

The target-day defect stored tomorrow's forecast under today's validity window.
Roughly half of the archived rows are affected, and the affected half cannot be
identified by commit time: the same job wrote both, and whether a given row
happens to be correct depends on the market's weather day. Eligibility therefore
has to live on the row and be established by checking it against the immutable
payload it came from.

Nothing is rewritten. The defective rows keep their original observed values -
they are evidence of what the system recorded - and are marked ineligible. A
correction is a NEW row carrying lineage back to the raw payload and to the row
it supersedes.

``research_eligible`` defaults to 0. A row whose eligibility has never been
established must be excluded rather than assumed fine, so the column fails
closed for anything written before or outside the audit.

Revision ID: d5a83c17e2b4
Revises: c7d2e94b1f30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd5a83c17e2b4'
down_revision: Union[str, None] = 'c7d2e94b1f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("source_observations") as batch:
        batch.add_column(
            sa.Column(
                "validity_status", sa.String(length=40),
                nullable=False, server_default="UNKNOWN",
            )
        )
        # Fails closed. An unaudited row is not a valid row.
        batch.add_column(
            sa.Column(
                "research_eligible", sa.Boolean(),
                nullable=False, server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("defect_class", sa.String(length=64),
                                   nullable=False, server_default=""))
        # --- lineage for a re-derived correction -------------------------
        batch.add_column(sa.Column("rederived_from_raw", sa.Boolean(),
                                   nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("source_raw_sha256", sa.String(length=80),
                                   nullable=False, server_default=""))
        batch.add_column(sa.Column("original_observation_id", sa.Integer(),
                                   nullable=True))
        batch.add_column(sa.Column("derivation_logic_version", sa.String(length=40),
                                   nullable=False, server_default=""))

    op.create_index(
        "ix_sourceobs_eligible",
        "source_observations",
        ["research_eligible", "settlement_station_id", "forecast_valid_start"],
    )
    op.create_index(
        "ix_sourceobs_original", "source_observations", ["original_observation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_sourceobs_original", table_name="source_observations")
    op.drop_index("ix_sourceobs_eligible", table_name="source_observations")
    with op.batch_alter_table("source_observations") as batch:
        for column in (
            "derivation_logic_version", "original_observation_id",
            "source_raw_sha256", "rederived_from_raw", "defect_class",
            "research_eligible", "validity_status",
        ):
            batch.drop_column(column)
