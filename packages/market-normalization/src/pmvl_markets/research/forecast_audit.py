"""Auditing every archived forecast against its own immutable raw payload.

The target-day defect stored tomorrow's forecast under today's validity window.
It is tempting to identify affected rows by commit time - "anything before
f6c821b" - but that is a proxy for the thing that actually matters and it is
wrong in both directions: a row written before the fix may be correct by
coincidence when the market's weather day happened to be the resolution day
minus nothing, and a row written after could still be wrong for an unrelated
reason.

Every row carries a ``raw_payload_ref`` to the gridpoint document it came from,
and that document is immutable and content-addressed. So the check is direct:
re-read the raw payload, extract the maximum for the date the row *claims* to
describe, and compare. Agreement is evidence, not assumption.

This also settles the recovery question. The raw payload holds eight days of
forecast values, so if it already contained the correct target date at the
original retrieval time, a corrected observation can be re-derived from it
without asking the provider anything today. Re-querying the provider now would
return a forecast that did not exist then, which is feature backfill and is
forbidden - the distinction this module exists to keep sharp.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping


class RecoveryClass(str, Enum):
    """What can be done with one archived forecast observation."""

    #: Stored value already matches its own raw payload for the claimed date.
    NOT_AFFECTED = "NOT_AFFECTED"
    #: Stored value disagrees, but the raw payload holds the correct value for
    #: the claimed date - so a corrected observation can be derived from data
    #: that genuinely existed at the original retrieval time.
    RECOVERABLE_FROM_IMMUTABLE_RAW = "RECOVERABLE_FROM_IMMUTABLE_RAW"
    #: The raw payload is gone or unreadable. Nothing can be re-derived, and
    #: asking the provider today would be feature backfill.
    UNRECOVERABLE_RAW_MISSING = "UNRECOVERABLE_RAW_MISSING"
    #: The raw payload survives but never contained the claimed date, so the
    #: correct value was not knowable at the original retrieval time.
    UNRECOVERABLE_RAW_INSUFFICIENT = "UNRECOVERABLE_RAW_INSUFFICIENT"


class ValidityStatus(str, Enum):
    """Durable, machine-checkable research eligibility."""

    VALID = "VALID"
    DEFECT_TARGET_DAY_SHIFT = "DEFECT_TARGET_DAY_SHIFT"
    SUPERSEDED_BY_RAW_REDERIVATION = "SUPERSEDED_BY_RAW_REDERIVATION"
    #: Eligibility could not be established. Fails closed: an unknown row is
    #: excluded, because "we could not check" is not "it is fine".
    UNKNOWN = "UNKNOWN"


#: The only statuses a research cohort may draw from.
RESEARCH_ELIGIBLE = frozenset({ValidityStatus.VALID})

DERIVATION_LOGIC_VERSION = "raw-rederivation-v1.0.0"

#: Values agreeing to within this are the same reading. The store keeps four
#: decimals after a Celsius-to-Fahrenheit conversion, so exact equality would
#: flag float noise as a defect.
_TOLERANCE_F = Decimal("0.01")


def _celsius_to_f(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def max_temperatures_by_day(raw: bytes | str) -> dict[str, Decimal]:
    """Every daily maximum in a raw gridpoint payload, keyed by local date.

    Returns an empty mapping rather than raising: an unreadable payload is an
    unrecoverable observation, which the caller classifies, not an exception the
    audit should die on.
    """
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    properties = document.get("properties", document)
    block = properties.get("maxTemperature") or {}
    celsius = "degc" in str(block.get("uom", "")).lower()
    out: dict[str, Decimal] = {}
    for entry in block.get("values") or []:
        valid = str(entry.get("validTime", ""))
        raw_value = entry.get("value")
        if not valid or raw_value is None:
            continue
        try:
            reading = float(raw_value)
        except (TypeError, ValueError):
            continue
        day = valid.split("/")[0][:10]
        fahrenheit = _celsius_to_f(reading) if celsius else reading
        out[day] = Decimal(f"{fahrenheit:.4f}")
    return out


@dataclass(frozen=True)
class ForecastVerdict:
    """One observation, checked against the payload it came from."""

    observation_id: int
    claimed_date: str
    stored_value: Decimal | None
    raw_value: Decimal | None
    recovery: RecoveryClass
    validity: ValidityStatus
    raw_payload_ref: str = ""

    @property
    def recoverable(self) -> bool:
        return self.recovery is RecoveryClass.RECOVERABLE_FROM_IMMUTABLE_RAW

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "claimed_date": self.claimed_date,
            "stored_value": str(self.stored_value) if self.stored_value is not None else None,
            "raw_value": str(self.raw_value) if self.raw_value is not None else None,
            "recovery": self.recovery.value,
            "validity": self.validity.value,
            "raw_payload_ref": self.raw_payload_ref,
        }


def classify_observation(
    *,
    observation_id: int,
    claimed_date: str | date | None,
    stored_value: Decimal | float | str | None,
    raw_by_day: Mapping[str, Decimal] | None,
    raw_payload_ref: str = "",
) -> ForecastVerdict:
    """Compare one stored observation to its own immutable payload."""
    day = (
        claimed_date.isoformat()
        if isinstance(claimed_date, date)
        else (str(claimed_date)[:10] if claimed_date else "")
    )

    def verdict(recovery: RecoveryClass, validity: ValidityStatus, raw: Decimal | None):
        return ForecastVerdict(
            observation_id=observation_id,
            claimed_date=day,
            stored_value=(
                Decimal(str(stored_value)) if stored_value not in (None, "") else None
            ),
            raw_value=raw,
            recovery=recovery,
            validity=validity,
            raw_payload_ref=raw_payload_ref,
        )

    if not day or stored_value in (None, ""):
        # Cannot be checked, so cannot be trusted.
        return verdict(RecoveryClass.UNRECOVERABLE_RAW_INSUFFICIENT,
                       ValidityStatus.UNKNOWN, None)
    if not raw_by_day:
        return verdict(RecoveryClass.UNRECOVERABLE_RAW_MISSING,
                       ValidityStatus.UNKNOWN, None)

    raw_value = raw_by_day.get(day)
    if raw_value is None:
        # The payload never described this day, so the correct value was not
        # knowable then. Re-deriving it would require asking the provider today.
        return verdict(RecoveryClass.UNRECOVERABLE_RAW_INSUFFICIENT,
                       ValidityStatus.DEFECT_TARGET_DAY_SHIFT, None)

    stored = Decimal(str(stored_value))
    if abs(stored - raw_value) <= _TOLERANCE_F:
        return verdict(RecoveryClass.NOT_AFFECTED, ValidityStatus.VALID, raw_value)
    return verdict(RecoveryClass.RECOVERABLE_FROM_IMMUTABLE_RAW,
                   ValidityStatus.DEFECT_TARGET_DAY_SHIFT, raw_value)


@dataclass
class AuditReport:
    verdicts: list[ForecastVerdict] = field(default_factory=list)
    payloads_read: int = 0
    payloads_unreadable: int = 0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.verdicts:
            out[v.recovery.value] = out.get(v.recovery.value, 0) + 1
        return dict(sorted(out.items()))

    def validity_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.verdicts:
            out[v.validity.value] = out.get(v.validity.value, 0) + 1
        return dict(sorted(out.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "observations_audited": len(self.verdicts),
            "payloads_read": self.payloads_read,
            "payloads_unreadable": self.payloads_unreadable,
            "recovery": self.counts(),
            "validity": self.validity_counts(),
        }
