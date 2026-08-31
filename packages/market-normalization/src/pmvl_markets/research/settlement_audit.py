"""Venue label self-consistency and independent audit agreement, kept apart.

These are two different questions and conflating them is how a real defect hid.

The venue's terminal result is the label. It is a fact about who was paid, the
venue is the only authority on it, and no weather bulletin may overwrite it.
The archived CLI maximum is *independent audit evidence* about the physical
quantity. When the two disagree, the useful output is a visible contradiction -
not a silently rewritten label, and not silence.

On 2026-08-29 KMIA the venue paid the 90-91F bucket while the archived audit
value read 85.0F, and nothing in the pipeline could say so: ``parity.classify``
compares only provenance (station, product, day definition) and never compares a
value against an outcome. The contradiction sat inside a CLEAN cohort member.

The second half of the same defect: NWS issued two FINAL bulletins for that
station-day, 46 minutes apart, reading 90.0F and 85.0F. ``build_cohort`` selects
with ``setdefault`` over ``ORDER BY available_at``, so the row that wins is the
one whose HTTP response landed first - 0.86s apart in that case. Neither
ordering rule is reliable here: the *later published* bulletin is the one the
venue contradicts, so "latest published" would have chosen 85.0 too. When
finals disagree, the honest output is that they disagree.

Everything here is pure. No database, no I/O, no clock. It computes nothing that
feeds a label, an admission rule, or a metric - it only describes evidence that
already exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Sequence

#: Physically defensible bounds for a US surface daily maximum, in Fahrenheit.
#: Only used to bound the feasibility search; a ladder never spans this range.
_MIN_F = -100
_MAX_F = 150


class VenueLabelStatus(str, Enum):
    """Whether the venue's own outcomes can all be true at once."""

    #: Some temperature satisfies every YES and no NO in the ladder.
    SELF_CONSISTENT = "VENUE_LABEL_SELF_CONSISTENT"
    #: No temperature can. The venue representation is impossible on its face,
    #: which is a defect in the label or in how the contract was parsed.
    SELF_INCONSISTENT = "VENUE_LABEL_SELF_INCONSISTENT"
    #: Nothing to check against - no contract carried usable bounds.
    UNVERIFIABLE = "VENUE_LABEL_UNVERIFIABLE"


class AuditStatus(str, Enum):
    """How independent audit evidence relates to the venue's implied value."""

    #: A final audit value falls inside the venue-implied range.
    MATCH = "AUDIT_MATCH"
    #: Audit evidence exists and contradicts the venue-implied range.
    CONFLICT = "AUDIT_CONFLICT"
    #: Several finals exist and agree with each other.
    DUPLICATE_FINAL_AGREED = "AUDIT_DUPLICATE_FINAL_AGREED"
    #: Several finals exist and disagree with each other. Distinct from
    #: CONFLICT: the audit source contradicts *itself*, so no single value can
    #: be selected without inventing a rule the evidence does not support.
    DUPLICATE_FINAL_CONFLICT = "AUDIT_DUPLICATE_FINAL_CONFLICT"
    #: No final audit observation.
    UNAVAILABLE = "AUDIT_UNAVAILABLE"


@dataclass(frozen=True)
class VenueContract:
    """One settled contract as the venue represents it."""

    ticker: str
    lower_inclusive: Decimal | None
    upper_inclusive: Decimal | None
    outcome_yes: bool

    def covers(self, value: Decimal) -> bool:
        if self.lower_inclusive is not None and value < self.lower_inclusive:
            return False
        if self.upper_inclusive is not None and value > self.upper_inclusive:
            return False
        return True


@dataclass(frozen=True)
class AuditObservation:
    """One archived FINAL bulletin reading."""

    value_f: Decimal
    product_code: str
    #: Kept for reporting only. Deliberately NOT used to pick a winner.
    published_at: str | None = None
    available_at: str | None = None
    source_product_id: str | None = None


@dataclass(frozen=True)
class SettlementParity:
    """The two verdicts, side by side, plus the evidence behind them."""

    venue_label_status: VenueLabelStatus
    audit_status: AuditStatus
    #: Every distinct final audit value, ascending. Never reduced to one when
    #: they disagree.
    audit_values: tuple[Decimal, ...]
    #: Populated only when the finals agree. ``None`` under
    #: DUPLICATE_FINAL_CONFLICT, because choosing would be fabrication.
    selected_audit_value: Decimal | None
    #: Inclusive integer range the venue's own outcomes imply, or None.
    venue_implied_low: Decimal | None
    venue_implied_high: Decimal | None
    reasons: tuple[str, ...]

    @property
    def is_contradiction(self) -> bool:
        """Anything a human must look at before this row is used."""
        return (
            self.venue_label_status is VenueLabelStatus.SELF_INCONSISTENT
            or self.audit_status
            in (AuditStatus.CONFLICT, AuditStatus.DUPLICATE_FINAL_CONFLICT)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "venue_label_status": self.venue_label_status.value,
            "audit_status": self.audit_status.value,
            "audit_values": [str(v) for v in self.audit_values],
            "selected_audit_value": (
                str(self.selected_audit_value)
                if self.selected_audit_value is not None
                else None
            ),
            "venue_implied_low": (
                str(self.venue_implied_low)
                if self.venue_implied_low is not None
                else None
            ),
            "venue_implied_high": (
                str(self.venue_implied_high)
                if self.venue_implied_high is not None
                else None
            ),
            "reasons": list(self.reasons),
        }


def venue_implied_range(
    contracts: "Sequence[VenueContract]",
) -> tuple[Decimal | None, Decimal | None]:
    """The inclusive integer range every venue outcome agrees on.

    A daily maximum is reported as a whole degree, so the search is over
    integers. Returns ``(None, None)`` when the outcomes are jointly impossible.
    """
    feasible = [
        Decimal(t)
        for t in range(_MIN_F, _MAX_F + 1)
        if all(c.covers(Decimal(t)) == c.outcome_yes for c in contracts)
    ]
    if not feasible:
        return None, None
    return feasible[0], feasible[-1]


def classify(
    *,
    contracts: "Sequence[VenueContract]",
    audit: "Sequence[AuditObservation]",
) -> SettlementParity:
    """Judge the venue against itself, then the audit against the venue.

    Order matters. The venue verdict is computed without reference to the audit
    evidence, so no bulletin can ever move it.
    """
    reasons: list[str] = []
    usable = [
        c
        for c in contracts
        if c.lower_inclusive is not None or c.upper_inclusive is not None
    ]

    if not usable:
        venue_status = VenueLabelStatus.UNVERIFIABLE
        low = high = None
        reasons.append("no contract carried bounds; venue outcomes not checkable")
    else:
        low, high = venue_implied_range(usable)
        if low is None:
            venue_status = VenueLabelStatus.SELF_INCONSISTENT
            reasons.append(
                "no temperature satisfies every venue outcome at once: "
                + ", ".join(
                    f"{c.ticker}[{c.lower_inclusive}..{c.upper_inclusive}]="
                    f"{'YES' if c.outcome_yes else 'NO'}"
                    for c in usable
                )
            )
        else:
            venue_status = VenueLabelStatus.SELF_CONSISTENT
            reasons.append(f"venue outcomes imply {low}..{high}F")

    values = sorted({a.value_f for a in audit})

    if not values:
        return SettlementParity(
            venue_status, AuditStatus.UNAVAILABLE, (), None, low, high,
            tuple(reasons + ["no final audit observation"]),
        )

    if len(values) > 1:
        reasons.append(
            "audit source reports "
            + " and ".join(f"{v}F" for v in values)
            + " as FINAL for the same station-day; no value selected"
        )
        # Whether they also contradict the venue is secondary: the audit source
        # disagreeing with itself is the finding, and selecting one of them by
        # fetch order is exactly the defect this module exists to expose.
        return SettlementParity(
            venue_status, AuditStatus.DUPLICATE_FINAL_CONFLICT,
            tuple(values), None, low, high, tuple(reasons),
        )

    value = values[0]
    if venue_status is not VenueLabelStatus.SELF_CONSISTENT:
        # There is no coherent venue range to compare against, so the audit
        # value is recorded rather than judged. Calling it MATCH would assert
        # an agreement with something that does not exist.
        reasons.append(
            f"single final audit value {value}F; no coherent venue range to "
            "compare against"
        )
        return SettlementParity(
            venue_status, AuditStatus.UNAVAILABLE, (value,), value, low, high,
            tuple(reasons),
        )

    if low is not None and high is not None and low <= value <= high:
        reasons.append(f"audit {value}F inside venue-implied {low}..{high}F")
        status = AuditStatus.MATCH
    else:
        reasons.append(
            f"audit {value}F outside venue-implied {low}..{high}F; "
            "venue label stands, audit evidence disputed"
        )
        status = AuditStatus.CONFLICT

    if len(audit) > 1 and status is AuditStatus.MATCH:
        reasons.append(f"{len(audit)} finals, all reading {value}F")
        status = AuditStatus.DUPLICATE_FINAL_AGREED

    return SettlementParity(
        venue_status, status, (value,), value, low, high, tuple(reasons)
    )
