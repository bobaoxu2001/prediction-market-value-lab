"""Measure candidate-generation recall, so "0 equivalent pairs" can be qualified.

The deployed diagnostic reports "109 pairs examined, 0 verified equivalent". That
describes the 109 pairs the generator proposed and cannot distinguish "the venues
list no equivalent contracts" from "the generator never proposed the equivalent
contracts that exist". Those have opposite implications for what to work on, and
the product has been asserting the first without evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pmvl_markets.matching.benchmark import load_benchmark, run_benchmark

BENCHMARK = Path(__file__).parent / "fixtures" / "matching_benchmark.json"


@pytest.fixture(scope="module")
def pairs():  # noqa: ANN201
    return load_benchmark(BENCHMARK)


@pytest.fixture(scope="module")
def metrics(pairs):  # noqa: ANN001, ANN201
    return run_benchmark(pairs)


class TestBenchmarkShape:
    def test_it_contains_positives_and_negatives(self, pairs) -> None:  # noqa: ANN001
        """A benchmark of only positives measures nothing: a generator that
        proposes every pair would score perfectly."""
        labels = {p.label for p in pairs}
        assert {"equivalent", "near_miss", "false_friend"} <= labels

    def test_every_pair_states_why_it_is_labelled_that_way(self, pairs) -> None:  # noqa: ANN001
        for pair in pairs:
            assert pair.why.strip(), f"{pair.id} has no stated reasoning"

    def test_the_hard_cases_are_represented(self, pairs) -> None:  # noqa: ANN001
        """The distinctions that actually separate contracts, from the equivalence
        work: comparator, measurement basis, 90-minute vs advancement, initial vs
        revised data, interval vs one-sided."""
        ids = {p.id for p in pairs}
        for required in (
            "btc-comparator-differs",
            "soccer-90min-vs-advancement",
            "cpi-initial-vs-revised",
            "temp-interval-vs-one-sided",
            "different-source-same-metric",
        ):
            assert required in ids, f"benchmark is missing the {required} case"


class TestCandidateRecall:
    def test_recall_is_measured_not_assumed(self, metrics) -> None:  # noqa: ANN001
        report = metrics.as_dict()
        assert report["equivalent_total"] > 0
        assert "candidate_recall" in report

    def test_no_truly_equivalent_pair_is_missed(self, metrics) -> None:  # noqa: ANN001
        """A miss here means the generator's blocking rules exclude a real match,
        and "0 verified equivalent" would be measuring the generator rather than
        the venues."""
        assert metrics.missed_equivalent_ids == [], (
            f"generator missed equivalent pairs: {metrics.missed_equivalent_ids}"
        )
        assert metrics.candidate_recall == 1.0

    def test_near_misses_are_mostly_proposed(self, metrics) -> None:  # noqa: ANN001
        """Near misses SHOULD reach the verifier, so it can reject them with a
        stated reason rather than them vanishing silently at generation."""
        assert metrics.near_miss_found >= metrics.near_miss_total - 1

    def test_the_report_carries_its_own_caveat(self, metrics) -> None:  # noqa: ANN001
        """A dozen hand-labelled pairs cannot support a population estimate, and
        the number must not be quoted as though it could."""
        report = metrics.as_dict()
        assert "Indicative only" in report["caveat"]
        assert report["benchmark_size"] == 12


class TestRegressionGuard:
    def test_recall_does_not_silently_degrade(self, metrics) -> None:  # noqa: ANN001
        """Pinned. Tightening the blocking rules to cut false friends is a
        reasonable change; doing it without noticing that a real match stopped
        being proposed is not."""
        assert metrics.candidate_recall >= 1.0, (
            "candidate recall regressed below the benchmarked baseline of 100%"
        )

    def test_the_generator_still_filters_something(self, metrics) -> None:  # noqa: ANN001
        """A generator that proposes every false friend is doing no work; the
        verifier would carry the entire burden."""
        assert metrics.false_friend_rate < 1.0
