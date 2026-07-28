"""Component-wise contract equivalence scoring.

:func:`~pmvl_markets.matching.verify.verify_match` answers "how compatible is this
pair" as a single monotone level. That is the right shape for the internal gate, but
it collapses eight independent questions into one answer, so a reader cannot see
*which* term failed, and a caller cannot apply a different bar to, say, a cancellation
mismatch than to a threshold mismatch.

This module scores each component separately and then derives a public verdict. It
**wraps** the existing verifier rather than reimplementing it: the deterministic
demotion logic there stays the single source of truth for compatibility, and this
layer adds attribution on top.

The verdict vocabulary is deliberately blunt about what it licenses:

``VERIFIED_EQUIVALENT_STRICT``
    Every material term checked out, *including* how each venue handles cancellation,
    postponement and void. The only verdict that may back a guaranteed-arbitrage claim.
``VERIFIED_EQUIVALENT_STANDARD``
    The terms that decide the payout are identical, but cancellation handling could
    not be confirmed on both sides. Tradeable as a hedged position carrying residual
    rule risk; never as guaranteed arbitrage.
``PROBABLE_MATCH``
    Same question in substance, but at least one term could not be *confirmed*.
    Tradeable as relative value; never as arbitrage.
``RELATED_NOT_EQUIVALENT``
    Genuinely related and worth showing side by side, but a material term differs.
``REJECTED``
    Different questions.

The asymmetry is intentional: missing information demotes exactly like a
contradiction does, because an unverified term is not a verified one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.enums import RuleCompatibility
from pmvl_shared.money import D, ZERO

from ..normalize.rules import NormalizedRules
from .candidates import MatchCandidate
from .verify import (
    MatchVerdict,
    compare_thresholds,
    comparators_match,
    cutoff_delta,
    is_continuous_quantity,
    rules_for,
    unmatched_proper_nouns,
    verify_match,
)


class EquivalenceVerdict(StrEnum):
    """Public equivalence vocabulary.

    Split into two verified tiers because neither venue publishes cancellation,
    postponement or void handling in structured form. Collapsing them into a single
    "verified" level forces a false choice: either certify pairs whose void behaviour
    is unknown - and a pair where one leg voids while the other pays is not riskless -
    or refuse every cross-venue pair outright and lose the genuine ones too.
    """

    VERIFIED_EQUIVALENT_STRICT = "VERIFIED_EQUIVALENT_STRICT"
    VERIFIED_EQUIVALENT_STANDARD = "VERIFIED_EQUIVALENT_STANDARD"
    PROBABLE_MATCH = "PROBABLE_MATCH"
    RELATED_NOT_EQUIVALENT = "RELATED_NOT_EQUIVALENT"
    REJECTED = "REJECTED"

    @property
    def allows_guaranteed_arbitrage(self) -> bool:
        """Only a STRICT match may back a guaranteed terminal payout.

        STANDARD is deliberately excluded. Its payout terms agree, but if one venue
        voids a postponed event while the other settles it, both legs can lose - which
        is precisely the thing "guaranteed" is supposed to rule out.
        """
        return self is EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT

    #: Retained under the old name so existing callers keep their meaning.
    @property
    def allows_cross_platform_arbitrage(self) -> bool:
        return self.allows_guaranteed_arbitrage

    @property
    def allows_hedged_position(self) -> bool:
        """STANDARD supports a hedged trade carrying residual rule risk."""
        return self in (
            EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT,
            EquivalenceVerdict.VERIFIED_EQUIVALENT_STANDARD,
        )

    @property
    def allows_relative_value(self) -> bool:
        """A probable match can carry a directional view, but not a guarantee."""
        return self in (
            EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT,
            EquivalenceVerdict.VERIFIED_EQUIVALENT_STANDARD,
            EquivalenceVerdict.PROBABLE_MATCH,
        )


#: How a single component turned out. ``UNKNOWN`` is not a pass.
class ComponentResult(StrEnum):
    MATCH = "match"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class ComponentScore:
    """One component of the equivalence decision, with its reason."""

    name: str
    result: ComponentResult
    detail: str = ""

    @property
    def score(self) -> Decimal:
        return {
            ComponentResult.MATCH: D(1),
            ComponentResult.UNKNOWN: D("0.5"),
            ComponentResult.MISMATCH: ZERO,
        }[self.result]


@dataclass
class EquivalenceScore:
    """Per-component attribution plus the derived public verdict."""

    verdict: EquivalenceVerdict
    components: list[ComponentScore]
    #: The underlying monotone level, kept so existing callers keep working.
    rule_compatibility: RuleCompatibility
    match_confidence: Decimal
    polarity_inverted: bool
    outcome_mapping: dict[str, str]
    resolution_hash_a: str = ""
    resolution_hash_b: str = ""
    reasons: list[str] = field(default_factory=list)

    def component(self, name: str) -> ComponentScore | None:
        return next((c for c in self.components if c.name == name), None)

    @property
    def mismatches(self) -> list[ComponentScore]:
        return [c for c in self.components if c.result is ComponentResult.MISMATCH]

    @property
    def unknowns(self) -> list[ComponentScore]:
        return [c for c in self.components if c.result is ComponentResult.UNKNOWN]

    @property
    def aggregate_score(self) -> Decimal:
        """Mean component score. Reporting only - the verdict is rule-based.

        A weighted average must never decide equivalence: it lets several strong
        components outvote one fatal mismatch, which is exactly how a pair with
        different settlement sources gets called identical.
        """
        if not self.components:
            return ZERO
        total = sum((c.score for c in self.components), ZERO)
        return (total / D(len(self.components))).quantize(Decimal("0.0001"))

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "allows_guaranteed_arbitrage": self.verdict.allows_guaranteed_arbitrage,
            "allows_cross_platform_arbitrage": self.verdict.allows_guaranteed_arbitrage,
            "allows_hedged_position": self.verdict.allows_hedged_position,
            "allows_relative_value": self.verdict.allows_relative_value,
            "cancellation_status": (
                next(
                    (c.result.value for c in self.components if c.name == "cancellation"),
                    "unknown",
                )
            ),
            "aggregate_score": str(self.aggregate_score),
            "match_confidence": str(self.match_confidence),
            "polarity_inverted": self.polarity_inverted,
            "outcome_mapping": self.outcome_mapping,
            "rule_compatibility": self.rule_compatibility.value,
            "resolution_hash_a": self.resolution_hash_a,
            "resolution_hash_b": self.resolution_hash_b,
            "components": [
                {"name": c.name, "result": c.result.value, "detail": c.detail}
                for c in self.components
            ],
            "reasons": list(self.reasons),
        }


# --------------------------------------------------------------- components
def _entity_component(candidate: MatchCandidate) -> ComponentScore:
    unmatched = unmatched_proper_nouns(candidate.market_a, candidate.market_b)
    if unmatched:
        return ComponentScore(
            "entity", ComponentResult.MISMATCH,
            "named on one side only: " + ", ".join(sorted(unmatched)[:4]),
        )
    if candidate.entity_overlap < 0.3:
        return ComponentScore(
            "entity", ComponentResult.MISMATCH,
            f"entity overlap {candidate.entity_overlap:.2f} below 0.30",
        )
    return ComponentScore("entity", ComponentResult.MATCH,
                          f"entity overlap {candidate.entity_overlap:.2f}")


def _event_component(candidate: MatchCandidate) -> ComponentScore:
    if candidate.token_similarity < 0.35:
        return ComponentScore(
            "event", ComponentResult.MISMATCH,
            f"title similarity {candidate.token_similarity:.2f} below 0.35",
        )
    if candidate.token_similarity < 0.6:
        return ComponentScore(
            "event", ComponentResult.UNKNOWN,
            f"title similarity {candidate.token_similarity:.2f} is weak",
        )
    return ComponentScore("event", ComponentResult.MATCH,
                          f"title similarity {candidate.token_similarity:.2f}")


def _threshold_component(a: NormalizedRules, b: NormalizedRules) -> ComponentScore:
    verdict = compare_thresholds(a.threshold, b.threshold)
    if verdict == "differ":
        return ComponentScore("threshold", ComponentResult.MISMATCH,
                              f"{a.threshold} vs {b.threshold}")
    if verdict == "unknown":
        return ComponentScore("threshold", ComponentResult.UNKNOWN,
                              f"not established on both sides ({a.threshold} vs {b.threshold})")
    return ComponentScore("threshold", ComponentResult.MATCH, str(a.threshold))


def _operator_component(
    candidate: MatchCandidate, a: NormalizedRules, b: NormalizedRules
) -> ComponentScore:
    if not a.comparator or not b.comparator:
        return ComponentScore("operator", ComponentResult.UNKNOWN,
                              "comparator not established on both sides")
    continuous = is_continuous_quantity(candidate.market_a) and is_continuous_quantity(
        candidate.market_b
    )
    compatible, inverted = comparators_match(a.comparator, b.comparator, continuous=continuous)
    if not compatible:
        return ComponentScore("operator", ComponentResult.MISMATCH,
                              f"{a.comparator} vs {b.comparator}")
    detail = f"{a.comparator} vs {b.comparator}"
    if inverted:
        detail += " (polarity inverted)"
    if not continuous and a.comparator != b.comparator:
        detail += " on a lattice-valued source"
    return ComponentScore("operator", ComponentResult.MATCH, detail)


def _time_component(a: NormalizedRules, b: NormalizedRules) -> ComponentScore:
    delta = cutoff_delta(a, b)
    if delta is None:
        return ComponentScore("time", ComponentResult.UNKNOWN,
                              "cutoff unknown on at least one side")
    hours = delta.total_seconds() / 3600
    if hours <= 1:
        return ComponentScore("time", ComponentResult.MATCH, f"cutoffs within {hours:.2f}h")
    if hours <= 24:
        return ComponentScore("time", ComponentResult.UNKNOWN, f"cutoffs differ by {hours:.1f}h")
    return ComponentScore("time", ComponentResult.MISMATCH,
                          f"cutoffs differ by {hours / 24:.1f} days")


def _source_component(a: NormalizedRules, b: NormalizedRules) -> ComponentScore:
    fam_a, fam_b = a.settlement_source_family, b.settlement_source_family
    if fam_a and fam_b:
        if fam_a == fam_b:
            return ComponentScore("data_source", ComponentResult.MATCH, fam_a)
        return ComponentScore("data_source", ComponentResult.MISMATCH, f"{fam_a} vs {fam_b}")
    return ComponentScore("data_source", ComponentResult.UNKNOWN,
                          "settlement authority not identified on both sides")


def _settlement_rule_component(a: NormalizedRules, b: NormalizedRules) -> ComponentScore:
    """Measurement basis plus the qualifiers that change who gets paid."""
    sem_a, sem_b = a.threshold_semantics, b.threshold_semantics
    if sem_a and sem_b and sem_a != sem_b:
        return ComponentScore("settlement_rule", ComponentResult.MISMATCH,
                              f"measurement basis {sem_a} vs {sem_b}")
    if (
        a.includes_overtime is not None
        and b.includes_overtime is not None
        and a.includes_overtime != b.includes_overtime
    ):
        return ComponentScore("settlement_rule", ComponentResult.MISMATCH,
                              "overtime inclusion differs")
    if (
        a.uses_revised_data is not None
        and b.uses_revised_data is not None
        and a.uses_revised_data != b.uses_revised_data
    ):
        return ComponentScore("settlement_rule", ComponentResult.MISMATCH,
                              "revised-data treatment differs")
    if bool(sem_a) != bool(sem_b):
        return ComponentScore("settlement_rule", ComponentResult.UNKNOWN,
                              f"measurement basis stated on one side only ({sem_a or sem_b})")
    if not sem_a and not sem_b:
        return ComponentScore("settlement_rule", ComponentResult.UNKNOWN,
                              "measurement basis not stated on either side")
    return ComponentScore("settlement_rule", ComponentResult.MATCH, sem_a)


def _cancellation_component(a: NormalizedRules, b: NormalizedRules) -> ComponentScore:
    """Cancellation and postponement handling.

    Neither venue publishes this in a structured field, and the rule text rarely
    states it explicitly. Reporting UNKNOWN is the honest answer, and because UNKNOWN
    blocks VERIFIED_EQUIVALENT it is also the conservative one: a pair cannot be
    certified risk-free while it is unknown whether one leg voids and the other pays
    on a postponed event.
    """
    hashes_agree = a.resolution_hash == b.resolution_hash
    if hashes_agree:
        return ComponentScore(
            "cancellation", ComponentResult.UNKNOWN,
            "not published in structured form; normalized rule digests agree",
        )
    return ComponentScore(
        "cancellation", ComponentResult.UNKNOWN,
        "not published in structured form; digests differ, so some uncompared term differs",
    )


# ------------------------------------------------------------------ verdict
def _derive_verdict(
    components: list[ComponentScore], level: RuleCompatibility
) -> EquivalenceVerdict:
    """Rule-based, never a weighted average.

    An average lets several strong components outvote one fatal mismatch, which is
    precisely how a pair settling on two different indices gets called identical.
    """
    if level is RuleCompatibility.INCOMPATIBLE:
        return EquivalenceVerdict.REJECTED

    mismatched = {c.name for c in components if c.result is ComponentResult.MISMATCH}
    if mismatched:
        # A different entity or event is a different question; a different term on the
        # same question is a related contract.
        if mismatched & {"entity", "event"}:
            return EquivalenceVerdict.REJECTED
        return EquivalenceVerdict.RELATED_NOT_EQUIVALENT

    # No contradictions. Everything that decides the payout must be positively
    # confirmed, not merely un-contradicted.
    decisive = {"threshold", "operator", "time", "data_source", "settlement_rule"}
    unconfirmed = {
        c.name for c in components
        if c.result is ComponentResult.UNKNOWN and c.name in decisive
    }
    if unconfirmed:
        return EquivalenceVerdict.PROBABLE_MATCH

    if level is not RuleCompatibility.IDENTICAL:
        # The monotone verifier found something these components do not cover.
        return EquivalenceVerdict.PROBABLE_MATCH

    # Payout terms agree. Cancellation decides which verified tier this is.
    #
    # Neither venue publishes void/postponement handling structurally, so in practice
    # this is almost always UNKNOWN and almost every real pair lands on STANDARD. That
    # is the honest outcome: the payout terms really do match, and the void behaviour
    # really is unverified, so the pair supports a hedge but not a guarantee.
    cancellation = next((c for c in components if c.name == "cancellation"), None)
    if cancellation is not None and cancellation.result is not ComponentResult.MATCH:
        return EquivalenceVerdict.VERIFIED_EQUIVALENT_STANDARD

    return EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT


def score_equivalence(
    candidate: MatchCandidate, verdict: MatchVerdict | None = None
) -> EquivalenceScore:
    """Score a candidate pair component by component and derive a public verdict."""
    verdict = verdict or verify_match(candidate)
    rules_a, rules_b = rules_for(candidate.market_a), rules_for(candidate.market_b)

    components = [
        _entity_component(candidate),
        _event_component(candidate),
        _threshold_component(rules_a, rules_b),
        _operator_component(candidate, rules_a, rules_b),
        _time_component(rules_a, rules_b),
        _source_component(rules_a, rules_b),
        _settlement_rule_component(rules_a, rules_b),
        _cancellation_component(rules_a, rules_b),
    ]

    return EquivalenceScore(
        verdict=_derive_verdict(components, verdict.rule_compatibility),
        components=components,
        rule_compatibility=verdict.rule_compatibility,
        match_confidence=verdict.match_confidence,
        polarity_inverted=verdict.polarity_inverted,
        outcome_mapping=verdict.outcome_mapping,
        resolution_hash_a=verdict.resolution_hash_a,
        resolution_hash_b=verdict.resolution_hash_b,
        reasons=list(verdict.mismatch_reasons),
    )
