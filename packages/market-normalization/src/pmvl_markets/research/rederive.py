"""Classifying archived forecasts and re-deriving the recoverable ones.

Two operations, deliberately separate.

**Classification** writes only metadata - ``validity_status``,
``research_eligible``, ``defect_class``. Every original observed value is left
exactly as recorded, because a defective row is evidence of what the system
believed at that instant and editing it destroys the only record of the bug.

**Re-derivation** appends a NEW row carrying the correct value for the date it
claims, with lineage back to the raw payload's digest and to the row it
supersedes. This is not feature backfill: the value comes from a document that
was already in hand at the original availability instant, so it was genuinely
knowable then. Asking the provider for it today would be backfill and is
exactly what this module exists to avoid.

The re-derived row inherits the ORIGINAL ``available_at``. That is the whole
point - it records when the information became available to us, not when we got
around to reading it correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.db.models import SourceObservationRow

from .forecast_audit import (
    DERIVATION_LOGIC_VERSION,
    ValidityStatus,
    classify_observation,
    max_temperatures_by_day,
)


#: Corrections carry their own kind so they cannot collide with the poll they
#: correct, and so a query can find them without knowing about a flag column.
REDERIVED_KIND = "source_point_value_rederived"


@dataclass
class RederiveReport:
    audited: int = 0
    marked_valid: int = 0
    marked_defective: int = 0
    marked_unknown: int = 0
    rederived: int = 0
    unrecoverable: int = 0
    already_rederived: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "audited": self.audited,
            "marked_valid": self.marked_valid,
            "marked_defective": self.marked_defective,
            "marked_unknown": self.marked_unknown,
            "rederived": self.rederived,
            "unrecoverable": self.unrecoverable,
            "already_rederived": self.already_rederived,
            "errors": self.errors[:10],
        }


def classify_and_rederive(
    session: Session, store: Any, *, dry_run: bool = False
) -> RederiveReport:
    """Audit every forecast row, then append corrections for what can be fixed."""
    report = RederiveReport()
    payload_cache: dict[str, dict[str, Decimal]] = {}

    rows = list(
        session.scalars(
            select(SourceObservationRow)
            .where(SourceObservationRow.rederived_from_raw.is_(False))
            .order_by(SourceObservationRow.id)
        )
    )
    # A correction already appended for an original must not be duplicated on a
    # second run: this job has to be safe to re-run on a schedule.
    corrected = {
        row_id
        for (row_id,) in session.execute(
            select(SourceObservationRow.original_observation_id).where(
                SourceObservationRow.rederived_from_raw.is_(True)
            )
        )
        if row_id is not None
    }

    for row in rows:
        ref = row.raw_payload_ref or ""
        if ref not in payload_cache:
            try:
                payload_cache[ref] = max_temperatures_by_day(store.get(ref))
            except Exception as exc:  # noqa: BLE001
                payload_cache[ref] = {}
                report.errors.append(f"payload {ref[:24]}: {type(exc).__name__}: {exc}")

        claimed = row.forecast_valid_start
        verdict = classify_observation(
            observation_id=row.id,
            claimed_date=claimed.date() if claimed is not None else None,
            stored_value=row.value,
            raw_by_day=payload_cache[ref],
            raw_payload_ref=ref,
        )
        report.audited += 1

        if dry_run:
            continue

        row.validity_status = verdict.validity.value
        row.defect_class = (
            "" if verdict.validity is ValidityStatus.VALID else verdict.validity.value
        )
        row.research_eligible = verdict.validity is ValidityStatus.VALID

        if verdict.validity is ValidityStatus.VALID:
            report.marked_valid += 1
            continue
        if verdict.validity is ValidityStatus.UNKNOWN:
            report.marked_unknown += 1
            continue

        report.marked_defective += 1
        if not verdict.recoverable or verdict.raw_value is None:
            report.unrecoverable += 1
            continue
        if row.id in corrected:
            report.already_rederived += 1
            continue

        # The original is superseded, not deleted. Its recorded value stays.
        row.validity_status = ValidityStatus.SUPERSEDED_BY_RAW_REDERIVATION.value
        session.add(
            SourceObservationRow(
                market_id=row.market_id,
                source_id=row.source_id,
                source_product=row.source_product,
                # A distinct kind, not the original's.
                #
                # The unique constraint on (market, source, kind, available_at)
                # exists to stop one poll being recorded twice, and a correction
                # deliberately inherits available_at - so it collides. Widening
                # the constraint would remove a real protection; a separate kind
                # keeps it intact and makes corrections explicitly identifiable
                # without relying on a boolean nobody remembers to filter on.
                kind=REDERIVED_KIND,
                value=verdict.raw_value,
                event_time=row.event_time,
                observed_at=row.observed_at,
                # Inherited, not restamped. This records when the information
                # became available to us, not when we re-read it correctly.
                available_at=row.available_at,
                ingested_at=row.ingested_at,
                published_at=row.published_at,
                source_generated_at=row.source_generated_at,
                poll_started_at=row.poll_started_at,
                poll_completed_at=row.poll_completed_at,
                forecast_valid_start=row.forecast_valid_start,
                forecast_valid_end=row.forecast_valid_end,
                source_endpoint=row.source_endpoint,
                source_location=row.source_location,
                settlement_station_id=row.settlement_station_id,
                source_payload_hash=row.source_payload_hash,
                raw_payload_ref=ref,
                parser_version=row.parser_version,
                collector_version=row.collector_version,
                provenance=row.provenance,
                validity_status=ValidityStatus.VALID.value,
                research_eligible=True,
                defect_class="",
                rederived_from_raw=True,
                source_raw_sha256=ref.split(":")[-1],
                original_observation_id=row.id,
                derivation_logic_version=DERIVATION_LOGIC_VERSION,
            )
        )
        report.rederived += 1

    return report
