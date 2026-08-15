"""Record a market's rules, appending a version only when the wording changes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.timeutil import utcnow

from ..db_models import MarketRuleVersion
from .rule_completeness import classify_completeness, diff_rules, rule_hash

#: Bumped whenever the extraction logic changes in a way that could produce
#: different normalized terms from identical input. Stored per version so an old
#: verdict can be attributed to the parser that produced it.
PARSER_VERSION = "1.0.0"


def _payload_hash(payload: Any) -> str:
    try:
        text = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def record_rule_version(
    session: Session,
    *,
    market_id: int,
    raw_title: str = "",
    raw_subtitle: str = "",
    raw_rules: str = "",
    raw_resolution_source: str = "",
    raw_cancellation_language: str = "",
    raw_postponement_language: str = "",
    platform_metadata: dict | None = None,
    source_endpoint: str = "",
    source_payload: Any = None,
    normalized_terms: dict | None = None,
    extraction_confidence: Any = None,
) -> MarketRuleVersion:
    """Append a version, or extend the current one if nothing changed.

    Returns the row representing the current wording either way. Re-ingesting
    identical rules must not accumulate duplicates - the pipeline sees the same
    market every ten minutes, and a row per sighting would bury the handful of
    real rewrites in tens of thousands of identical records.
    """
    from decimal import Decimal

    now = utcnow()
    digest = rule_hash(raw_rules)

    existing = session.scalar(
        select(MarketRuleVersion)
        .where(
            MarketRuleVersion.market_id == market_id,
            MarketRuleVersion.rule_hash == digest,
        )
        .limit(1)
    )
    if existing is not None:
        # Same wording seen again. last_observed_at is the only mutable column on
        # this table; overwriting anything else would edit history.
        existing.last_observed_at = now
        session.flush()
        return existing

    previous = session.scalar(
        select(MarketRuleVersion)
        .where(MarketRuleVersion.market_id == market_id)
        .order_by(MarketRuleVersion.version.desc())
        .limit(1)
    )

    current_fields = {
        "raw_rules": raw_rules,
        "settlement_source": raw_resolution_source,
        "raw_title": raw_title,
        "raw_subtitle": raw_subtitle,
        "cancellation": raw_cancellation_language,
        "postponement": raw_postponement_language,
    }
    changed: list[str] = []
    if previous is not None:
        changed = list(
            diff_rules(
                {
                    "raw_rules": previous.raw_rules,
                    "settlement_source": previous.raw_resolution_source,
                    "raw_title": previous.raw_title,
                    "raw_subtitle": previous.raw_subtitle,
                    "cancellation": previous.raw_cancellation_language,
                    "postponement": previous.raw_postponement_language,
                },
                current_fields,
            ).changed_fields
        )

    row = MarketRuleVersion(
        market_id=market_id,
        version=(previous.version + 1) if previous else 1,
        raw_title=raw_title or "",
        raw_subtitle=raw_subtitle or "",
        raw_rules=raw_rules or "",
        raw_resolution_source=raw_resolution_source or "",
        raw_cancellation_language=raw_cancellation_language or "",
        raw_postponement_language=raw_postponement_language or "",
        platform_metadata=platform_metadata,
        fetched_at=now,
        source_endpoint=source_endpoint or "",
        source_payload_hash=_payload_hash(source_payload) if source_payload else "",
        parser_version=PARSER_VERSION,
        normalized_terms=normalized_terms,
        normalized_rule_hash=_payload_hash(normalized_terms) if normalized_terms else "",
        extraction_confidence=Decimal(str(extraction_confidence or 0)),
        completeness=classify_completeness(
            raw_rules=raw_rules,
            settlement_source=raw_resolution_source,
            normalized_terms=normalized_terms,
        ).value,
        rule_hash=digest,
        changed_fields=changed or None,
        first_observed_at=now,
        last_observed_at=now,
    )
    from pmvl_shared.db.base import insert_or_skip

    inserted = insert_or_skip(
        session,
        MarketRuleVersion.__table__,
        {
            "market_id": market_id,
            "version": row.version,
            "raw_title": row.raw_title,
            "raw_subtitle": row.raw_subtitle,
            "raw_rules": row.raw_rules,
            "raw_resolution_source": row.raw_resolution_source,
            "raw_cancellation_language": row.raw_cancellation_language,
            "raw_postponement_language": row.raw_postponement_language,
            "platform_metadata": row.platform_metadata,
            "fetched_at": row.fetched_at,
            "source_endpoint": row.source_endpoint,
            "source_payload_hash": row.source_payload_hash,
            "parser_version": row.parser_version,
            "normalized_terms": row.normalized_terms,
            "normalized_rule_hash": row.normalized_rule_hash,
            "extraction_confidence": row.extraction_confidence,
            "completeness": row.completeness,
            "rule_hash": row.rule_hash,
            "changed_fields": row.changed_fields,
            "first_observed_at": row.first_observed_at,
            "last_observed_at": row.last_observed_at,
        },
        conflict_cols=["market_id", "rule_hash"],
    )
    if inserted:
        # The Core insert does not backfill the ORM instance, so reload the
        # persisted row: callers rely on its identity (id, timestamps).
        persisted = session.scalar(
            select(MarketRuleVersion)
            .where(
                MarketRuleVersion.market_id == market_id,
                MarketRuleVersion.rule_hash == digest,
            )
            .limit(1)
        )
        if persisted is None:  # pragma: no cover - the insert just targeted this key
            raise RuntimeError(
                f"insert for market_rule_versions ({market_id}, {digest}) "
                "reported success but the row cannot be reloaded"
            )
        return persisted
    # Two concurrent ingests both missed the SELECT above; the unique
    # (market_id, rule_hash) index proves this exact wording exists. Reload it
    # and extend its observation window instead of failing the ingest.
    existing = session.scalar(
        select(MarketRuleVersion)
        .where(
            MarketRuleVersion.market_id == market_id,
            MarketRuleVersion.rule_hash == digest,
        )
        .limit(1)
    )
    if existing is None:  # pragma: no cover - the insert just targeted this key
        raise RuntimeError(
            f"insert for market_rule_versions ({market_id}, {digest}) "
            "reported success but the row cannot be reloaded"
        )
    existing.last_observed_at = now
    session.flush()
    return existing


def current_rules(session: Session, market_id: int) -> MarketRuleVersion | None:
    return session.scalar(
        select(MarketRuleVersion)
        .where(MarketRuleVersion.market_id == market_id)
        .order_by(MarketRuleVersion.version.desc())
        .limit(1)
    )


def rule_history(session: Session, market_id: int) -> list[MarketRuleVersion]:
    return list(
        session.scalars(
            select(MarketRuleVersion)
            .where(MarketRuleVersion.market_id == market_id)
            .order_by(MarketRuleVersion.version)
        )
    )
