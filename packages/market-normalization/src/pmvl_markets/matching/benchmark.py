"""Measure what the candidate generator FINDS, not what the verifier decides.

The deployed diagnostic reports "109 pairs examined, 0 verified equivalent". That
is a statement about the 109 pairs the generator proposed, and it cannot separate
two very different worlds:

* the venues genuinely list no contracts with identical settlement terms, or
* equivalent contracts exist and the generator never proposed them.

The first is a result worth writing down. The second is a bug. Reporting the
number without the distinction lets the reader assume whichever they prefer, and
the product has been asserting the first without evidence for it.

Recall here is measured against a small hand-labelled set, so it is an indicative
figure and not a population estimate - a benchmark of a dozen pairs cannot support
a confidence interval, and none is offered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pmvl_shared.enums import Category, MarketStatus, Platform
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from .candidates import generate_candidates


@dataclass
class BenchmarkPair:
    id: str
    label: str
    why: str
    a: NormalizedMarket
    b: NormalizedMarket

    @property
    def must_be_proposed(self) -> bool:
        """Equivalent pairs are the ones a miss is a failure on.

        A near miss SHOULD be proposed so the verifier can reject it with a stated
        reason, but failing to propose one costs nothing - it was never going to
        be a match.
        """
        return self.label == "equivalent"


def _market(spec: dict[str, Any]) -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform(spec["platform"]),
        platform_market_id=spec["id"],
        title=spec["title"],
        status=MarketStatus.OPEN,
        category=Category(spec.get("category", "other")),
        quote_observed_at=utcnow(),
    )


def load_benchmark(path: Path) -> list[BenchmarkPair]:
    data = json.loads(path.read_text())
    return [
        BenchmarkPair(
            id=p["id"], label=p["label"], why=p["why"], a=_market(p["a"]), b=_market(p["b"])
        )
        for p in data["pairs"]
    ]


@dataclass
class RecallMetrics:
    """Candidate-generation performance on the labelled set."""

    equivalent_total: int = 0
    equivalent_found: int = 0
    near_miss_total: int = 0
    near_miss_found: int = 0
    false_friend_total: int = 0
    false_friend_found: int = 0
    missed_equivalent_ids: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.missed_equivalent_ids is None:
            self.missed_equivalent_ids = []

    @property
    def candidate_recall(self) -> float:
        """Fraction of truly-equivalent pairs the generator proposed.

        This is the number the product must quote next to "0 verified equivalent".
        """
        if not self.equivalent_total:
            return 0.0
        return self.equivalent_found / self.equivalent_total

    @property
    def false_friend_rate(self) -> float:
        """Fraction of surface-similar-but-unrelated pairs proposed.

        Not an error in itself - the verifier exists to reject them - but a
        generator proposing every false friend is doing no useful filtering.
        """
        if not self.false_friend_total:
            return 0.0
        return self.false_friend_found / self.false_friend_total

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_recall": round(self.candidate_recall, 4),
            "equivalent_found": self.equivalent_found,
            "equivalent_total": self.equivalent_total,
            "near_miss_found": self.near_miss_found,
            "near_miss_total": self.near_miss_total,
            "false_friend_rate": round(self.false_friend_rate, 4),
            "false_friend_found": self.false_friend_found,
            "false_friend_total": self.false_friend_total,
            "missed_equivalent_ids": sorted(self.missed_equivalent_ids),
            "benchmark_size": (
                self.equivalent_total + self.near_miss_total + self.false_friend_total
            ),
            "caveat": (
                "Indicative only. Measured on a small hand-labelled set, which "
                "cannot support a population estimate or a confidence interval."
            ),
        }


def _proposed(pair: BenchmarkPair, **kwargs: Any) -> bool:
    """Whether the generator proposes this pair when shown only these two markets.

    Deliberately isolated: presenting one pair at a time measures the blocking and
    similarity rules themselves, without the result depending on what else happened
    to be in the corpus.
    """
    candidates = generate_candidates([pair.a], [pair.b], **kwargs)
    return bool(candidates)


def run_benchmark(pairs: list[BenchmarkPair], **kwargs: Any) -> RecallMetrics:
    metrics = RecallMetrics()
    for pair in pairs:
        found = _proposed(pair, **kwargs)
        if pair.label == "equivalent":
            metrics.equivalent_total += 1
            if found:
                metrics.equivalent_found += 1
            else:
                metrics.missed_equivalent_ids.append(pair.id)
        elif pair.label == "near_miss":
            metrics.near_miss_total += 1
            metrics.near_miss_found += int(found)
        else:
            metrics.false_friend_total += 1
            metrics.false_friend_found += int(found)
    return metrics
