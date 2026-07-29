"""Preserve every distinct wording of a market's settlement rules.

Rules lived in one mutable column, so a venue editing its resolution criteria
silently overwrote the text every stored verdict had been derived from. Afterwards
nobody could tell whether a match was verified against the current wording or an
older one.

Additive only. Nothing is backfilled: the existing column holds whichever wording
happened to be current at the last ingest, and inserting it as "version 1 observed
at some unknown time" would fabricate a history. The first real version appears
the next time each market is ingested.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import pmvl_shared.db.base

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_rule_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("raw_title", sa.Text(), nullable=False),
        sa.Column("raw_subtitle", sa.Text(), nullable=False),
        sa.Column("raw_rules", sa.Text(), nullable=False),
        sa.Column("raw_resolution_source", sa.Text(), nullable=False),
        sa.Column("raw_cancellation_language", sa.Text(), nullable=False),
        sa.Column("raw_postponement_language", sa.Text(), nullable=False),
        sa.Column("platform_metadata", pmvl_shared.db.base.JSONColumn(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("source_endpoint", sa.Text(), nullable=False),
        sa.Column("source_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.Column("normalized_terms", pmvl_shared.db.base.JSONColumn(), nullable=True),
        sa.Column("normalized_rule_hash", sa.String(length=64), nullable=False),
        sa.Column("extraction_confidence", pmvl_shared.db.base.Money(), nullable=False),
        sa.Column("completeness", sa.String(length=16), nullable=False),
        sa.Column("rule_hash", sa.String(length=64), nullable=False),
        sa.Column("changed_fields", pmvl_shared.db.base.JSONColumn(), nullable=True),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_rule_versions_market_id", "market_rule_versions", ["market_id"]
    )
    op.create_index(
        "ix_market_rule_versions_rule_hash", "market_rule_versions", ["rule_hash"]
    )
    # Unique per (market, wording): re-ingesting unchanged rules must extend
    # last_observed_at, not append a duplicate row.
    op.create_index(
        "ix_rulever_market_hash",
        "market_rule_versions",
        ["market_id", "rule_hash"],
        unique=True,
    )
    op.create_index(
        "ix_rulever_market_seen",
        "market_rule_versions",
        ["market_id", "first_observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rulever_market_seen", table_name="market_rule_versions")
    op.drop_index("ix_rulever_market_hash", table_name="market_rule_versions")
    op.drop_index("ix_market_rule_versions_rule_hash", table_name="market_rule_versions")
    op.drop_index("ix_market_rule_versions_market_id", table_name="market_rule_versions")
    op.drop_table("market_rule_versions")
