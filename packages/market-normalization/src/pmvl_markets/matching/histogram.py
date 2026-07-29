"""Aggregate why candidate pairs fail to reach equivalence.

The cross-platform scanner reports zero matches on live data. That is either a
**finding** — Kalshi and Polymarket genuinely list few contracts with identical
settlement terms — or a **bug**, where the rule parser fails to extract a field and
every pair dies on missing information rather than on a real difference.

Those two look identical from the outside, and the difference decides what to work on
next. This module tells them apart by aggregating the stable
:class:`~pmvl_markets.matching.verify.DemotionCode` values across a whole scan and
splitting them into:

``contradiction``
    The two contracts really do settle differently. Nothing to fix; the pair should
    be rejected.
``missing_information``
    A field could not be established on at least one side. Fixable in the parser, and
    every one of these is a pair that *might* be equivalent once it is.

A histogram dominated by ``missing_information`` is an engineering backlog. One
dominated by ``contradiction`` is a result worth writing down.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from pmvl_shared.enums import RuleCompatibility
from pmvl_shared.money import D, ZERO, safe_div

from .verify import DemotionCode, MatchVerdict


@dataclass
class DemotionHistogram:
    """Counts of demotion codes across a set of verified candidate pairs."""

    pairs_examined: int = 0
    by_code: Counter[str] = field(default_factory=Counter)
    by_level: Counter[str] = field(default_factory=Counter)
    #: Pairs whose ONLY demotions were missing-information codes. These are the
    #: pairs a better parser could plausibly promote, so they size the opportunity.
    blocked_only_by_missing_info: int = 0
    #: Pairs that reached the level permitting a risk-free claim.
    verified_equivalent: int = 0

    def add(self, verdict: MatchVerdict) -> None:
        self.pairs_examined += 1
        self.by_level[verdict.rule_compatibility.value] += 1

        codes = [DemotionCode(c) for c in verdict.demotion_codes if c in _VALID_CODES]
        for code in codes:
            self.by_code[code.value] += 1

        if verdict.rule_compatibility is RuleCompatibility.IDENTICAL:
            self.verified_equivalent += 1
        elif codes and all(code.is_missing_information for code in codes):
            self.blocked_only_by_missing_info += 1

    @property
    def missing_information_count(self) -> int:
        return sum(
            n for code, n in self.by_code.items()
            if DemotionCode(code).is_missing_information
        )

    @property
    def contradiction_count(self) -> int:
        return sum(
            n for code, n in self.by_code.items()
            if not DemotionCode(code).is_missing_information
        )

    @property
    def missing_information_share(self) -> Decimal:
        """Fraction of all demotions that are absent evidence rather than conflict."""
        total = self.missing_information_count + self.contradiction_count
        return safe_div(D(self.missing_information_count), D(total)) if total else ZERO

    @property
    def diagnosis(self) -> str:
        """One line naming what the histogram implies about where to spend effort."""
        if self.pairs_examined == 0:
            return "No candidate pairs were generated, so nothing reached verification."
        if self.verified_equivalent:
            return (
                f"{self.verified_equivalent} of {self.pairs_examined} pairs reached "
                "IDENTICAL and may back a cross-platform claim."
            )
        share = self.missing_information_share
        if self.blocked_only_by_missing_info:
            return (
                f"No pair reached IDENTICAL. {self.blocked_only_by_missing_info} of "
                f"{self.pairs_examined} were blocked ONLY by fields the parser could "
                "not establish, so improving rule extraction could promote them. This "
                "is an engineering gap, not yet a finding about the venues."
            )
        if share >= Decimal("0.5"):
            return (
                f"No pair reached IDENTICAL. {share:.0%} of demotions are missing "
                "information, but every pair also has at least one genuine "
                "contradiction, so better parsing alone would not promote any of them."
            )
        return (
            f"No pair reached IDENTICAL, and {1 - share:.0%} of demotions are genuine "
            "contradictions. On this sample the two venues do not list contracts with "
            "identical settlement terms. The candidate generator retrieved "
            f"{BENCHMARK_EQUIVALENT_RETRIEVED}/{BENCHMARK_EQUIVALENT_TOTAL} known "
            f"equivalent examples from {BENCHMARK_SIZE} manually reviewed pairs, so "
            "on the cases reviewed so far this is a result "
            "about the venues rather than about the generator - on a benchmark too "
            "small to generalise from."
        )

    def top_codes(self, limit: int = 10) -> list[tuple[str, int, str]]:
        """``(code, count, kind)`` ordered by frequency."""
        return [
            (
                code,
                count,
                "missing_information"
                if DemotionCode(code).is_missing_information
                else "contradiction",
            )
            for code, count in self.by_code.most_common(limit)
        ]

    def as_dict(self) -> dict[str, object]:
        return {
            "pairs_examined": self.pairs_examined,
            "verified_equivalent": self.verified_equivalent,
            "blocked_only_by_missing_info": self.blocked_only_by_missing_info,
            "missing_information_count": self.missing_information_count,
            "contradiction_count": self.contradiction_count,
            "missing_information_share": str(self.missing_information_share),
            "by_level": dict(self.by_level),
            "by_code": [
                {"code": code, "count": count, "kind": kind}
                for code, count, kind in self.top_codes(limit=50)
            ],
            "diagnosis": self.diagnosis,
        }


_VALID_CODES = {code.value for code in DemotionCode}


def build_histogram(verdicts: Iterable[MatchVerdict]) -> DemotionHistogram:
    histogram = DemotionHistogram()
    for verdict in verdicts:
        histogram.add(verdict)
    return histogram


def histogram_from_pairs(
    pairs: Sequence[tuple[object, MatchVerdict]]
) -> DemotionHistogram:
    """Convenience for the ``verify_all`` return shape."""
    return build_histogram(verdict for _candidate, verdict in pairs)


#: Measured on `tests/fixtures/matching_benchmark.json`. Stated as a number rather
#: than left implicit, because "no equivalent pairs were found" and "no equivalent
#: pairs exist" are different claims and only a recall figure separates them.
#:
#: Recomputed by the benchmark test rather than at import time: loading a fixture
#: and running the generator on every API request would be absurd, and a stale
#: constant that the test pins is honest as long as the test is what pins it.
#: Retrieved / known-equivalent, on the reviewed benchmark. Published as a
#: fraction rather than a percentage, deliberately: "100% recall" reads as a
#: property of the generator, while "3/3" makes the sample size impossible to
#: miss. Three positives cannot support a population estimate, and a reader who
#: sees only the percentage has no way to know that.
BENCHMARK_EQUIVALENT_RETRIEVED = 3
BENCHMARK_EQUIVALENT_TOTAL = 3
BENCHMARK_SIZE = 12

#: What the benchmark must reach before recall may be described as measured
#: rather than illustrated. Recorded here so the gap is visible in the product
#: rather than living only in a review comment.
ROBUST_BENCHMARK_TARGET = {
    "reviewed_pairs": 40,
    "positive_equivalent_pairs": 10,
    "categories": ["sports", "weather", "crypto", "economics", "politics"],
    "structures": [
        "threshold",
        "interval",
        "time_scope_mismatch",
        "source_mismatch",
        "cancellation_mismatch",
    ],
    "both_venue_directions": True,
}


def _recall_phrase() -> str:
    return (
        f"{BENCHMARK_EQUIVALENT_RETRIEVED}/{BENCHMARK_EQUIVALENT_TOTAL} known "
        f"equivalent examples retrieved from {BENCHMARK_SIZE} manually reviewed pairs"
    )


def recall_context() -> dict[str, object]:
    """What the product must publish next to a zero-match result."""
    return {
        "equivalent_retrieved": BENCHMARK_EQUIVALENT_RETRIEVED,
        "equivalent_total": BENCHMARK_EQUIVALENT_TOTAL,
        "reviewed_pairs": BENCHMARK_SIZE,
        # No bare percentage. A ratio of three cannot be quoted as a rate without
        # implying a precision it does not have.
        "headline": (
            f"Candidate-generation benchmark: {BENCHMARK_EQUIVALENT_RETRIEVED}/"
            f"{BENCHMARK_EQUIVALENT_TOTAL} known equivalent examples retrieved; "
            f"{BENCHMARK_SIZE} manually reviewed pairs total. Small benchmark - "
            "not a population recall estimate."
        ),
        "confidence_interval": None,
        "confidence_interval_note": (
            "Not offered. With three positive examples any interval would span "
            "most of the unit range, and quoting one would suggest the sample "
            "supports an estimate it cannot."
        ),
        "is_robust_estimate": False,
        "robust_target": ROBUST_BENCHMARK_TARGET,
        "note": (
            "Recall is how many known-equivalent pairs the candidate generator "
            "proposes. Without it, 'no verified equivalent pair' cannot be "
            "distinguished from 'the generator did not look in the right place'. "
            f"Measured at {_recall_phrase()}."
        ),
    }
