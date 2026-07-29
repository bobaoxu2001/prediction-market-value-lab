"""Freeze every input a published recommendation rested on.

The snapshot stored the market-informed probability alone, so a backtest reading
it back could not tell whether an independent estimate existed when the call was
made - and that answer changes as models are added, which means it cannot be
reconstructed from current data either.

Also records the parser version and rule version in force at publication. Without
them a re-grade cannot tell whether a later parser change altered the contract's
meaning underneath a historical recommendation.

Additive and nullable. Rows published before the split have no independent
estimate on record, and copying the blended figure into that column would
fabricate the very provenance this migration exists to preserve.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import pmvl_shared.db.base

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

_MONEY = (
    "independent_probability_at_publication",
    "market_informed_probability_at_publication",
    "conservative_probability_at_publication",
)


def upgrade() -> None:
    for name in _MONEY:
        op.add_column(
            "recommendation_snapshots",
            sa.Column(name, pmvl_shared.db.base.Money(), nullable=True),
        )
    op.add_column(
        "recommendation_snapshots",
        sa.Column("parser_version", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "recommendation_snapshots", sa.Column("rule_version_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "recommendation_snapshots",
        sa.Column("input_freshness", pmvl_shared.db.base.JSONColumn(), nullable=True),
    )
    op.add_column(
        "recommendation_snapshots",
        sa.Column("input_data_cutoff", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    for name in (
        "input_data_cutoff",
        "input_freshness",
        "rule_version_id",
        "parser_version",
        *reversed(_MONEY),
    ):
        op.drop_column("recommendation_snapshots", name)
