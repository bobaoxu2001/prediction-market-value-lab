"""Persist the equivalence verdict on each cross-platform match.

``rule_compatibility`` is a monotone internal level and cannot express the one
distinction that decides whether a matched pair may be called riskless: whether the
two venues were confirmed to handle cancellation and postponement the same way.
Storing the public verdict alongside it lets a rejected or demoted pair be read back
as rejected, instead of a stale ``identical`` surviving on the row.

Revision ID: e6b41c78d92f
Revises: d5a83c17e2b4
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6b41c78d92f"
down_revision = "d5a83c17e2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows have never had their void handling compared, so they default to
    # the verdict that licenses nothing. Failing closed is the point: a pair carried
    # over from before this check existed must not read as verified.
    op.add_column(
        "market_matches",
        sa.Column(
            "equivalence_verdict",
            sa.String(length=32),
            nullable=False,
            server_default="REJECTED",
        ),
    )
    op.add_column(
        "market_matches",
        sa.Column("equivalence_components", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_matches", "equivalence_components")
    op.drop_column("market_matches", "equivalence_verdict")
