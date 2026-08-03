"""Build the daily Founding Pilot digest from a validated Snapshot.

Deliberately not a second scoring engine. Candidates are reconstructed from the
Snapshot and then priced by `value.ranking.build_candidate` and admitted by
`value.ranking.passes_gates` - the same code that decides what the public site
shows. If this module had its own idea of "clears the bar", the paid digest and
the free site would eventually disagree, and the paid one would be the wrong one.

Two design choices are load-bearing.

**The funnel is the product.** A report with zero candidates is not an empty
report; it is a report whose finding is "nothing cleared the bar, and here is how
many things were examined and where each one stopped". That is the only version
of this product that is honest on the days - most days - when efficiently priced
venues offer nothing. So `select_candidates` returns the stage counts and the
top rejection reasons whether or not it returns candidates.

**Three, not ten.** The public site ranks a Top-10. A daily read that a person
acts on is a different artefact from a leaderboard: past three, the marginal
entry is there to fill the page rather than because it earned a place. The cap
is enforced here rather than left to the caller.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmvl_shared.enums import Category, DataProvenance, MarketStatus, Platform, Side
from pmvl_shared.schemas import BookLevel, FairProbability, NormalizedMarket, OrderBook, ValueCandidate
from pmvl_shared.snapshot_artifact import resolve_snapshot_path
from pmvl_shared.timeutil import ensure_utc, horizons_for

from ..value.ranking import RankingConfig, build_candidate, passes_gates
from .gate import GateResult

#: The published cap. Three is a read; ten is a leaderboard.
MAX_CANDIDATES = 3

#: How many near misses to show. Enough to be informative, few enough that the
#: watchlist cannot be mistaken for the recommendation list.
MAX_WATCHLIST = 10

#: Spelled out once so the funnel, the rejection tally and the tests agree.
MISSING_RESOLUTION_TIME = (
    "No expected resolution time recorded, so holding cost cannot be computed"
)


@dataclass
class FunnelStage:
    label: str
    count: int
    note: str


@dataclass
class RejectionReason:
    reason: str
    count: int


@dataclass
class Candidate:
    """One published candidate, with every figure the reader needs to check it."""

    market_id: int
    platform: str
    platform_market_id: str
    title: str
    side: str
    category: str

    #: VWAP of the ask ladder at reference size - not a last trade or a midpoint.
    executable_ask: Decimal
    executable_size: Decimal
    all_in_cost: Decimal
    cost_components: list[tuple[str, Decimal | None]]

    market_implied_probability: Decimal | None
    #: The only figure that can argue the market is wrong. `None` means there is
    #: no independent estimate, which is not a probability of zero.
    independent_probability: Decimal | None
    #: The independent lower bound after reliability, freshness and model-risk
    #: penalties. Eligibility is decided on this one.
    decision_adjusted_probability: Decimal | None
    probability_interval: tuple[Decimal, Decimal]
    model_confidence: Decimal

    #: Conservative net EV per contract, after fees, slippage, transfer and
    #: capital cost. This is the number the admission gate uses.
    net_edge_after_costs: Decimal
    net_roi: Decimal
    liquidity_usd: Decimal | None
    spread: Decimal | None
    position_cap_contracts: Decimal

    resolution_date: datetime | None
    settlement_source: str
    rules_risk: list[str]
    invalidation_conditions: list[str]
    risk_flags: list[str]


@dataclass
class WatchlistEntry:
    """A market that was examined and is explicitly NOT actionable.

    The watchlist exists so the reader can see the near misses without being
    invited to treat them as opportunities. Every entry therefore carries the
    reason it is not actionable, and none of them carries a net edge presented as
    a number to act on: quoting an edge for something that failed the gate is how
    a "diagnostic" list quietly becomes a second recommendation list.
    """

    market_id: int
    platform: str
    title: str
    side: str
    resolution_date: datetime | None
    #: Why this is not actionable. Always at least one reason.
    blocking_reasons: list[str]
    #: Context only, and labelled as such wherever it is rendered.
    executable_ask: Decimal | None = None
    liquidity_usd: Decimal | None = None
    quote_age_seconds: float | None = None


@dataclass
class DigestReport:
    """The daily report. Renders identically in Markdown, HTML and text."""

    kind: str  # "daily"
    generated_at: datetime
    gate: GateResult
    #: Whether the report was allowed to look for candidates at all. False when
    #: the gate's checks failed, and no candidates are ever present in that case.
    #: Not the same as `gate.publication_allowed`: a historical sample computes
    #: candidates (so the format can be shown) while being denied publication as
    #: current research, which the mandatory warning states in every format.
    actionable_allowed: bool
    candidates: list[Candidate] = field(default_factory=list)
    watchlist: list[WatchlistEntry] = field(default_factory=list)
    funnel: list[FunnelStage] = field(default_factory=list)
    top_rejections: list[RejectionReason] = field(default_factory=list)
    markets_examined: int = 0
    horizon: str = "7d"

    #: Actionable candidates only. The watchlist is deliberately excluded: it is
    #: diagnostic, and counting it here would overstate what the report found.
    @property
    def actionable_count(self) -> int:
        return len(self.candidates)

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)

    @property
    def headline(self) -> str:
        if not self.actionable_allowed:
            return "No research issued — the data did not meet the freshness bar"
        if not self.candidates:
            return "No actionable opportunity today"
        n = len(self.candidates)
        return f"{n} candidate{'s' if n != 1 else ''} cleared every gate"


# ------------------------------------------------------------------ loading --


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a malformed cell is a missing cell
        return None


def _time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def open_snapshot(manifest_path: Path) -> sqlite3.Connection:
    """Open the published artefact read-only.

    `resolve_snapshot_path` handles the compressed publication: the committed
    artefact is gzip, and the raw `.db` may not exist on disk at all.
    """
    db = resolve_snapshot_path(manifest_path, manifest_path.parent / "pmvl-snapshot.db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _market_from_row(row: sqlite3.Row) -> NormalizedMarket | None:
    try:
        return NormalizedMarket(
            platform=Platform(row["platform"]),
            platform_market_id=row["platform_market_id"],
            title=row["title"] or "",
            subtitle=row["subtitle"] or "",
            description=row["description"] or "",
            category=Category(row["category"]) if row["category"] else Category.OTHER,
            status=MarketStatus(row["status"]) if row["status"] else MarketStatus.UNKNOWN,
            accepting_orders=bool(row["accepting_orders"]),
            strike_type=row["strike_type"] or None,
            floor_strike=_dec(row["floor_strike"]),
            cap_strike=_dec(row["cap_strike"]),
            tick_size=_dec(row["tick_size"]) or Decimal("0.01"),
            min_order_size=_dec(row["min_order_size"]) or Decimal("1"),
            fee_rate=_dec(row["fee_rate"]) or Decimal("0"),
            maker_fee_rate=_dec(row["maker_fee_rate"]) or Decimal("0"),
            fee_type=row["fee_type"] or "",
            close_time=_time(row["close_time"]),
            event_occurrence_time=_time(row["event_occurrence_time"]),
            expected_resolution_time=_time(row["expected_resolution_time"]),
            settlement_source=row["settlement_source"] or "",
            settlement_rules_raw=row["settlement_rules_raw"] or "",
            volume_24h=_dec(row["volume_24h"]),
            provenance=DataProvenance(row["provenance"]),
        )
    except Exception:  # noqa: BLE001 - a row we cannot normalise is a row we skip
        return None


def _book_for(conn: sqlite3.Connection, market_id: int, market: NormalizedMarket) -> OrderBook | None:
    snap = conn.execute(
        "select id, observed_at, source_timestamp, provenance from orderbook_snapshots "
        "where market_id = ? order by observed_at desc limit 1",
        (market_id,),
    ).fetchone()
    if snap is None:
        return None

    sides: dict[tuple[str, bool], list[BookLevel]] = {}
    for level in conn.execute(
        "select side, is_ask, price, size from orderbook_levels "
        "where snapshot_id = ? order by level_index",
        (snap["id"],),
    ):
        price, size = _dec(level["price"]), _dec(level["size"])
        if price is None or size is None:
            continue
        sides.setdefault((level["side"], bool(level["is_ask"])), []).append(
            BookLevel(price=price, size=size)
        )

    observed = _time(snap["observed_at"])
    if observed is None:
        return None
    return OrderBook(
        platform=market.platform,
        platform_market_id=market.platform_market_id,
        observed_at=observed,
        source_timestamp=_time(snap["source_timestamp"]),
        yes_asks=sides.get(("yes", True), []),
        yes_bids=sides.get(("yes", False), []),
        no_asks=sides.get(("no", True), []),
        no_bids=sides.get(("no", False), []),
        provenance=DataProvenance(snap["provenance"]),
    )


def _fair_from_row(row: sqlite3.Row) -> FairProbability | None:
    mean, low, high = (
        _dec(row["fair_probability_mean"]),
        _dec(row["fair_probability_low"]),
        _dec(row["fair_probability_high"]),
    )
    if mean is None or low is None or high is None:
        return None
    return FairProbability(
        fair_probability_mean=mean,
        fair_probability_low=low,
        fair_probability_high=high,
        model_confidence=_dec(row["model_confidence"]) or Decimal("0"),
        data_freshness_seconds=row["data_freshness_seconds"],
        model_version=row["model_version"] or "",
        probability_explanation=row["explanation"] or "",
        category=Category(row["category"]) if row["category"] else Category.OTHER,
        has_independent_prior=bool(row["has_independent_prior"]),
        market_implied_probability=_dec(row["market_implied_probability"]),
        market_informed_probability=_dec(row["market_informed_probability"]),
        independent_probability=_dec(row["independent_probability"]),
        independent_probability_low=_dec(row["independent_probability_low"]),
        independent_probability_high=_dec(row["independent_probability_high"]),
        conservative_decision_probability=_dec(row["conservative_decision_probability"]),
    )


# ------------------------------------------------------------- rules & risk --


def _rules_risk(conn: sqlite3.Connection, market_id: int, market: NormalizedMarket) -> tuple[str, list[str]]:
    """What could make this contract settle other than as its title reads.

    Returned as plain sentences rather than codes: the reader is deciding whether
    to take a position, and "uses revised data" means nothing without the reason
    it matters.
    """
    row = conn.execute(
        "select settlement_source_name, settlement_source_url, threshold_semantics, "
        "comparator, cutoff_time, cutoff_timezone, includes_overtime, uses_revised_data "
        "from market_rules where market_id = ? limit 1",
        (market_id,),
    ).fetchone()

    source = market.settlement_source or ""
    risks: list[str] = []

    if row is None:
        return source, [
            "No normalised settlement rule is stored for this contract, so the "
            "rule text has not been machine-checked. Read the venue's own rules "
            "before acting."
        ]

    source = row["settlement_source_name"] or source
    if not source:
        risks.append("No settlement source is named, so who decides the outcome is unstated.")
    if row["uses_revised_data"]:
        risks.append(
            "Settlement uses data that can be revised after first publication, so "
            "an outcome that looks decided can still change."
        )
    if row["includes_overtime"] is not None and not row["includes_overtime"]:
        risks.append("Overtime or extra time is excluded from the settled result.")
    cutoff = _time(row["cutoff_time"])
    if cutoff is not None:
        tz = row["cutoff_timezone"] or "UTC"
        risks.append(
            f"The measurement cutoff is {cutoff.strftime('%Y-%m-%d %H:%M')} UTC "
            f"(source timezone {tz}), which may differ from the title's plain reading."
        )
    if row["threshold_semantics"] and row["comparator"]:
        risks.append(
            f"The threshold is interpreted as '{row['threshold_semantics']}' with "
            f"comparator '{row['comparator']}'. A boundary value settles on that rule, "
            "not on intuition."
        )
    if not risks:
        risks.append(
            "No specific rule hazard was detected, which is not the same as none existing. "
            "The venue's written rules remain the only authority."
        )
    return source, risks


def _invalidation_conditions(candidate: ValueCandidate, market: NormalizedMarket) -> list[str]:
    """What would make this candidate wrong before resolution.

    Stated as observable conditions rather than advice: each is something the
    reader can check themselves against the venue.
    """
    conditions = [
        f"The ask moves above {candidate.total_cost_per_contract:.4f} all-in — the edge "
        "is measured against the executable ask, and it disappears when the ask rises "
        "to the all-in cost.",
        "Resting depth on this side falls below the size you intend to take: the "
        f"edge was measured against {candidate.executable_size:.0f} contracts of "
        "visible ladder.",
        "The venue amends the settlement rules, changes the settlement source, or "
        "voids the market.",
    ]
    if candidate.spread is not None:
        conditions.append(
            f"The spread widens materially from {candidate.spread:.4f}, which usually "
            "means the quote you priced against is no longer there."
        )
    if market.expected_resolution_time is not None:
        conditions.append(
            "The underlying event occurs before the quote refreshes — a market can stay "
            "open through a settlement window after the outcome is already known."
        )
    conditions.append(
        "A newer Snapshot supersedes this one. Every figure here is fixed at the "
        "Snapshot's cutoff and does not update."
    )
    return conditions


# ---------------------------------------------------------------- selection --


def _missing_economics_fields(
    row: sqlite3.Row, book: OrderBook, conn: sqlite3.Connection, market_id: int
) -> list[str]:
    """Fields without which an edge cannot honestly be computed.

    Checked explicitly rather than left to fail somewhere downstream as a zero: a
    missing fee rate silently priced as 0 produces an edge that does not exist,
    and that is precisely the number a paid report must never invent.

    Note this reads the raw row, not the normalised market. Normalisation is
    lenient by design - `NormalizedMarket` coerces a missing fee rate to zero so
    the rest of the pipeline has a number to work with - which means by the time
    a market reaches here, "no fee recorded" and "genuinely zero fees" look
    identical. Only the row still distinguishes them, and for a paid report the
    distinction is the whole point.
    """
    missing: list[str] = []
    if row["fee_rate"] is None:
        missing.append("no fee rate is recorded, so the all-in cost cannot be computed")
    if not (book.yes_asks or book.no_asks):
        missing.append("the order book carries no ask levels to price against")
    rule = conn.execute(
        "select 1 from market_rules where market_id = ? limit 1", (market_id,)
    ).fetchone()
    if rule is None:
        missing.append(
            "no normalised settlement rule is stored, so rules risk has not been checked"
        )
    return missing


def select_candidates(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    horizon: str = "7d",
    config: RankingConfig | None = None,
    limit: int = MAX_CANDIDATES,
    candidate_quote_max_age_seconds: int = 30 * 60,
    watchlist_limit: int = MAX_WATCHLIST,
) -> tuple[
    list[Candidate], list[WatchlistEntry], list[FunnelStage], list[RejectionReason], int
]:
    """Price every eligible live market and return at most ``limit`` candidates.

    The funnel and rejection tallies are returned unconditionally: on a day with
    no candidates they are the entire report.
    """
    config = config or RankingConfig()

    total_live = conn.execute(
        "select count(*) from markets where provenance = 'live'"
    ).fetchone()[0]
    open_rows = conn.execute(
        "select * from markets where provenance = 'live' and status = 'open' "
        "and accepting_orders = 1"
    ).fetchall()

    with_book = 0
    scored = 0
    with_prior = 0
    priced = 0
    rejections: dict[str, int] = {}
    admitted: list[tuple[ValueCandidate, sqlite3.Row, NormalizedMarket]] = []
    watch: dict[int, WatchlistEntry] = {}
    stale_quote_markets = 0
    incomplete_markets = 0

    def note(market_id: int, entry: WatchlistEntry) -> None:
        """Record a near miss, merging reasons when both sides fail differently."""
        existing = watch.get(market_id)
        if existing is None:
            watch[market_id] = entry
            return
        for reason in entry.blocking_reasons:
            if reason not in existing.blocking_reasons:
                existing.blocking_reasons.append(reason)

    for row in open_rows:
        market = _market_from_row(row)
        if market is None:
            continue

        # No resolution time means no horizon to place the market in, and no
        # holding cost either - it is unpriceable rather than merely out of
        # scope. Counted rather than skipped silently, because "we hold markets
        # we cannot price" is a fact about the data the reader is entitled to.
        if market.expected_resolution_time is None:
            incomplete_markets += 1
            rejections[MISSING_RESOLUTION_TIME] = (
                rejections.get(MISSING_RESOLUTION_TIME, 0) + 1
            )
            continue

        # Horizon next: it is the cheapest remaining filter and it defines the report.
        horizons = horizons_for(market.expected_resolution_time, now=as_of)
        if horizon not in horizons:
            continue

        book = _book_for(conn, row["id"], market)
        if book is None or book.is_empty:
            continue
        with_book += 1

        # The per-candidate quote rule. A market whose own book is older than the
        # SLA can never be actionable however good its economics look, because the
        # ask the edge is measured against is no longer a price anyone can take.
        quote_age = (as_of - book.observed_at).total_seconds()
        quote_is_stale = quote_age > candidate_quote_max_age_seconds or quote_age < 0
        if quote_is_stale:
            stale_quote_markets += 1

        missing = _missing_economics_fields(row, book, conn, row["id"])
        if missing:
            incomplete_markets += 1
            note(
                row["id"],
                WatchlistEntry(
                    market_id=row["id"],
                    platform=market.platform.value,
                    title=market.title,
                    side="—",
                    resolution_date=market.expected_resolution_time,
                    blocking_reasons=[f"Not actionable: {m}." for m in missing],
                    quote_age_seconds=quote_age,
                ),
            )
            rejections["Required economics fields missing"] = (
                rejections.get("Required economics fields missing", 0) + 1
            )
            continue

        prediction = conn.execute(
            "select * from model_predictions where market_id = ? and provenance = 'live' "
            "order by created_at desc limit 1",
            (row["id"],),
        ).fetchone()
        if prediction is None:
            continue
        scored += 1

        fair = _fair_from_row(prediction)
        if fair is None:
            continue
        if not fair.has_independent_prior:
            rejections["No probability estimate independent of this market's own price"] = (
                rejections.get(
                    "No probability estimate independent of this market's own price", 0
                )
                + 1
            )
            continue
        with_prior += 1

        for side in (Side.YES, Side.NO):
            candidate = build_candidate(
                market, book, fair, side, horizon, config=config, now=as_of,
                market_id=row["id"],
            )
            if candidate is None:
                continue
            priced += 1
            ok, reason = passes_gates(candidate, config)
            if not ok:
                key = _summarise_rejection(reason)
                rejections[key] = rejections.get(key, 0) + 1
                note(
                    row["id"],
                    WatchlistEntry(
                        market_id=row["id"],
                        platform=market.platform.value,
                        title=market.title,
                        side=side.value,
                        resolution_date=market.expected_resolution_time,
                        blocking_reasons=[f"Not actionable: {key.lower()}."],
                        executable_ask=candidate.entry_price,
                        liquidity_usd=candidate.liquidity_usd,
                        quote_age_seconds=quote_age,
                    ),
                )
                continue

            if quote_is_stale:
                # Economics cleared, but the quote behind them did not. This is
                # the case most worth surfacing and the one most dangerous to
                # publish, so it is named explicitly rather than folded into the
                # generic rejection tally.
                minutes = quote_age / 60.0
                rejections["Quote too old to be actionable"] = (
                    rejections.get("Quote too old to be actionable", 0) + 1
                )
                note(
                    row["id"],
                    WatchlistEntry(
                        market_id=row["id"],
                        platform=market.platform.value,
                        title=market.title,
                        side=side.value,
                        resolution_date=market.expected_resolution_time,
                        blocking_reasons=[
                            "Not actionable: the economics cleared, but the order book "
                            f"behind them was observed {minutes:.0f} minutes ago, past the "
                            f"{candidate_quote_max_age_seconds // 60}-minute limit for an "
                            "actionable quote."
                        ],
                        executable_ask=candidate.entry_price,
                        liquidity_usd=candidate.liquidity_usd,
                        quote_age_seconds=quote_age,
                    ),
                )
                continue

            admitted.append((candidate, prediction, market))

    # One side per market: YES and NO are opposite expressions of one view.
    best_per_market: dict[int, tuple[ValueCandidate, sqlite3.Row, NormalizedMarket]] = {}
    for candidate, prediction, market in admitted:
        key = candidate.market_id or -1
        existing = best_per_market.get(key)
        if existing is None or candidate.conservative_net_ev > existing[0].conservative_net_ev:
            best_per_market[key] = (candidate, prediction, market)

    ranked = sorted(
        best_per_market.values(), key=lambda t: t[0].conservative_net_ev, reverse=True
    )[:limit]

    candidates = [
        _to_candidate(conn, candidate, prediction, market)
        for candidate, prediction, market in ranked
    ]

    watchlist = sorted(
        watch.values(), key=lambda w: (w.liquidity_usd or Decimal("0")), reverse=True
    )[:watchlist_limit]

    funnel = [
        FunnelStage("Live markets in the Snapshot", total_live, "both venues, after ingest"),
        FunnelStage("Open and accepting orders", len(open_rows), "tradeable at the cutoff"),
        FunnelStage(
            f"Resolving within {horizon}, with a live book",
            with_book,
            "inside the report's horizon and quotable",
        ),
        FunnelStage(
            "Excluded: a required economics field was missing",
            incomplete_markets,
            "fee, ask ladder, resolution time or settlement rule",
        ),
        FunnelStage(
            "Flagged: own quote too old to be actionable",
            stale_quote_markets,
            "cannot be actionable regardless of economics",
        ),
        FunnelStage("Scored by a probability model", scored, "the model covers the category"),
        FunnelStage(
            "With an independent prior",
            with_prior,
            "an estimate that never saw this market's own price",
        ),
        # The unit changes here, from markets to sides, and the count therefore
        # goes UP. Saying so in the row keeps the table from looking broken.
        FunnelStage(
            "Sides priced against the ask ladder",
            priced,
            "YES and NO are priced separately, so this counts sides, not markets",
        ),
        FunnelStage(
            "Sides clearing every admission gate", len(admitted), "net of all costs"
        ),
        FunnelStage(
            "Distinct markets admitted",
            len(best_per_market),
            "one side per market: YES and NO express the same view",
        ),
        FunnelStage("Published as actionable", len(candidates), f"capped at {limit}"),
        FunnelStage(
            "Shown on the watchlist (not actionable)",
            len(watchlist),
            f"nearest misses, capped at {watchlist_limit}",
        ),
    ]

    top = [
        RejectionReason(reason=reason, count=count)
        for reason, count in sorted(rejections.items(), key=lambda kv: kv[1], reverse=True)
    ][:6]

    return candidates, watchlist, funnel, top, len(open_rows)


def _summarise_rejection(reason: str) -> str:
    """Collapse per-market detail into a countable class of rejection."""
    if "conservative net EV" in reason:
        return "Conservative net EV did not clear the required margin after all costs"
    if "depth" in reason:
        return "Resting depth below the minimum executable size"
    if "spread" in reason:
        return "Spread too wide to price against"
    if "confidence" in reason:
        return "Model confidence below the minimum"
    if "independent prior" in reason:
        return "No probability estimate independent of this market's own price"
    if "already occurred" in reason:
        return "The underlying event has already occurred; the market knows the outcome"
    if "executable size" in reason:
        return "No executable size on the ladder"
    return reason


def _to_candidate(
    conn: sqlite3.Connection,
    candidate: ValueCandidate,
    prediction: sqlite3.Row,
    market: NormalizedMarket,
) -> Candidate:
    source, rules_risk = _rules_risk(conn, candidate.market_id or -1, market)
    cost = candidate.cost
    components: list[tuple[str, Decimal | None]] = []
    for name in (
        "entry_cost",
        "fee",
        "rounding",
        "slippage",
        "transfer_cost",
        "capital_cost",
    ):
        value = getattr(cost, name, None)
        if value is not None:
            components.append((name.replace("_", " "), value))

    return Candidate(
        market_id=candidate.market_id or -1,
        platform=candidate.platform.value,
        platform_market_id=candidate.platform_market_id,
        title=candidate.title,
        side=candidate.side.value,
        category=candidate.fair.category.value,
        executable_ask=candidate.entry_price,
        executable_size=candidate.executable_size,
        all_in_cost=candidate.total_cost_per_contract,
        cost_components=components,
        market_implied_probability=candidate.fair.market_implied_probability,
        independent_probability=candidate.fair.independent_probability,
        decision_adjusted_probability=(
            candidate.fair.conservative_decision_probability
            or candidate.fair.fair_probability_low
        ),
        probability_interval=(
            candidate.fair.fair_probability_low,
            candidate.fair.fair_probability_high,
        ),
        model_confidence=candidate.fair.model_confidence,
        net_edge_after_costs=candidate.conservative_net_ev,
        net_roi=candidate.net_roi,
        liquidity_usd=candidate.liquidity_usd,
        spread=candidate.spread,
        position_cap_contracts=candidate.recommended_position_cap,
        resolution_date=candidate.expected_resolution_time,
        settlement_source=source,
        rules_risk=rules_risk,
        invalidation_conditions=_invalidation_conditions(candidate, market),
        risk_flags=list(candidate.risk_flags),
    )


# ------------------------------------------------------------------- report --


def build_daily_digest(
    manifest_path: Path,
    gate_result: GateResult,
    *,
    horizon: str = "7d",
    limit: int = MAX_CANDIDATES,
) -> DigestReport:
    """Assemble the daily report, honouring the gate.

    Two different questions are being asked of the gate here, and conflating them
    is what would break historical samples:

    - ``checks_passed`` - were the artefact's hashes, integrity, jobs and
      freshness sound *at the moment it was evaluated*? If not, no Snapshot query
      runs at all, because the cheapest way to guarantee bad data is never
      published is to never read it.
    - ``publication_allowed`` - may the result be presented as **current**
      actionable research? This is what a historical sample is denied, and the
      renderers stamp the warning accordingly.

    A historical sample therefore reads the Snapshot and shows the full report
    format, while every rendered format says in its first lines that it is not
    current research.
    """
    if not gate_result.checks_passed:
        return DigestReport(
            kind="daily",
            generated_at=gate_result.as_of,
            gate=gate_result,
            actionable_allowed=False,
            horizon=horizon,
        )

    conn = open_snapshot(manifest_path)
    try:
        # Candidates are evaluated at the Snapshot's cutoff, which is when its
        # quotes were true. The gate has already decided whether that cutoff is
        # recent enough for the report to be published at all; using `as_of` here
        # instead would age every book by the same amount twice.
        reference = gate_result.source_data_cutoff or gate_result.as_of
        candidates, watchlist, funnel, rejections, examined = select_candidates(
            conn,
            as_of=reference,
            horizon=horizon,
            limit=limit,
            candidate_quote_max_age_seconds=(
                gate_result.sla.candidate_quote_max_age_seconds
            ),
        )
    finally:
        conn.close()

    return DigestReport(
        kind="daily",
        generated_at=gate_result.as_of,
        gate=gate_result,
        actionable_allowed=True,
        candidates=candidates,
        watchlist=watchlist,
        funnel=funnel,
        top_rejections=rejections,
        markets_examined=examined,
        horizon=horizon,
    )
