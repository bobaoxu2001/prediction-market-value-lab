"""Deterministic verification of proposed cross-platform matches.

This module decides :class:`RuleCompatibility`, and that decision is the gate on the
phrase "executable arbitrage". The design principle is **asymmetric caution**: every
check can only *lower* compatibility, never raise it, and every reason for lowering
it is recorded as a human-readable mismatch string.

An LLM may propose or annotate matches, but it cannot promote one: `verify_match` is
pure, deterministic, and testable, and it is the only thing that can return
``IDENTICAL``.

Compatibility ladder
--------------------
``IDENTICAL``
    Same threshold, same comparator, same measurement basis, same settlement source
    family, cutoffs within an hour, no qualifier conflicts, and a resolution-hash
    match. Only this level may back a risk-free claim.
``EQUIVALENT``
    Same substantive terms with immaterial differences (e.g. cutoffs within a day,
    or one side's source unknown). Tradeable as an opportunity, but labelled with
    residual rule risk.
``SIMILAR``
    Same subject, at least one material term differs. Usable as a weak prior only.
``INCOMPATIBLE``
    Different questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from pmvl_shared.enums import RuleCompatibility
from pmvl_shared.money import D, ZERO, safe_div
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import ensure_utc

from ..normalize.rules import (
    KALSHI_STRIKE_COMPARATORS,
    NormalizedRules,
    normalize_rules,
)
from .candidates import MatchCandidate

#: Cutoff differences at or below this are immaterial.
IDENTICAL_CUTOFF_TOLERANCE = timedelta(hours=1)
EQUIVALENT_CUTOFF_TOLERANCE = timedelta(hours=24)
#: Relative threshold difference tolerated before the strikes are "different".
THRESHOLD_RELATIVE_TOLERANCE = Decimal("0.001")

#: Comparator pairs that are materially the same for a continuous quantity.
#: ">" and ">=" differ only on an exact tie, which for a continuous price or
#: temperature has measure zero. For an integer count they genuinely differ, so the
#: caller supplies `continuous` accordingly.
_CONTINUOUS_EQUIVALENT = {("gt", "gte"), ("gte", "gt"), ("lt", "lte"), ("lte", "lt")}
_OPPOSITE = {("gt", "lte"), ("lte", "gt"), ("gte", "lt"), ("lt", "gte")}


class DemotionCode(StrEnum):
    """Stable identifier for why a pair was demoted.

    The human-readable reasons carry numbers and are written to be read once; a
    histogram built by pattern-matching that prose would break the moment the wording
    changed. These codes are the thing aggregation keys on, so they must stay stable
    even when the message text is reworded.
    """

    ENTITY_MISMATCH = "entity_mismatch"
    LOW_TITLE_SIMILARITY = "low_title_similarity"
    LOW_ENTITY_OVERLAP = "low_entity_overlap"
    THRESHOLD_DIFFERS = "threshold_differs"
    THRESHOLD_UNKNOWN = "threshold_unknown"
    COMPARATOR_INCOMPATIBLE = "comparator_incompatible"
    COMPARATOR_UNKNOWN = "comparator_unknown"
    MEASUREMENT_BASIS_DIFFERS = "measurement_basis_differs"
    MEASUREMENT_BASIS_UNKNOWN = "measurement_basis_unknown"
    CUTOFF_UNKNOWN = "cutoff_unknown"
    CUTOFF_DRIFT_HOURS = "cutoff_drift_hours"
    CUTOFF_DIFFERS_DAYS = "cutoff_differs_days"
    SOURCE_DIFFERS = "source_differs"
    SOURCE_UNKNOWN = "source_unknown"
    OVERTIME_DIFFERS = "overtime_differs"
    REVISION_DIFFERS = "revision_differs"
    DIGEST_DIFFERS = "digest_differs"

    @property
    def is_missing_information(self) -> bool:
        """Whether this is absent evidence rather than a contradiction.

        The distinction is what makes the histogram actionable. A pile of
        *_UNKNOWN codes means the rule parser is not extracting a field, which is a
        fixable engineering gap. A pile of *_DIFFERS codes means the contracts really
        do settle differently, which is a finding about the venues rather than a bug.
        """
        return self in (
            DemotionCode.THRESHOLD_UNKNOWN,
            DemotionCode.COMPARATOR_UNKNOWN,
            DemotionCode.MEASUREMENT_BASIS_UNKNOWN,
            DemotionCode.CUTOFF_UNKNOWN,
            DemotionCode.SOURCE_UNKNOWN,
            DemotionCode.DIGEST_DIFFERS,
        )


@dataclass
class MatchVerdict:
    """The outcome of verifying one candidate pair."""

    rule_compatibility: RuleCompatibility
    match_confidence: Decimal
    time_compatibility: Decimal
    source_compatibility: Decimal
    polarity_inverted: bool
    outcome_mapping: dict[str, str]
    mismatch_reasons: list[str] = field(default_factory=list)
    #: Stable codes parallel to ``mismatch_reasons``, for aggregation.
    demotion_codes: list[str] = field(default_factory=list)
    resolution_hash_a: str = ""
    resolution_hash_b: str = ""

    @property
    def is_tradeable_pair(self) -> bool:
        """Whether this pair may be used for a cross-platform opportunity at all."""
        return self.rule_compatibility in (
            RuleCompatibility.IDENTICAL,
            RuleCompatibility.EQUIVALENT,
        )

    @property
    def allows_riskfree_claim(self) -> bool:
        """Only an exact rule match may ever be labelled executable arbitrage."""
        return self.rule_compatibility == RuleCompatibility.IDENTICAL


def compare_thresholds(a: Decimal | None, b: Decimal | None) -> str:
    """Compare two thresholds as ``"match"``, ``"differ"`` or ``"unknown"``.

    The three-way result matters. A ``None`` threshold means *not established* - the
    venue published structured strike metadata with no numeric strike ("finish top
    20"), so text extraction was deliberately suppressed. Treating that as "different
    from 10" is a category error: it is a contradiction only when both sides are
    known and disagree. Collapsing unknown into differ rejected every candidate pair
    outright, because the two venues almost never both expose a numeric strike for
    the same question.
    """
    if a is None and b is None:
        return "unknown"
    if a is None or b is None:
        return "unknown"
    if a == b:
        return "match"
    scale = max(abs(a), abs(b))
    if scale == 0:
        return "match"
    if safe_div(abs(a - b), scale) <= THRESHOLD_RELATIVE_TOLERANCE:
        return "match"
    return "differ"


def thresholds_match(a: Decimal | None, b: Decimal | None) -> bool:
    """Whether two thresholds are positively known to agree."""
    return compare_thresholds(a, b) == "match"


def comparators_match(a: str, b: str, *, continuous: bool) -> tuple[bool, bool]:
    """Return ``(compatible, polarity_inverted)``."""
    if not a or not b:
        return True, False  # unknown comparator is handled as a separate penalty
    if a == b:
        return True, False
    if continuous and (a, b) in _CONTINUOUS_EQUIVALENT:
        return True, False
    if (a, b) in _OPPOSITE:
        return True, True
    return False, False


#: Granularity of the value the SETTLEMENT SOURCE publishes, per category.
#:
#: This is not the granularity of the underlying physical quantity, and conflating
#: the two is a real source of false equivalence. Air temperature is continuous, but
#: Kalshi and Polymarket settle their temperature markets on the NWS climatological
#: report, which publishes WHOLE DEGREES. On that lattice "above 75" means 76-or-more
#: while "75 or above" includes 75, and a whole degree of probability mass separates
#: two contracts that a category-level "temperature is continuous" rule would have
#: declared identical.
#:
#: ``None`` means the published value is effectively continuous at the scale these
#: markets are struck on.
_SETTLEMENT_STEP: dict[str, Decimal] = {
    "weather": Decimal("1"),      # NWS reports whole degrees
    "economics": Decimal("0.1"),  # CPI/GDP prints carry one decimal
}

#: Phrases implying a counted quantity, where every value is an integer and a tie on
#: the threshold is a reachable outcome.
_DISCRETE_MARKERS = (
    "how many", "number of", "times", "count", "goals", "points", "seats",
    "medals", "wins", "games", "runs", "sets",
)


def settlement_step(market: NormalizedMarket) -> Decimal | None:
    """Smallest increment the settlement source can publish, if it is lattice-valued.

    Returns ``None`` when the published value is fine-grained enough that landing
    exactly on a threshold has negligible probability.
    """
    text = f"{market.title} {market.subtitle}".lower()
    if any(marker in text for marker in _DISCRETE_MARKERS):
        return Decimal("1")
    return _SETTLEMENT_STEP.get(market.category.value)


def is_continuous_quantity(market: NormalizedMarket) -> bool:
    """Whether '>' and '>=' at the same threshold are the same contract.

    True only when the settled value cannot realistically land exactly on the
    threshold. A lattice-valued source (whole degrees, integer counts) makes the tie
    reachable, so the two comparators describe genuinely different contracts.
    """
    step = settlement_step(market)
    if step is None:
        return True
    threshold = market.floor_strike if market.floor_strike is not None else market.cap_strike
    if threshold is None or threshold == 0:
        # Lattice-valued source and no threshold to scale against: assume the tie is
        # reachable rather than assume it away.
        return False
    # A step that is a negligible fraction of the threshold cannot separate the two
    # comparators in practice (a $0.01 step against a $70,000 strike).
    return abs(step / threshold) < Decimal("0.0001")


def cutoff_delta(a: NormalizedRules, b: NormalizedRules) -> timedelta | None:
    ta, tb = ensure_utc(a.cutoff_utc), ensure_utc(b.cutoff_utc)
    if ta is None or tb is None:
        return None
    return abs(ta - tb)


#: Capitalised words that start a sentence or are generic headline furniture, and so
#: carry no identifying information.
_NON_IDENTIFYING_CAPS = {
    "will", "the", "a", "an", "is", "are", "does", "do", "who", "what", "which",
    "when", "how", "if", "by", "in", "on", "at", "to", "for", "and", "or", "of",
    "yes", "no", "top", "over", "under", "above", "below", "before", "after",
    "market", "price", "high", "low", "open", "close", "win", "wins", "finish",
    "round", "game", "match", "day", "week", "month", "year", "total", "score",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def proper_nouns(text: str) -> set[str]:
    """Capitalised, identifying words in a market title.

    A word qualifies when it is capitalised mid-sentence (so not merely the first
    word), is at least four characters, and is not generic headline furniture. These
    are the tokens that name *which* person, team or place the market is about.
    """
    import re as _re

    words = _re.findall(r"\b[A-Z][a-zA-Z'\-]{3,}\b", text)
    out: set[str] = set()
    for index, word in enumerate(words):
        lowered = word.lower()
        if lowered in _NON_IDENTIFYING_CAPS:
            continue
        # Skip a leading capital that is only capitalised because it starts the title.
        if index == 0 and text.strip().startswith(word):
            continue
        out.add(lowered)
    return out


def unmatched_proper_nouns(a: NormalizedMarket, b: NormalizedMarket) -> set[str]:
    """Identifying names appearing on one side but nowhere in the other's text.

    The comparison is against the other market's *full* text (title, subtitle and
    rules), not just its title, so a name that appears only in the settlement rules
    still counts as present and does not trigger a false rejection.
    """
    text_a = f"{a.title} {a.subtitle}"
    text_b = f"{b.title} {b.subtitle}"
    full_a = f"{text_a} {a.settlement_rules_raw[:1500]}".lower()
    full_b = f"{text_b} {b.settlement_rules_raw[:1500]}".lower()

    nouns_a = proper_nouns(text_a)
    nouns_b = proper_nouns(text_b)

    missing = {n for n in nouns_a if n not in full_b}
    missing |= {n for n in nouns_b if n not in full_a}
    return missing


def rules_for(market: NormalizedMarket) -> NormalizedRules:
    """Structured settlement terms for a market.

    The venue's own ``strike_type`` is passed through as the authoritative
    comparator. Without it the comparator came from a regex over the title and
    subtitle, and Kalshi's subtitle convention restates a ``>75`` market inclusively
    as "76 or above" - so text extraction returned ``gte`` for a market the venue had
    explicitly declared ``greater``. Both sides of a strict-vs-inclusive pair then
    normalised to the same comparator and the pair was certified IDENTICAL, which is
    exactly the false risk-free claim this module exists to prevent. Structured venue
    fields beat text extraction, the same rule already applied to the threshold.
    """
    return normalize_rules(
        title=market.title,
        subtitle=market.subtitle,
        description=market.settlement_rules_raw or market.description,
        settlement_source=market.settlement_source,
        cutoff_time=(
            market.event_occurrence_time
            or market.expected_resolution_time
            or market.close_time
        ),
        explicit_threshold=(
            market.floor_strike if market.floor_strike is not None else market.cap_strike
        ),
        explicit_comparator=KALSHI_STRIKE_COMPARATORS.get(
            (market.strike_type or "").lower(), ""
        ),
        has_structured_strike=bool(market.strike_type),
    )


def verify_match(candidate: MatchCandidate) -> MatchVerdict:
    """Decide how compatible a proposed pair really is.

    Starts optimistic at ``IDENTICAL`` and demotes on every failed check. There is no
    path that promotes.
    """
    a, b = candidate.market_a, candidate.market_b
    rules_a, rules_b = rules_for(a), rules_for(b)

    reasons: list[str] = []
    codes: list[str] = []
    level = RuleCompatibility.IDENTICAL
    polarity_inverted = False

    def demote(to: RuleCompatibility, code: DemotionCode, reason: str) -> None:
        nonlocal level
        reasons.append(reason)
        codes.append(code.value)
        order = [
            RuleCompatibility.IDENTICAL,
            RuleCompatibility.EQUIVALENT,
            RuleCompatibility.SIMILAR,
            RuleCompatibility.INCOMPATIBLE,
        ]
        if order.index(to) > order.index(level):
            level = to

    # ---- distinguishing proper nouns -------------------------------------
    # Two markets naming different people, teams or places are different questions,
    # no matter how similar the surrounding boilerplate is. Token-overlap scores do
    # not catch this: "Will Michael Brennan finish top 10" and "Will Michael Kim
    # finish in the Top 10" share every word except the one that decides the outcome,
    # and scored 0.55 confidence before this check existed.
    unmatched = unmatched_proper_nouns(a, b)
    if unmatched:
        demote(
            RuleCompatibility.INCOMPATIBLE,
            DemotionCode.ENTITY_MISMATCH,
            "proper nouns present on only one side: " + ", ".join(sorted(unmatched)[:4]),
        )

    # ---- subject ---------------------------------------------------------
    if candidate.token_similarity < 0.35:
        demote(
            RuleCompatibility.SIMILAR,
            DemotionCode.LOW_TITLE_SIMILARITY,
            f"low title similarity ({candidate.token_similarity:.2f})",
        )
    if candidate.entity_overlap < 0.3:
        demote(
            RuleCompatibility.SIMILAR,
            DemotionCode.LOW_ENTITY_OVERLAP,
            f"low entity overlap ({candidate.entity_overlap:.2f})",
        )

    # ---- threshold -------------------------------------------------------
    threshold_verdict = compare_thresholds(rules_a.threshold, rules_b.threshold)
    if threshold_verdict == "differ":
        demote(
            RuleCompatibility.INCOMPATIBLE,
            DemotionCode.THRESHOLD_DIFFERS,
            f"different thresholds ({rules_a.threshold} vs {rules_b.threshold})",
        )
    elif threshold_verdict == "unknown":
        # Cannot be confirmed identical without a verified threshold on both sides,
        # but this is missing information rather than a contradiction.
        demote(
            RuleCompatibility.SIMILAR,
            DemotionCode.THRESHOLD_UNKNOWN,
            f"threshold not established on both sides "
            f"({rules_a.threshold} vs {rules_b.threshold})",
        )

    # ---- comparator / polarity -------------------------------------------
    continuous = is_continuous_quantity(a) and is_continuous_quantity(b)
    compatible, inverted = comparators_match(
        rules_a.comparator, rules_b.comparator, continuous=continuous
    )
    if not compatible:
        demote(
            RuleCompatibility.INCOMPATIBLE,
            DemotionCode.COMPARATOR_INCOMPATIBLE,
            f"incompatible comparators ({rules_a.comparator} vs {rules_b.comparator})",
        )
    polarity_inverted = inverted
    if inverted:
        reasons.append("YES/NO polarity is inverted between venues")
    if not rules_a.comparator or not rules_b.comparator:
        demote(
            RuleCompatibility.EQUIVALENT,
            DemotionCode.COMPARATOR_UNKNOWN,
            "comparator could not be established on one side",
        )

    # ---- measurement basis ----------------------------------------------
    sem_a, sem_b = rules_a.threshold_semantics, rules_b.threshold_semantics
    if sem_a and sem_b and sem_a != sem_b:
        # "closes above" vs "touches intraday" are genuinely different questions.
        demote(
            RuleCompatibility.INCOMPATIBLE,
            DemotionCode.MEASUREMENT_BASIS_DIFFERS,
            f"different measurement basis ({sem_a} vs {sem_b})",
        )
    elif bool(sem_a) != bool(sem_b):
        demote(
            RuleCompatibility.EQUIVALENT,
            DemotionCode.MEASUREMENT_BASIS_UNKNOWN,
            f"measurement basis stated on only one side ({sem_a or sem_b})",
        )

    # ---- timing ----------------------------------------------------------
    delta = cutoff_delta(rules_a, rules_b)
    if delta is None:
        time_compatibility = D("0.5")
        demote(
            RuleCompatibility.EQUIVALENT,
            DemotionCode.CUTOFF_UNKNOWN,
            "cutoff time unknown on at least one side",
        )
    elif delta <= IDENTICAL_CUTOFF_TOLERANCE:
        time_compatibility = D(1)
    elif delta <= EQUIVALENT_CUTOFF_TOLERANCE:
        time_compatibility = D("0.7")
        demote(
            RuleCompatibility.EQUIVALENT,
            DemotionCode.CUTOFF_DRIFT_HOURS,
            f"cutoffs differ by {delta.total_seconds() / 3600:.1f}h",
        )
    else:
        time_compatibility = ZERO
        demote(
            RuleCompatibility.INCOMPATIBLE,
            DemotionCode.CUTOFF_DIFFERS_DAYS,
            f"cutoffs differ by {delta.total_seconds() / 86400:.1f} days",
        )

    # ---- settlement source ----------------------------------------------
    fam_a, fam_b = rules_a.settlement_source_family, rules_b.settlement_source_family
    if fam_a and fam_b and fam_a == fam_b:
        source_compatibility = D(1)
    elif fam_a and fam_b:
        source_compatibility = ZERO
        # Different official sources can and do disagree (a revised print, a
        # different index methodology), so this is a material difference.
        demote(
            RuleCompatibility.SIMILAR,
            DemotionCode.SOURCE_DIFFERS,
            f"different settlement sources ({fam_a} vs {fam_b})",
        )
    else:
        source_compatibility = D("0.4")
        demote(
            RuleCompatibility.EQUIVALENT,
            DemotionCode.SOURCE_UNKNOWN,
            "settlement source could not be identified on at least one side",
        )

    # ---- qualifiers ------------------------------------------------------
    if (
        rules_a.includes_overtime is not None
        and rules_b.includes_overtime is not None
        and rules_a.includes_overtime != rules_b.includes_overtime
    ):
        demote(
            RuleCompatibility.INCOMPATIBLE,
            DemotionCode.OVERTIME_DIFFERS,
            "overtime inclusion differs",
        )
    if (
        rules_a.uses_revised_data is not None
        and rules_b.uses_revised_data is not None
        and rules_a.uses_revised_data != rules_b.uses_revised_data
    ):
        demote(
            RuleCompatibility.SIMILAR,
            DemotionCode.REVISION_DIFFERS,
            "revised-data treatment differs",
        )

    # ---- digest ----------------------------------------------------------
    if level == RuleCompatibility.IDENTICAL and rules_a.resolution_hash != rules_b.resolution_hash:
        # Every individual term passed but the digest disagrees, so some term the
        # explicit checks do not cover differs. Refuse the risk-free claim.
        demote(
            RuleCompatibility.EQUIVALENT,
            DemotionCode.DIGEST_DIFFERS,
            "normalized rule digests differ despite matching individual terms",
        )

    confidence = _confidence(
        candidate, level, time_compatibility, source_compatibility
    )

    outcome_mapping = (
        {"a_yes": "b_no", "a_no": "b_yes"} if polarity_inverted
        else {"a_yes": "b_yes", "a_no": "b_no"}
    )

    return MatchVerdict(
        rule_compatibility=level,
        match_confidence=confidence,
        time_compatibility=time_compatibility,
        source_compatibility=source_compatibility,
        polarity_inverted=polarity_inverted,
        outcome_mapping=outcome_mapping,
        mismatch_reasons=reasons,
        demotion_codes=codes,
        resolution_hash_a=rules_a.resolution_hash,
        resolution_hash_b=rules_b.resolution_hash,
    )


def _confidence(
    candidate: MatchCandidate,
    level: RuleCompatibility,
    time_compatibility: Decimal,
    source_compatibility: Decimal,
) -> Decimal:
    if level == RuleCompatibility.INCOMPATIBLE:
        return ZERO
    ceiling = {
        RuleCompatibility.IDENTICAL: D("0.98"),
        RuleCompatibility.EQUIVALENT: D("0.8"),
        RuleCompatibility.SIMILAR: D("0.55"),
    }[level]
    textual = D(str(candidate.prescreen_score))
    blended = (
        D("0.5") * textual
        + D("0.25") * time_compatibility
        + D("0.25") * source_compatibility
    )
    return min(ceiling, blended).quantize(Decimal("0.0001"))


def verify_all(candidates: Sequence[MatchCandidate]) -> list[tuple[MatchCandidate, MatchVerdict]]:
    """Verify every candidate, dropping outright incompatible pairs."""
    out: list[tuple[MatchCandidate, MatchVerdict]] = []
    for candidate in candidates:
        verdict = verify_match(candidate)
        if verdict.rule_compatibility == RuleCompatibility.INCOMPATIBLE:
            continue
        out.append((candidate, verdict))
    out.sort(key=lambda pair: pair[1].match_confidence, reverse=True)
    return out
