"""Validation rules for persisted cross-platform matching diagnostics."""

from __future__ import annotations

import pytest

from scripts.validate_snapshot import matching_diagnostics_problems


def _diagnostics(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ran_at": "2026-07-31T08:56:33.973037Z",
        "pairs_examined": 0,
        "verified_equivalent": 0,
        "blocked_only_by_missing_info": 0,
        "missing_information_count": 0,
        "contradiction_count": 0,
        "top_reasons": [],
        "diagnosis": (
            "No candidate pairs were generated, so nothing reached verification."
        ),
    }
    value.update(overrides)
    return value


def test_a_zero_candidate_scan_is_an_explicit_valid_result() -> None:
    assert matching_diagnostics_problems(True, _diagnostics()) == []


def test_a_nonempty_histogram_remains_valid() -> None:
    diagnostics = _diagnostics(
        pairs_examined=3,
        verified_equivalent=1,
        blocked_only_by_missing_info=1,
        missing_information_count=2,
        contradiction_count=1,
        top_reasons=[{"code": "missing_rule", "count": 2}],
        diagnosis="1 of 3 pairs reached IDENTICAL.",
    )
    assert matching_diagnostics_problems(True, diagnostics) == []


def test_a_recorded_scan_requires_persisted_diagnostics() -> None:
    problems = matching_diagnostics_problems(True, None)
    assert any("matching_diagnostics is null" in p for p in problems)


def test_no_scan_and_no_diagnostics_is_valid() -> None:
    assert matching_diagnostics_problems(False, None) == []


@pytest.mark.parametrize("value", [None, -1, 1.5, "0", True])
def test_pairs_examined_must_be_a_non_negative_integer(value: object) -> None:
    problems = matching_diagnostics_problems(
        True, _diagnostics(pairs_examined=value)
    )
    assert any("pairs_examined must be a non-negative integer" in p for p in problems)


def test_zero_pairs_cannot_hide_nonzero_derived_counts() -> None:
    problems = matching_diagnostics_problems(
        True, _diagnostics(missing_information_count=1)
    )
    assert any("zero pairs but non-zero derived counts" in p for p in problems)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("diagnosis", "", "diagnosis is missing"),
        ("ran_at", None, "ran_at is missing"),
        ("top_reasons", None, "top_reasons must be a list"),
    ],
)
def test_response_shape_fields_are_required(
    field: str, value: object, message: str
) -> None:
    problems = matching_diagnostics_problems(
        True, _diagnostics(**{field: value})
    )
    assert any(message in p for p in problems)


def test_verified_and_parser_blocked_counts_cannot_exceed_examined_pairs() -> None:
    problems = matching_diagnostics_problems(
        True,
        _diagnostics(
            pairs_examined=1,
            verified_equivalent=1,
            blocked_only_by_missing_info=1,
            diagnosis="One verified, one parser-blocked.",
        ),
    )
    assert any(
        "blocked_only_by_missing_info exceeds pairs_examined" in p
        for p in problems
    )
