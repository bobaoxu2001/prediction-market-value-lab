"""The post-deploy smoke check must fail on the outage it was written for.

PR #4 shipped with green CI, a successful Vercel build and a READY deployment,
and every production API route returned FastAPI's own 404. A check that only
asked "did the build succeed" would have passed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from postdeploy_smoke import (  # noqa: E402
    API_ROUTES,
    WEB_ROUTES,
    Failure,
    SmokeReport,
    check_routes,
    check_semantics,
)


def _stub(monkeypatch, responses: dict[str, tuple[int, str]]) -> None:
    import postdeploy_smoke

    def fake_get(url: str, timeout: int = 45, attempts: int = 4):  # noqa: ANN202, ARG001
        for suffix, response in responses.items():
            if url.endswith(suffix):
                return response
        return responses.get("*", (200, "{}"))

    monkeypatch.setattr(postdeploy_smoke, "_get", fake_get)


class TestRouteFailuresAreCategorised:
    def test_a_uniform_404_is_caught(self, monkeypatch) -> None:  # noqa: ANN001
        """The exact outage: FastAPI answering, every route missing."""
        _stub(monkeypatch, {"*": (404, '{"detail":"Not Found"}')})
        report = SmokeReport(expected_commit="abc")

        check_routes("https://api", API_ROUTES, expect_json=True, report=report)

        assert len(report.errors) == len(API_ROUTES)
        assert all(e["category"] == Failure.UNEXPECTED_404 for e in report.errors)

    def test_a_5xx_is_categorised_separately(self, monkeypatch) -> None:  # noqa: ANN001
        _stub(monkeypatch, {"*": (503, "upstream error")})
        report = SmokeReport(expected_commit="abc")
        check_routes("https://api", ("/health",), expect_json=True, report=report)
        assert report.errors[0]["category"] == Failure.UNEXPECTED_5XX

    def test_html_where_json_belongs_is_caught(self, monkeypatch) -> None:  # noqa: ANN001
        """A platform error page can return 200 and still be broken."""
        _stub(monkeypatch, {"*": (200, "<!doctype html><title>Error</title>")})
        report = SmokeReport(expected_commit="abc")
        check_routes("https://api", ("/system",), expect_json=True, report=report)
        assert report.errors[0]["category"] == Failure.INVALID_JSON

    def test_a_transport_failure_is_not_a_404(self, monkeypatch) -> None:  # noqa: ANN001
        _stub(monkeypatch, {"*": (0, "TimeoutError")})
        report = SmokeReport(expected_commit="abc")
        check_routes("https://api", ("/health",), expect_json=True, report=report)
        assert report.errors[0]["category"] == Failure.API_UNAVAILABLE

    def test_healthy_routes_produce_no_errors(self, monkeypatch) -> None:  # noqa: ANN001
        _stub(monkeypatch, {"*": (200, '{"ok":true}')})
        report = SmokeReport(expected_commit="abc")
        check_routes("https://api", API_ROUTES, expect_json=True, report=report)
        assert report.errors == []
        assert len(report.routes) == len(API_ROUTES)

    def test_web_failures_are_labelled_web(self, monkeypatch) -> None:  # noqa: ANN001
        _stub(monkeypatch, {"*": (0, "reset")})
        report = SmokeReport(expected_commit="abc")
        check_routes("https://web", WEB_ROUTES, expect_json=False, report=report)
        assert all(e["category"] == Failure.WEB_UNAVAILABLE for e in report.errors)


class TestSnapshotSemantics:
    def _data(self, **overrides):  # noqa: ANN202
        base = {
            "runtime_mode": "read_only_snapshot",
            "trading_execution_enabled": False,
            "deployment": {"commit_ref": "main"},
        }
        base.update(overrides)
        return base

    def test_a_healthy_snapshot_deployment_passes(self) -> None:
        report = SmokeReport(expected_commit="abc")
        check_semantics(self._data(), report)
        assert report.errors == []
        assert report.snapshot_mode is True

    def test_a_non_snapshot_runtime_mode_fails(self) -> None:
        report = SmokeReport(expected_commit="abc")
        check_semantics(self._data(runtime_mode="continuous_live_pipeline"), report)
        assert any(e["category"] == Failure.SNAPSHOT_SEMANTICS for e in report.errors)

    def test_trading_execution_enabled_in_production_fails(self) -> None:
        """This platform has no execution path; production claiming otherwise is
        a red alert, not a warning."""
        report = SmokeReport(expected_commit="abc")
        check_semantics(self._data(trading_execution_enabled=True), report)
        assert any("trading execution" in e["detail"] for e in report.errors)

    def test_a_non_main_ref_fails(self) -> None:
        report = SmokeReport(expected_commit="abc")
        check_semantics(self._data(deployment={"commit_ref": "some-branch"}), report)
        assert any("commit_ref" in e["detail"] for e in report.errors)

    def test_a_missing_data_block_fails(self) -> None:
        report = SmokeReport(expected_commit="abc")
        check_semantics(None, report)
        assert report.errors


class TestCommitPolling:
    def test_it_fails_when_the_commit_never_arrives(self, monkeypatch) -> None:  # noqa: ANN001
        """A healthy OLD deployment answering while the new one silently failed to
        replace it is drift, not health."""
        import postdeploy_smoke

        monkeypatch.setattr(postdeploy_smoke, "_api_commit", lambda base: ("old000000000", {}))
        monkeypatch.setattr(postdeploy_smoke, "_web_commit", lambda base: "old000000000")
        monkeypatch.setattr(postdeploy_smoke.time, "sleep", lambda s: None)

        report = SmokeReport(expected_commit="new111111111")
        # A short but non-zero window, so the poll body runs at least once and the
        # observed commits are recorded before the deadline expires.
        ok = postdeploy_smoke.wait_for_commit(
            "a", "w", "new111111111", report, minutes=0.001
        )

        assert ok is False
        assert report.api_commit == "old000000000"
        assert any(e["category"] == Failure.COMMIT_MISMATCH for e in report.errors)

    def test_it_succeeds_when_both_serve_the_pushed_commit(self, monkeypatch) -> None:  # noqa: ANN001
        import postdeploy_smoke

        monkeypatch.setattr(postdeploy_smoke, "_api_commit", lambda base: ("abc123abc123ff", {}))
        monkeypatch.setattr(postdeploy_smoke, "_web_commit", lambda base: "abc123abc123")

        report = SmokeReport(expected_commit="abc123abc123ff")
        assert postdeploy_smoke.wait_for_commit("a", "w", "abc123abc123ff", report, minutes=1)


class TestReportShape:
    def test_the_report_carries_every_required_field(self) -> None:
        payload = SmokeReport(expected_commit="abc").as_dict()
        for field in ("expected_commit", "api_commit", "web_commit", "routes", "parity", "snapshot_mode", "errors"):
            assert field in payload, field
        json.dumps(payload)
