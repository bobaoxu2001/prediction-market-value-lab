"""Deterministic, explicitly bounded retention for the deployment snapshot.

Why this module exists
----------------------
The snapshot builder used to be a list of prunes chosen because each one removed
something large at the time it was written. None of them was a *bound*. The
artefact's size was therefore a coincidence: it happened to land under the ceiling
because the venues happened to list a certain number of markets and because the
pipeline happened to have run a certain number of times.

Both of those grow. On 2026-07-31 the orphaned ``market_rule_versions`` leak took
the artefact over the ceiling; the cascade fixed that one table, and the artefact
grew past the ceiling again from three different directions:

* ``backtest_runs`` / ``backtest_trades`` accumulate ten runs per pipeline
  execution and nothing ever removed one. On the database that motivated this
  module they were 150 runs and 35,406 trades - 12.9 MB of a 52.2 MB build, and
  rising by roughly 0.9 MB every day the pipeline ran.
* every market that has ever *settled* keeps a row forever, because it has a
  settlement, and settled markets were 4,356 of 6,757. Their free text alone -
  ``description`` and ``settlement_rules_raw``, neither of which any endpoint
  renders for a contract that has already resolved - was 4.8 MB.
* the live browsable universe grows with the venues' own boards.

An activity filter cannot bound any of those. ``volume_24h > 0`` removes markets
nobody traded, which is a *selection* rule, not a *budget*: if Kalshi lists twice
as many actively-traded contracts next year, the artefact is twice as big and the
pipeline fails again with every job reporting SUCCESS.

The policy
----------
Retention is expressed as budgets and applied in a fixed order, cheapest and
least destructive first:

1. **Bound the backtest history** to the newest ``max_backtest_runs`` runs. This
   is pure accumulation - a run from forty days ago is superseded research, not
   evidence anybody reads - and it is by far the largest single reclaim.
2. **Strip free text from resolved markets outside the audit set.** A settled
   contract's row is kept (it is the subject of a settlement, and dropping it
   would delete outcome history the calibration work needs) but its description
   and raw rule text are not rendered anywhere for a market that has resolved.
3. **Cap the per-market row fan-out** for ``trades`` and ``price_snapshots``.
   Both are append-only histories with no per-market bound at all.
4. Only if the artefact is *still* over target, **tighten the market budget**
   along a fixed ladder, dropping the least-traded live markets and the oldest
   resolved ones until it fits.

Step 4 is deliberately last. Steps 1-3 removed 22 MB from the build that
motivated this module and dropped no market at all, so on today's data the ladder
never engages; it exists so that continued growth degrades *coverage*, in a
declared order, instead of failing the pipeline.

Determinism
-----------
Every ordering used to choose survivors ends in a unique tiebreak (``id``), every
budget is a fixed integer, and nothing reads the clock or a random source. The
same operational database produces byte-identical output, which is what lets the
manifest carry a content hash that means something.

Failing closed
--------------
If the tightest rung on the ladder still exceeds ``max_bytes``, this raises
:class:`SnapshotTooLargeError` and the build fails. The ceiling is not raised to
make a build pass: the artefact ships inside a serverless function bundle, and an
artefact that does not fit is not a validation problem to be relaxed, it is a
deployment that would not work.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Hard ceiling on the uncompressed SQLite bytes.
#:
#: GitHub warns above 50 MB and a Vercel function bundle is capped well below
#: that. ``scripts/validate_snapshot.py`` imports this rather than declaring its
#: own copy, so the builder's target and the validator's gate cannot drift apart -
#: which they would, silently, the first time somebody adjusted one of them.
MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024

#: What the builder aims for, leaving ~20% headroom under the ceiling.
#:
#: The gap is not slack for its own sake. VACUUM's output size depends on page
#: packing, and a build that lands one kilobyte under a hard gate is a build that
#: fails on the next run for no reason a reader could act on. Tightening happens
#: at the target; failure happens at the ceiling.
BUILD_TARGET_BYTES = 32 * 1024 * 1024


class SnapshotTooLargeError(RuntimeError):
    """The artefact exceeds the ceiling even at the tightest retention budget."""


#: Child tables keyed by ``market_id`` that must be reduced to the surviving
#: market set after any market prune.
#:
#: A table missing from here is an unbounded leak: the pruned market never comes
#: back, so nothing ever deletes its rows, and every run adds another generation
#: of orphans. That is precisely how ``market_rule_versions`` grew to 9.75 MB of
#: a 10.15 MB table while looking, from the outside, like ordinary growth.
MARKET_SCOPED_TABLES: tuple[str, ...] = (
    "outcomes",
    "market_rules",
    "trades",
    "price_snapshots",
    "market_rule_versions",
    "model_predictions",
    "orderbook_snapshots",
    "evidence_items",
)

#: Markets whose presence is not negotiable, at any budget.
#:
#: These are the audit trail. ``recommendation_snapshots`` is what the public
#: track record reads; ``recommendations`` is what settlement grading resolves
#: through. Dropping either to save bytes would be exactly the trade this
#: repository exists to refuse - the artefact would get smaller and the record of
#: what was claimed, and how it turned out, would get shorter.
_AUDIT_MARKETS_SQL = """
    SELECT market_id FROM recommendations WHERE market_id IS NOT NULL
    UNION
    SELECT market_id FROM recommendation_snapshots WHERE market_id IS NOT NULL
"""

#: Statuses that mean the contract has resolved and will never quote again.
#:
#: Named explicitly rather than written as ``status != 'open'``: the vocabulary
#: also contains ``paused`` and ``unknown``, and a market whose status could not
#: be read is not one whose free text should be stripped on the assumption that
#: it resolved.
_RESOLVED_STATUSES = ("settled", "closed")


@dataclass(frozen=True)
class RetentionPolicy:
    """Every bound the builder applies, in one reviewable object."""

    #: Build target. Tightening stops as soon as the artefact is at or under it.
    target_bytes: int = BUILD_TARGET_BYTES
    #: Hard ceiling. Exceeding it after the last ladder rung is a build failure.
    max_bytes: int = MAX_SNAPSHOT_BYTES

    #: Newest N backtest runs kept, with their trades. Ten runs are written per
    #: pipeline execution, so 30 is three executions of comparable history -
    #: enough to see a strategy's numbers move, far short of a permanent archive.
    max_backtest_runs: int = 30

    #: Public trade prints kept per market. The detail page renders a short
    #: recent tape; nothing reads deeper than this.
    max_trades_per_market: int = 20

    #: Price observations kept per market, for the detail page's sparkline.
    max_price_snapshots_per_market: int = 12

    #: The tightening ladder: (live market budget, resolved market budget).
    #:
    #: Rung 0 is generous enough that today's data never reaches rung 1. Each
    #: rung is a strict subset of the one before it, because the orderings that
    #: pick survivors are total and the budgets only shrink - so tightening can
    #: continue deleting from the same database rather than rebuilding, and the
    #: result is still exactly what a from-scratch build at that rung would give.
    market_ladder: tuple[tuple[int, int], ...] = (
        (4000, 6000),
        (3000, 4000),
        (2000, 3000),
        (1600, 2000),
        (1200, 1200),
        (800, 600),
        (400, 300),
    )

    def __post_init__(self) -> None:
        if self.target_bytes > self.max_bytes:
            raise ValueError("retention target must not exceed the hard ceiling")
        if not self.market_ladder:
            raise ValueError("the ladder needs at least one rung")
        live = [rung[0] for rung in self.market_ladder]
        resolved = [rung[1] for rung in self.market_ladder]
        if live != sorted(live, reverse=True) or resolved != sorted(
            resolved, reverse=True
        ):
            # Non-monotonic rungs would break the "keep deleting from the same
            # database" optimisation: a later rung could need a market an earlier
            # one already removed, and the result would silently stop matching a
            # from-scratch build at that rung.
            raise ValueError("ladder rungs must be non-increasing")


DEFAULT_RETENTION = RetentionPolicy()


@dataclass
class RetentionStep:
    """One applied bound and what it cost, for the manifest."""

    name: str
    detail: str
    bytes_after: int


@dataclass
class RetentionOutcome:
    """The full record of how the artefact was bounded.

    Written into the manifest so an artefact can answer "why is this market not
    in here?" without anybody re-running the build. A retention decision that is
    not recorded is indistinguishable from a bug.
    """

    policy: RetentionPolicy
    bytes_before: int = 0
    bytes_after: int = 0
    steps: list[RetentionStep] = field(default_factory=list)
    #: Index into ``policy.market_ladder`` of the rung that was sufficient.
    ladder_rung_applied: int = 0
    #: True when step 1-3 alone met the target and no market was budget-dropped.
    ladder_engaged: bool = False
    markets_before: int = 0
    markets_after: int = 0
    audit_markets: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": {
                "target_bytes": self.policy.target_bytes,
                "max_bytes": self.policy.max_bytes,
                "max_backtest_runs": self.policy.max_backtest_runs,
                "max_trades_per_market": self.policy.max_trades_per_market,
                "max_price_snapshots_per_market": (
                    self.policy.max_price_snapshots_per_market
                ),
                "market_ladder": [list(rung) for rung in self.policy.market_ladder],
            },
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "markets_before": self.markets_before,
            "markets_after": self.markets_after,
            "audit_markets": self.audit_markets,
            "ladder_engaged": self.ladder_engaged,
            "ladder_rung_applied": self.ladder_rung_applied,
            "live_market_budget": (
                self.policy.market_ladder[self.ladder_rung_applied][0]
                if self.ladder_engaged
                else None
            ),
            "resolved_market_budget": (
                self.policy.market_ladder[self.ladder_rung_applied][1]
                if self.ladder_engaged
                else None
            ),
            "steps": [
                {"name": s.name, "detail": s.detail, "bytes_after": s.bytes_after}
                for s in self.steps
            ],
        }


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _count(con: sqlite3.Connection, table: str) -> int:
    if not _table_exists(con, table):
        return 0
    return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _vacuum(con: sqlite3.Connection) -> None:
    con.commit()
    con.execute("VACUUM")
    con.commit()


def bound_backtest_history(con: sqlite3.Connection, *, keep_runs: int) -> str:
    """Keep the newest ``keep_runs`` runs and delete every trade of the rest.

    Ordered by ``(created_at DESC, id DESC)``: ``created_at`` alone is not a
    total order, because a single pipeline execution writes all ten of its runs
    inside the same second and SQLite would then pick arbitrarily between them.
    """
    if not _table_exists(con, "backtest_runs"):
        return "backtest_runs absent"
    before_runs = _count(con, "backtest_runs")
    before_trades = _count(con, "backtest_trades")
    con.execute(
        """
        DELETE FROM backtest_runs WHERE id NOT IN (
            SELECT id FROM backtest_runs ORDER BY created_at DESC, id DESC LIMIT ?
        )
        """,
        (keep_runs,),
    )
    con.execute(
        "DELETE FROM backtest_trades WHERE run_id NOT IN "
        "(SELECT run_id FROM backtest_runs)"
    )
    return (
        f"backtest_runs {before_runs} -> {_count(con, 'backtest_runs')}, "
        f"backtest_trades {before_trades} -> {_count(con, 'backtest_trades')}"
    )


def strip_resolved_market_text(con: sqlite3.Connection) -> str:
    """Blank the free text on resolved markets outside the audit set.

    The row itself stays. A settled market is the subject of a settlement and of
    whatever outcome history the calibration work reads, and deleting it to save
    bytes would throw away the only record that the contract existed.

    What goes is the text: ``description`` and ``settlement_rules_raw`` are
    rendered on the market detail page for a *live* contract, where a reader is
    deciding whether the rules say what they think. For a contract that has
    already resolved they are 1.1 kB per row of prose nobody opens.

    Markets that carry a recommendation are exempt, because the case study
    renders exactly that text beside the call that was published against it.
    """
    placeholders = ", ".join("?" for _ in _RESOLVED_STATUSES)
    before = con.execute(
        f"""
        SELECT COUNT(*) FROM markets
         WHERE status IN ({placeholders})
           AND (COALESCE(description, '') != ''
                OR COALESCE(settlement_rules_raw, '') != '')
           AND id NOT IN ({_AUDIT_MARKETS_SQL})
        """,
        _RESOLVED_STATUSES,
    ).fetchone()[0]
    con.execute(
        f"""
        UPDATE markets
           SET description = '',
               settlement_rules_raw = '',
               settlement_rules_normalized = ''
         WHERE status IN ({placeholders})
           AND id NOT IN ({_AUDIT_MARKETS_SQL})
        """,
        _RESOLVED_STATUSES,
    )
    return f"stripped free text from {before} resolved markets"


def cap_per_market_rows(
    con: sqlite3.Connection, *, max_trades: int, max_price_snapshots: int
) -> str:
    """Bound the two append-only per-market histories.

    Neither had any bound. ``trades`` is one row per public print the venue
    reported and ``price_snapshots`` one per orderbook capture, so both grow for
    as long as a market stays listed, and a long-lived weekly contract
    accumulates more history than a reader will ever scroll.

    Newest-first by ``id``, which on both tables is insertion order and therefore
    a total order over rows that share an ``observed_at`` to the second.
    """
    details = []
    for table, keep in (
        ("trades", max_trades),
        ("price_snapshots", max_price_snapshots),
    ):
        if not _table_exists(con, table):
            continue
        before = _count(con, table)
        con.execute(
            f"""
            DELETE FROM "{table}" WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY market_id ORDER BY id DESC
                    ) AS position
                    FROM "{table}"
                ) WHERE position <= {int(keep)}
            )
            """
        )
        details.append(f"{table} {before} -> {_count(con, table)} (<= {keep}/market)")
    return "; ".join(details) if details else "no per-market history tables"


def cascade_to_surviving_markets(con: sqlite3.Connection) -> None:
    """Delete every child row whose market is no longer in the artefact.

    Called after *every* market prune rather than once at the end, because a
    later ladder rung measures the file size and an uncascaded intermediate state
    would make that measurement describe a database that is not the one being
    built.

    ``settlements`` is nulled rather than deleted: its foreign key is nullable by
    design and it carries ``(platform, platform_market_id)``, so the settlement
    record survives a market prune as an identifiable row. Deleting it would lose
    resolution history that nothing can reconstruct.
    """
    for table in MARKET_SCOPED_TABLES:
        if _table_exists(con, table):
            con.execute(
                f"DELETE FROM {table} WHERE market_id NOT IN (SELECT id FROM markets)"
            )
    if _table_exists(con, "market_matches"):
        con.execute(
            "DELETE FROM market_matches WHERE market_a_id NOT IN "
            "(SELECT id FROM markets) OR market_b_id NOT IN (SELECT id FROM markets)"
        )
    if _table_exists(con, "orderbook_levels"):
        con.execute(
            "DELETE FROM orderbook_levels WHERE snapshot_id NOT IN "
            "(SELECT id FROM orderbook_snapshots)"
        )
    if _table_exists(con, "settlements"):
        con.execute(
            "UPDATE settlements SET market_id = NULL "
            "WHERE market_id IS NOT NULL "
            "AND market_id NOT IN (SELECT id FROM markets)"
        )


def apply_market_budget(
    con: sqlite3.Connection, *, live_budget: int, resolved_budget: int
) -> str:
    """Reduce the market set to audit trail + budgeted live + budgeted resolved.

    Live markets are ranked by 24h volume, descending. That is a coverage
    judgement stated once: when the artefact cannot hold every board, the
    contracts a reader is most likely to look up are the ones being traded. The
    ``CAST(... AS REAL)`` is load-bearing - ``volume_24h`` is a ``Money`` column
    stored as TEXT, and SQLite orders every TEXT value above every INTEGER, so an
    uncast comparison is not a looser filter, it is no filter.

    Resolved markets are ranked by settlement time, descending. Recent history is
    what a track record and a calibration study read; a contract that settled
    last spring is not.
    """
    placeholders = ", ".join("?" for _ in _RESOLVED_STATUSES)
    before = _count(con, "markets")
    con.execute(
        f"""
        DELETE FROM markets WHERE id NOT IN (
            {_AUDIT_MARKETS_SQL}
            UNION
            SELECT id FROM (
                SELECT id FROM markets
                 WHERE status NOT IN ({placeholders})
                 ORDER BY CAST(COALESCE(volume_24h, '0') AS REAL) DESC, id ASC
                 LIMIT {int(live_budget)}
            )
            UNION
            SELECT id FROM (
                SELECT id FROM markets
                 WHERE status IN ({placeholders})
                 ORDER BY COALESCE(
                     actual_settlement_time, close_time, created_at
                 ) DESC, id DESC
                 LIMIT {int(resolved_budget)}
            )
        )
        """,
        _RESOLVED_STATUSES * 2,
    )
    cascade_to_surviving_markets(con)
    return (
        f"markets {before} -> {_count(con, 'markets')} "
        f"(live <= {live_budget}, resolved <= {resolved_budget})"
    )


def apply_retention(
    target: Path, *, policy: RetentionPolicy = DEFAULT_RETENTION
) -> RetentionOutcome:
    """Bound ``target`` in place and return what was done.

    Opens its own connection because every step must be followed by a VACUUM and
    a ``stat()`` - the file size only reflects a delete after the pages are
    reclaimed, and a tightening loop that measured the un-vacuumed file would
    tighten forever.
    """
    target = Path(target)
    con = sqlite3.connect(target)
    outcome = RetentionOutcome(policy=policy, bytes_before=target.stat().st_size)
    try:
        outcome.markets_before = _count(con, "markets")
        outcome.audit_markets = int(
            con.execute(
                f"SELECT COUNT(*) FROM markets WHERE id IN ({_AUDIT_MARKETS_SQL})"
            ).fetchone()[0]
        )

        # Thunks, not a tuple of already-computed pairs. Building the pairs
        # eagerly runs all three deletes before the loop measures anything, and
        # every step then reports the same final size - which is worse than no
        # per-step accounting at all, because the numbers look like evidence.
        steps: tuple[tuple[str, Any], ...] = (
            (
                "backtest_history",
                lambda: bound_backtest_history(
                    con, keep_runs=policy.max_backtest_runs
                ),
            ),
            ("resolved_market_text", lambda: strip_resolved_market_text(con)),
            (
                "per_market_history",
                lambda: cap_per_market_rows(
                    con,
                    max_trades=policy.max_trades_per_market,
                    max_price_snapshots=policy.max_price_snapshots_per_market,
                ),
            ),
        )
        for name, run_step in steps:
            detail = run_step()
            _vacuum(con)
            outcome.steps.append(
                RetentionStep(name, detail, target.stat().st_size)
            )

        size = target.stat().st_size
        for rung, (live_budget, resolved_budget) in enumerate(policy.market_ladder):
            if size <= policy.target_bytes:
                break
            outcome.ladder_engaged = True
            outcome.ladder_rung_applied = rung
            detail = apply_market_budget(
                con, live_budget=live_budget, resolved_budget=resolved_budget
            )
            _vacuum(con)
            size = target.stat().st_size
            outcome.steps.append(
                RetentionStep(f"market_budget_rung_{rung}", detail, size)
            )

        outcome.markets_after = _count(con, "markets")
        outcome.bytes_after = size
    finally:
        con.close()

    if outcome.bytes_after > policy.max_bytes:
        raise SnapshotTooLargeError(
            f"the artefact is {outcome.bytes_after / 1e6:.1f} MB after the tightest "
            f"retention budget, above the {policy.max_bytes / 1e6:.1f} MB ceiling. "
            "The ceiling is a deployment constraint, not a validation preference: "
            "raising it ships a bundle that does not fit. Either the ladder needs "
            "a tighter rung or a table has started growing that no budget covers - "
            "check the per-step sizes in the manifest's retention block."
        )
    return outcome
