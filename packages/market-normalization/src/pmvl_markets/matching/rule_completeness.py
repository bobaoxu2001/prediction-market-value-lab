"""How much of a contract's settlement terms we actually kept.

A normalized term with no preserved source is an assertion nobody can check. The
committed snapshot has normalized terms for all 1,850 markets and raw rules text
for 462 - every one of them synthetic demo data. Its cross-platform verdicts,
including the "109 pairs examined, 0 equivalent" result, rest on parses whose
input was not retained. (The current ingest does capture raw rules; that artefact
predates it.)

Completeness is therefore tracked per market and gates the claims that depend on
it. An equivalence verdict strong enough to license a risk-free arbitrage claim
requires the rules it compared to still exist; a verdict derived from titles alone
may be reported, but not as proof.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class RuleCompleteness(StrEnum):
    """What survived of the venue's own words."""

    #: Raw rules text, a resolution source, and normalized terms.
    COMPLETE = "complete"
    #: Raw text present but missing a source, or normalized without one of them.
    PARTIAL = "partial"
    #: Only the title and normalized terms. The terms cannot be re-derived.
    TITLE_ONLY = "title_only"
    #: Nothing beyond an identifier.
    UNAVAILABLE = "unavailable"

    @property
    def supports_strict_equivalence(self) -> bool:
        """Whether a guaranteed-arbitrage claim may rest on this.

        Only COMPLETE. A strict claim asserts the two contracts settle identically
        in every case, and that cannot be established from a parse whose source is
        gone - the parse may be right, but "may be right" is not what strict means.
        """
        return self is RuleCompleteness.COMPLETE

    @property
    def supports_standard_equivalence(self) -> bool:
        """PARTIAL is enough for a probable match, which claims less."""
        return self in (RuleCompleteness.COMPLETE, RuleCompleteness.PARTIAL)


def classify_completeness(
    *,
    raw_rules: str | None,
    settlement_source: str | None,
    normalized_terms: dict | None,
) -> RuleCompleteness:
    has_raw = bool((raw_rules or "").strip())
    has_source = bool((settlement_source or "").strip())
    has_terms = bool(normalized_terms)

    if has_raw and has_source and has_terms:
        return RuleCompleteness.COMPLETE
    if has_raw or (has_terms and has_source):
        return RuleCompleteness.PARTIAL
    if has_terms:
        return RuleCompleteness.TITLE_ONLY
    return RuleCompleteness.UNAVAILABLE


def rule_hash(raw_rules: str | None) -> str:
    """Identity of a specific wording, used to detect a rewrite.

    Whitespace-normalised so a reflow does not read as a rule change, but
    otherwise exact: a single changed comparator is a different contract.
    """
    text = " ".join((raw_rules or "").split())
    return hashlib.sha256(text.encode()).hexdigest()[:32] if text else ""


@dataclass(frozen=True)
class RuleChange:
    """What moved between two observations of the same market's rules."""

    previous_hash: str
    new_hash: str
    changed_fields: tuple[str, ...]

    @property
    def is_material(self) -> bool:
        """Whether the change could alter how the contract settles.

        A change to the rules text or any settlement-bearing field is material.
        Presentation-only fields are not, so a venue reformatting its page does
        not invalidate every stored verdict.
        """
        material = {
            "raw_rules",
            "settlement_source",
            "threshold_value",
            "comparator",
            "cutoff_time",
            "includes_overtime",
            "uses_revised_data",
        }
        return bool(material.intersection(self.changed_fields))


def diff_rules(previous: dict, current: dict) -> RuleChange:
    """Compare two rule observations field by field."""
    fields = sorted(set(previous) | set(current))
    changed = tuple(f for f in fields if previous.get(f) != current.get(f))
    return RuleChange(
        previous_hash=rule_hash(previous.get("raw_rules")),
        new_hash=rule_hash(current.get("raw_rules")),
        changed_fields=changed,
    )
