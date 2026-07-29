"""Split the single fair probability into three declared classes.

`fair_probability_mean` pools every component, including one whose content is the
target market's own price. Published alone it made the model partly agree with
itself and called the residual an edge. The independent estimate, its band, and the
conservative figure eligibility is decided on are now stored separately.

Every column is nullable and nothing is backfilled. Rows written before the split
have no independent estimate on record, and inventing one - by copying the blended
mean, say - would fabricate exactly the provenance this change exists to establish.
A NULL here means "not computed under the split", which is the truth.

Revision ID: a1b2c3d4e5f6
Revises: 3d35414f0703
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import pmvl_shared.db.base

revision = "a1b2c3d4e5f6"
down_revision = "3d35414f0703"
branch_labels = None
depends_on = None

_MONEY_COLUMNS = (
    "market_informed_probability",
    "independent_probability",
    "independent_probability_low",
    "independent_probability_high",
    "conservative_decision_probability",
)


def upgrade() -> None:
    for name in _MONEY_COLUMNS:
        op.add_column(
            "model_predictions",
            sa.Column(name, pmvl_shared.db.base.Money(), nullable=True),
        )
    op.add_column(
        "model_predictions",
        sa.Column("independence", pmvl_shared.db.base.JSONColumn(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_predictions", "independence")
    for name in reversed(_MONEY_COLUMNS):
        op.drop_column("model_predictions", name)
