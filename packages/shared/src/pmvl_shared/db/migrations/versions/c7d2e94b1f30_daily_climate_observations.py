"""Archive the daily CLI maximum these contracts actually settle on.

Phase 4C0 established that the repository held hourly NWS station observations
and zero daily summaries, while every live weather contract settles on the daily
maximum published in the NWS Climatological Report. The settled quantity had
therefore never been observed, and every contract classified as
SETTLEMENT_MISMATCH.

Nothing about that is fixable by better modelling. The label itself was absent.

Revision ID: c7d2e94b1f30
Revises: 8c41f0d7ab29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7d2e94b1f30'
down_revision: Union[str, None] = '8c41f0d7ab29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_climate_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("station_id", sa.String(length=16), nullable=False),
        sa.Column("product_code", sa.String(length=16), nullable=False),
        sa.Column("observation_date", sa.String(length=10), nullable=False),
        sa.Column("daily_max_f", sa.Float(), nullable=True),
        sa.Column("daily_min_f", sa.Float(), nullable=True),
        sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("valid_as_of", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_backfill", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_product_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source_endpoint", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("raw_payload_ref", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("collector_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("parser_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("provenance", sa.String(length=16), nullable=False, server_default="live"),
        sa.PrimaryKeyConstraint("id"),
        # A re-poll of an unchanged bulletin must not create a second row, and a
        # genuine revision must not replace the first - so identity includes the
        # payload hash and the availability instant, never just the day.
        sa.UniqueConstraint(
            "station_id", "observation_date", "raw_payload_hash", "available_at",
            name="uq_daily_climate_identity",
        ),
    )
    op.create_index(
        "ix_dailyclimate_station_date",
        "daily_climate_observations",
        ["station_id", "observation_date"],
    )
    op.create_index(
        "ix_dailyclimate_available", "daily_climate_observations", ["available_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_dailyclimate_available", table_name="daily_climate_observations")
    op.drop_index("ix_dailyclimate_station_date", table_name="daily_climate_observations")
    op.drop_table("daily_climate_observations")
