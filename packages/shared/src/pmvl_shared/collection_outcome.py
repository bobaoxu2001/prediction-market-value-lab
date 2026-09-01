"""What a collection cycle actually did, as distinct from whether it returned.

A scheduler function that returns normally is not evidence that collection
happened. On 2026-08-31 `research_books` reported ``success`` with
``records_written = 0`` for six consecutive cycles while the Kalshi circuit was
open and nothing at all was being archived - through the two hours that
contained six frozen decision instants. Collection health read those cycles as
healthy, because from the scheduler's side they were: the function ran, raised
nothing, and wrote the number zero.

Zero rows is a legitimate outcome. Outside an issuance window, or on a market
whose book has not moved, there is genuinely nothing to write, and redefining
every zero as a failure would trade one blind spot for a louder one. The
distinction that matters is *why* the number is zero, and that is knowable at
the moment of collection: the job knows whether it reached the venue.

This module only classifies. It has no I/O, no clock and no database, it never
decides whether a station-day is admissible, and nothing it returns reaches the
research protocol - a collection outcome is an operational fact about a poll.
"""

from __future__ import annotations

from enum import Enum


class CollectionOutcome(str, Enum):
    """Why a collection cycle produced the row count it produced."""

    #: Reached the venue and archived at least one book.
    SUCCESS_WITH_ROWS = "SUCCESS_WITH_ROWS"
    #: Reached the venue; there was legitimately nothing new to write.
    SUCCESS_VALID_NOOP = "SUCCESS_VALID_NOOP"
    #: The provider's circuit breaker was open, so no request was made.
    UPSTREAM_CIRCUIT_OPEN = "UPSTREAM_CIRCUIT_OPEN"
    #: A request was made and failed.
    UPSTREAM_FETCH_FAILED = "UPSTREAM_FETCH_FAILED"
    #: The provider gave up after its retry budget.
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    #: Books were fetched but could not be persisted.
    DB_WRITE_FAILED = "DB_WRITE_FAILED"
    #: Nothing was in scope to collect.
    NO_MARKETS_CONFIGURED = "NO_MARKETS_CONFIGURED"

    @property
    def is_upstream_failure(self) -> bool:
        """Whether collection health should stop calling this healthy."""
        return self in (
            CollectionOutcome.UPSTREAM_CIRCUIT_OPEN,
            CollectionOutcome.UPSTREAM_FETCH_FAILED,
            CollectionOutcome.RETRY_EXHAUSTED,
            CollectionOutcome.DB_WRITE_FAILED,
        )


def classify_collection(
    *,
    books_archived: int,
    markets_seen: int,
    errors: "list[str] | tuple[str, ...]" = (),
) -> CollectionOutcome:
    """Classify one cycle from what it observed.

    Failure states are checked before the row count, because a cycle that never
    reached the venue wrote zero rows for a reason that has nothing to do with
    the market being quiet. Order among the failure states runs from most to
    least specific.
    """
    joined = " | ".join(errors).lower()

    if "circuit" in joined:
        return CollectionOutcome.UPSTREAM_CIRCUIT_OPEN
    if "exhausted retries" in joined:
        return CollectionOutcome.RETRY_EXHAUSTED
    if "locked" in joined or "operationalerror" in joined:
        return CollectionOutcome.DB_WRITE_FAILED
    if joined:
        return CollectionOutcome.UPSTREAM_FETCH_FAILED
    if markets_seen == 0:
        return CollectionOutcome.NO_MARKETS_CONFIGURED
    if books_archived > 0:
        return CollectionOutcome.SUCCESS_WITH_ROWS
    return CollectionOutcome.SUCCESS_VALID_NOOP
