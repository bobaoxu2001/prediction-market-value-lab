"""An unreachable venue must never read as an empty market universe.

2026-09-01T04:11-05:15Z: Kalshi's circuit opened, 84 configured research markets
existed throughout, zero books were archived for 64 minutes, and every
research_books run persisted:

    status  = success
    rows    = 0
    outcome = NO_MARKETS_CONFIGURED
    is_upstream_failure = False

Two stacked information losses produced that.

1. KalshiProvider.get_orderbooks catches ProviderError - and CircuitOpenError
   subclasses it - logs at DEBUG and sets ``data = None``; the per-market
   fallback likewise swallows each exception into ``log.debug`` and simply omits
   the market. The method returns ``{}`` and raises nothing, so
   ``refresh_orderbooks`` never appends to ``report.errors``.
2. ``archive_books`` increments ``markets_seen`` while iterating the FETCHED
   books, not the CONFIGURED markets. With nothing fetched it stays 0, which is
   indistinguishable from "nothing was configured".

The classifier then took its benign branch, because the only evidence it had
said there was nothing to collect.
"""

from __future__ import annotations

import pytest

from pmvl_shared.collection_outcome import CollectionOutcome, classify_collection


def test_configured_markets_with_nothing_fetched_is_an_upstream_failure() -> None:
    """The exact production contradiction. Fails before the fix."""
    outcome = classify_collection(
        books_archived=0,
        markets_seen=0,          # nothing FETCHED - the swallowed state
        configured_markets=84,   # but 84 were CONFIGURED
        errors=[],               # provider swallowed its own error
    )
    assert outcome is not CollectionOutcome.NO_MARKETS_CONFIGURED, (
        "a reachable-venue failure was reported as an empty universe"
    )
    assert outcome.is_upstream_failure, "a 64-minute outage read as healthy"


def test_circuit_open_evidence_is_named_when_available() -> None:
    outcome = classify_collection(
        books_archived=0, markets_seen=0, configured_markets=84,
        errors=["kalshi orderbooks: circuit open; 0 of 84 configured markets fetched"],
    )
    assert outcome is CollectionOutcome.UPSTREAM_CIRCUIT_OPEN
    assert outcome.is_upstream_failure


def test_genuinely_empty_universe_stays_benign() -> None:
    """Negative control A: nothing configured is still a valid no-op."""
    outcome = classify_collection(
        books_archived=0, markets_seen=0, configured_markets=0, errors=[]
    )
    assert outcome is CollectionOutcome.NO_MARKETS_CONFIGURED
    assert not outcome.is_upstream_failure


def test_normal_collection_unchanged() -> None:
    """Negative control F."""
    outcome = classify_collection(
        books_archived=84, markets_seen=84, configured_markets=84, errors=[]
    )
    assert outcome is CollectionOutcome.SUCCESS_WITH_ROWS
    assert not outcome.is_upstream_failure


@pytest.mark.parametrize(
    "errors,expected",
    [
        (["kalshi orderbooks: kalshi exhausted retries: timeout"],
         CollectionOutcome.RETRY_EXHAUSTED),
        (["OperationalError: database is locked"],
         CollectionOutcome.DB_WRITE_FAILED),
        (["kalshi orderbooks: 503 upstream"],
         CollectionOutcome.UPSTREAM_FETCH_FAILED),
    ],
)
def test_other_failure_states_survive_the_new_parameter(errors, expected) -> None:  # noqa: ANN001
    """Negative controls C/D/E/G: adding configured_markets changes nothing here."""
    outcome = classify_collection(
        books_archived=0, markets_seen=0, configured_markets=84, errors=errors
    )
    assert outcome is expected


def test_db_write_failure_is_not_blamed_on_the_provider() -> None:
    """Negative control G."""
    outcome = classify_collection(
        books_archived=0, markets_seen=84, configured_markets=84,
        errors=["OperationalError: database is locked"],
    )
    assert outcome is CollectionOutcome.DB_WRITE_FAILED


def test_partial_fetch_is_success_not_failure() -> None:
    """Some books returned means the venue was reachable."""
    outcome = classify_collection(
        books_archived=40, markets_seen=40, configured_markets=84, errors=[]
    )
    assert outcome is CollectionOutcome.SUCCESS_WITH_ROWS
    assert not outcome.is_upstream_failure


def test_runner_emits_structured_evidence_when_nothing_is_fetched() -> None:
    """The source-side half: refresh_orderbooks must not stay silent."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "packages/market-normalization/src/pmvl_markets/ingest/runner.py"
    ).read_text()
    body = src[src.index("async def refresh_orderbooks"):]
    assert "configured_markets" in body, "configured count is not propagated"
    assert "circuit_open" in body, "circuit state is not consulted"


def test_provider_swallow_point_is_documented() -> None:
    """Guard the boundary that lost the state, so a future edit re-reads this."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "packages/market-normalization/src/pmvl_markets/providers/kalshi.py"
    ).read_text()
    assert "except ProviderError" in src, (
        "the fallback contract changed; re-verify that refresh_orderbooks still "
        "detects an empty fetch, because this except is why it must"
    )
