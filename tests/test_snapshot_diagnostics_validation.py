"""Validation rules for persisted cross-platform matching diagnostics."""

from __future__ import annotations

import pytest

from scripts.validate_snapshot import (
    has_recorded_job,
    matching_diagnostics_problems,
    served_matching_diagnostics_problems,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self.payload


class _FakeClient:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self.payloads = payloads

    def get(self, path: str) -> _FakeResponse:
        return _FakeResponse(self.payloads[path])


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


def test_scan_existence_comes_from_job_history_not_opportunity_rows() -> None:
    system_data = {
        "jobs": [
            {
                "job_name": "arbitrage",
                "status": "success",
                "records_written": 0,
            }
        ]
    }
    assert has_recorded_job(system_data, "arbitrage") is True


def test_malformed_job_history_does_not_claim_a_scan() -> None:
    assert has_recorded_job({"jobs": [None, "arbitrage"]}, "arbitrage") is False


def test_a_nonempty_histogram_remains_valid() -> None:
    diagnostics = _diagnostics(
        pairs_examined=3,
        verified_equivalent=1,
        blocked_only_by_missing_info=1,
        missing_information_count=2,
        contradiction_count=1,
        top_reasons=[
            {"code": "threshold_unknown", "count": 2, "kind": "missing_information"},
            {"code": "source_differs", "count": 1, "kind": "contradiction"},
        ],
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
        ("ran_at", "not-a-timestamp", "ran_at is not an ISO timestamp"),
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


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (None, "is not an object"),
        ({"count": 1, "kind": "contradiction"}, "code is missing"),
        (
            {"code": "source_differs", "count": -1, "kind": "contradiction"},
            "count must",
        ),
        (
            {"code": "source_differs", "count": 1, "kind": "unknown"},
            "kind is invalid",
        ),
        (
            {"code": "invented", "count": 1, "kind": "contradiction"},
            "code is invalid",
        ),
        (
            {"code": "source_unknown", "count": 1, "kind": "contradiction"},
            "kind does not match code",
        ),
    ],
)
def test_top_reason_entries_fail_closed(
    reason: object, message: str
) -> None:
    problems = matching_diagnostics_problems(
        True,
        _diagnostics(
            pairs_examined=1,
            contradiction_count=1,
            top_reasons=[reason],
            diagnosis="One pair was rejected.",
        ),
    )
    assert any(message in p for p in problems)


def test_top_reason_counts_cannot_exceed_their_aggregate() -> None:
    problems = matching_diagnostics_problems(
        True,
        _diagnostics(
            pairs_examined=1,
            contradiction_count=1,
            top_reasons=[
                {"code": "source_differs", "count": 2, "kind": "contradiction"}
            ],
            diagnosis="One pair was rejected.",
        ),
    )
    assert any("contradiction reasons exceed" in p for p in problems)


def test_parser_blocked_pairs_require_missing_information_demotions() -> None:
    problems = matching_diagnostics_problems(
        True,
        _diagnostics(
            pairs_examined=1,
            blocked_only_by_missing_info=1,
            diagnosis="One pair was parser-blocked.",
        ),
    )
    assert any("parser-blocked pairs exceed" in p for p in problems)


def test_untruncated_reason_list_must_equal_aggregate() -> None:
    problems = matching_diagnostics_problems(
        True,
        _diagnostics(
            pairs_examined=1,
            contradiction_count=99,
            top_reasons=[
                {"code": "source_differs", "count": 1, "kind": "contradiction"}
            ],
            diagnosis="One pair was rejected.",
        ),
    )
    assert any("contradiction reasons do not equal" in p for p in problems)


def test_zero_opportunity_scan_still_requires_served_diagnostics() -> None:
    client = _FakeClient(
        {
            "/arbitrage": {"batch_id": None, "matching_diagnostics": None},
            "/system": {
                "data": {
                    "jobs": [
                        {
                            "job_name": "arbitrage",
                            "status": "success",
                            "records_written": 0,
                        }
                    ]
                }
            },
        }
    )
    problems = served_matching_diagnostics_problems(client)
    assert any("matching_diagnostics is null" in p for p in problems)


def test_no_job_run_and_no_diagnostics_is_a_valid_never_scanned_state() -> None:
    client = _FakeClient(
        {
            "/arbitrage": {"batch_id": None, "matching_diagnostics": None},
            "/system": {"data": {"jobs": []}},
        }
    )
    assert served_matching_diagnostics_problems(client) == []
