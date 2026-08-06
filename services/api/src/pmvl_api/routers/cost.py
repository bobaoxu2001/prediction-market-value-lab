"""What a contract costs to buy, for any market that has a price.

Every other analytical endpoint here is gated on a probability estimate, and the
independence rule refuses to supply one for most markets -- correctly, and that is
why `/opportunities` is usually empty. This endpoint asks a question that does not
need a probability at all, so it has an answer for every market with a quote, every
day.

Read-only like the rest of the service: it computes from stored observations and
published fee schedules, and places no orders.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Side
from pmvl_shared.money import D
from pmvl_shared.timeutil import horizons_for

from pmvl_markets.db_models import Market
from pmvl_markets.ingest.runner import _market_from_row
from pmvl_markets.ingest.store import latest_orderbook, orderbook_from_snapshot
from pmvl_markets.pricing.cost_truth import DEFAULT_LADDER, analyse_cost

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope

router = APIRouter(prefix="/cost", tags=["cost"])

#: Largest order size the endpoint will price.
#:
#: Not a limit on what anyone may trade -- a bound on what this calculation can
#: honestly describe. Past the observed depth the answer is governed by orders that
#: were never seen, and walking a ladder that ran out returns a partial fill whose
#: cost says nothing about the rest.
MAX_SIZE = Decimal("100000")

#: What the headline `measured_cost` is built from, named so the response can carry
#: its own definition rather than relying on the reader having found the docs.
MEASURED_BASIS = (
    "Observed ask depth, the venue's published fee schedule and its documented "
    "fee-rounding rule, plus disclosed configuration assumptions for transfer "
    "amortisation and the annual capital cost of holding to resolution. The first "
    "group is source-derived; the latter two are scenario inputs, not observations."
)

MODELLED_BASIS = (
    "A flat latency pad of tick_size x SLIPPAGE_TICKS, standing in for market impact "
    "between observing a book and reaching it. It is an assumption, not a "
    "measurement, so it is excluded from the headline figure and reported beside it."
)


def _parse_size(raw: str | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        size = D(raw)
    except (InvalidOperation, ValueError):
        # `from None`: the Decimal parse error carries the raw input, and the
        # handler echoes exception text into the response body.
        raise HTTPException(400, "size must be a number") from None
    if size <= 0:
        raise HTTPException(400, "size must be greater than zero")
    if size > MAX_SIZE:
        raise HTTPException(
            400,
            f"size above {MAX_SIZE} is not priced: beyond observed depth the cost "
            "would be governed by orders that were never seen.",
        )
    return size


def _load_market(db: Session, market_id: int, mode: DataMode) -> Market:
    stmt = apply_provenance(
        select(Market).where(Market.id == market_id), Market.provenance, mode
    )
    market = db.scalar(stmt)
    if market is None:
        raise HTTPException(404, "market not found")
    return market


#: Markets sampled per category for the sector comparison.
#:
#: Capped because the comparison is between *categories*, and a category with four
#: thousand contracts should not get a more precise median than one with two
#: hundred at the cost of pricing the whole universe on every request. Taken from
#: the highest-volume end, which is also the end a reader is likely to look up.
SECTOR_SAMPLE_PER_CATEGORY = 120

#: Below this a category is reported as unmeasured rather than given a median.
#: A "sector premium" drawn from three contracts is a number about three
#: contracts.
SECTOR_MIN_SAMPLE = 8

#: Memoised sector comparisons, keyed by the parameters that determine one.
#:
#: This endpoint prices thousands of contracts per call and renders on the public
#: homepage, where it measured 5.8-13.6s in production. The result cannot change
#: between calls: in snapshot mode the artefact is opened `immutable=1`, so the
#: bytes behind this process are fixed for the process's whole lifetime, and the
#: answer computed once is the answer for every later request that instance sees.
#:
#: Deliberately not enabled outside snapshot mode. There the database is a live
#: file the pipeline writes to, and a cached sector table would be a stale one.
#: Correctness first: a slow dev server is a cost worth paying, serving yesterday's
#: numbers is not.
#:
#: Bounded by construction rather than by an eviction policy - the key space is
#: (size x side x mode), and `size` is validated before it is ever used as a key,
#: so an unbounded parameter cannot grow this without bound.
_SECTOR_CACHE: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

#: How far down the volume-ranked universe the sampler will walk.
#:
#: Comfortably more than ``categories x SECTOR_SAMPLE_PER_CATEGORY`` so no category
#: is cut short by the bound, while keeping the work proportional to what is
#: actually sampled rather than to the size of the universe.
SECTOR_SCAN_LIMIT = 4000


@router.get("/by-category")
def cost_by_category(
    size: str = Query("100", description="Order size the comparison is priced at."),
    side: Side = Query(Side.YES),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Median execution-cost premium per market category.

    The comparison the venues cannot show and no model is needed for: whether the
    contracts people most want to trade are also the expensive ones to trade.

    Reported as a median rather than a mean because the premium distribution has a
    long right tail — a handful of sub-cent contracts carry premiums in the
    hundreds of percent and would drag any average to a figure no individual
    contract is near.
    """
    requested = _parse_size(size) or Decimal("100")

    # Keyed on the size's canonical form, not the raw string and not `str()` of
    # the Decimal: Decimal keeps trailing zeros, so `str(D("100.0"))` is "100.0"
    # and would occupy a second entry for the identical comparison. `normalize`
    # collapses them, and `format(..., "f")` renders the result as "100" rather
    # than Decimal's exponent form "1E+2".
    cache_key = (
        format(requested.normalize(), "f"),
        side.value,
        mode.value,
    )
    cacheable = get_settings().snapshot_mode
    if cacheable and cache_key in _SECTOR_CACHE:
        return _sector_envelope(_SECTOR_CACHE[cache_key], mode, requested, side)

    stmt = select(Market).where(
        Market.status == "open",
        Market.accepting_orders.is_(True),
    )
    stmt = apply_provenance(stmt, Market.provenance, mode)
    # Bounded. Ordered by volume and capped per category, the tail of the universe
    # cannot change any median — but walking all of it still cost an order-book
    # lookup per row, and this endpoint renders on the homepage.
    stmt = stmt.order_by(Market.volume_24h.desc()).limit(SECTOR_SCAN_LIMIT)

    per_category: dict[str, list[Decimal]] = {}
    with_book: dict[str, int] = {}
    for row in db.scalars(stmt):
        category = row.category or "other"
        bucket = per_category.setdefault(category, [])
        if len(bucket) >= SECTOR_SAMPLE_PER_CATEGORY:
            continue

        market = _market_from_row(row)
        snapshot = latest_orderbook(db, row.id)
        book = None
        observed_at = row.quote_observed_at
        if snapshot is not None:
            candidate = orderbook_from_snapshot(db, snapshot, row)
            if not candidate.is_empty:
                book = candidate
                observed_at = snapshot.observed_at

        summary_ask = row.best_yes_ask if side == Side.YES else row.best_no_ask
        truth = analyse_cost(
            market,
            side,
            book=book,
            requested_size=requested,
            summary_ask=summary_ask,
            quote_observed_at=observed_at,
            ladder=(),
        )
        if truth is None or truth.requested is None:
            continue
        entry = truth.requested
        if entry.below_min_order_size or entry.measured_premium_ratio is None:
            continue
        bucket.append(entry.measured_premium_ratio)
        if truth.depth_known:
            with_book[category] = with_book.get(category, 0) + 1

    rows: list[dict[str, Any]] = []
    for category, ratios in per_category.items():
        if len(ratios) < SECTOR_MIN_SAMPLE:
            continue
        ordered = sorted(ratios)
        books = with_book.get(category, 0)
        rows.append(
            {
                "category": category,
                "n": len(ordered),
                "median_premium_ratio": _median(ordered),
                "p25_premium_ratio": ordered[len(ordered) // 4],
                "p75_premium_ratio": ordered[(3 * len(ordered)) // 4],
                # How much of this median rests on a real ladder. Where it is zero
                # the premium excludes depth impact entirely and is a floor, which
                # makes the category's true cost *higher* than plotted, not lower.
                "priced_from_orderbook": books,
                "depth_coverage": _ratio(books, len(ordered)),
            }
        )

    rows.sort(key=lambda r: r["median_premium_ratio"], reverse=True)
    if cacheable:
        _SECTOR_CACHE[cache_key] = rows
    return _sector_envelope(rows, mode, requested, side)


def _sector_envelope(
    rows: list[dict[str, Any]],
    mode: DataMode,
    requested: Decimal,
    side: Side,
) -> dict[str, Any]:
    """The response body, built in one place.

    A cache hit and a cache miss must be byte-identical to a caller. Building the
    envelope at both return sites is how the two quietly diverge - one of them
    gains a field, and which body a reader gets depends on whether they were the
    first request to that instance.
    """
    return envelope(
        rows,
        mode,
        priced_at_size=requested,
        side=side.value,
        basis={"measured": MEASURED_BASIS, "modelled": MODELLED_BASIS},
        note=(
            "Median measured premium over the quoted price, per category, at the "
            "stated size. Categories with fewer than "
            f"{SECTOR_MIN_SAMPLE} priceable contracts are omitted rather than "
            "given a median. Where depth_coverage is low the figure excludes "
            "order-book impact and is a floor on the true premium."
        ),
    )


def _median(ordered: list[Decimal]) -> Decimal:
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _ratio(part: int, whole: int) -> Decimal:
    if whole <= 0:
        return Decimal("0")
    return (Decimal(part) / Decimal(whole)).quantize(Decimal("0.001"))


@router.get("/{market_id}")
def market_cost(
    market_id: int,
    side: Side = Query(
        Side.YES, description="Which side of the contract is being bought."
    ),
    size: str | None = Query(
        None,
        description=(
            "Order size in contracts. Priced in addition to the standard ladder, "
            "so the response always shows how cost varies with size."
        ),
    ),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """The cost of entering ``side`` of one market, at one size and across a ladder.

    Answers, for a contract quoted at 34c: what does buying 100 of them actually
    cost, and what probability does that cost imply is needed just to break even.
    """
    requested = _parse_size(size)
    row = _load_market(db, market_id, mode)
    market = _market_from_row(row)

    # The book is preferred and the venue summary is the labelled fallback. Which
    # one was used is reported as `quote_source`, and the caveat list says what the
    # fallback cannot compute.
    snapshot = latest_orderbook(db, row.id)
    book = None
    observed_at = row.quote_observed_at
    if snapshot is not None:
        candidate = orderbook_from_snapshot(db, snapshot, row)
        if not candidate.is_empty:
            book = candidate
            observed_at = snapshot.observed_at

    summary_ask = row.best_yes_ask if side == Side.YES else row.best_no_ask

    truth = analyse_cost(
        market,
        side,
        book=book,
        requested_size=requested,
        summary_ask=summary_ask,
        quote_observed_at=observed_at,
    )
    if truth is None:
        # No ask from any source. Said plainly rather than returned as a zero cost.
        return envelope(
            {
                "market": _identity(row),
                "priced": False,
                "reason": (
                    "No ask price was observed for this side from either the order "
                    "book or the venue summary, so there is nothing to cost. This is "
                    "a gap in observation, not a statement that the contract is free "
                    "or unavailable."
                ),
            },
            mode,
        )

    payload = truth.as_dict()
    payload["market"] = _identity(row)
    payload["priced"] = True
    payload["basis"] = {
        "measured": MEASURED_BASIS,
        "modelled": MODELLED_BASIS,
        "headline": "breakeven_probability, computed from measured cost only.",
    }
    return envelope(payload, mode)


def _identity(row: Market) -> dict[str, Any]:
    horizons = horizons_for(row.expected_resolution_time)
    return {
        "id": row.id,
        "platform": row.platform,
        "platform_market_id": row.platform_market_id,
        "title": row.title,
        "subtitle": row.subtitle,
        "category": row.category,
        "status": row.status,
        "accepting_orders": row.accepting_orders,
        "tick_size": row.tick_size,
        "fee_rate": row.fee_rate,
        "fee_type": row.fee_type,
        "min_order_size": row.min_order_size,
        "expected_resolution_time": row.expected_resolution_time,
        "horizon": horizons[0] if horizons else None,
        "volume_24h": row.volume_24h,
    }


@router.get("")
def cost_index(
    limit: int = Query(50, ge=1, le=200),
    platform: str | None = Query(None),
    category: str | None = Query(None),
    side: Side = Query(Side.YES),
    size: str = Query("100", description="Order size the comparison is priced at."),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Markets ranked by how far their true cost sits above the quoted price.

    The ranking a reader cannot get from either venue: not which contract is
    cheapest, but which one costs the most *more* than it appears to. Sorted by
    measured premium as a fraction of the quoted price, so a 1c contract carrying a
    1c fee outranks a 90c contract carrying the same cent.
    """
    requested = _parse_size(size) or Decimal("100")

    stmt = select(Market).where(
        Market.status == "open",
        Market.accepting_orders.is_(True),
    )
    if platform:
        stmt = stmt.where(Market.platform == platform)
    if category:
        stmt = stmt.where(Market.category == category)
    stmt = apply_provenance(stmt, Market.provenance, mode)
    # Ordered by traded volume before pricing so the comparison is over contracts
    # someone might actually buy, then re-sorted by premium below. A scan of the
    # whole universe would price thousands of untraded markets to rank them.
    stmt = stmt.order_by(Market.volume_24h.desc()).limit(max(limit * 6, 300))

    rows = list(db.scalars(stmt))
    priced: list[dict[str, Any]] = []
    for row in rows:
        market = _market_from_row(row)
        snapshot = latest_orderbook(db, row.id)
        book = None
        observed_at = row.quote_observed_at
        if snapshot is not None:
            candidate = orderbook_from_snapshot(db, snapshot, row)
            if not candidate.is_empty:
                book = candidate
                observed_at = snapshot.observed_at
        summary_ask = row.best_yes_ask if side == Side.YES else row.best_no_ask
        truth = analyse_cost(
            market,
            side,
            book=book,
            requested_size=requested,
            summary_ask=summary_ask,
            quote_observed_at=observed_at,
            ladder=(),  # the index needs the requested size only
        )
        if truth is None or truth.requested is None:
            continue
        entry = truth.requested
        # A ranking is a comparison, and a size the venue would reject is not
        # comparable to one it would accept. Polymarket's 5-contract minimum means
        # a 1-lot amortises the whole fixed bridge cost over one contract, which
        # topped this list with a 50,000% premium on an order nobody can place.
        if entry.below_min_order_size:
            continue
        priced.append(
            {
                "market": _identity(row),
                "quote_source": truth.quote_source,
                "depth_known": truth.depth_known,
                "is_stale": truth.is_stale,
                "quote_observed_at": truth.quote_observed_at,
                "nominal_price": entry.nominal_price,
                "measured_cost": entry.measured_cost,
                "measured_premium": entry.measured_premium,
                "measured_premium_ratio": entry.measured_premium_ratio,
                "breakeven_probability": entry.breakeven_probability,
                "filled_size": entry.filled_size,
                "fully_filled": entry.fully_filled,
            }
        )

    priced.sort(
        key=lambda r: r["measured_premium_ratio"] or Decimal("0"), reverse=True
    )
    return envelope(
        priced[:limit],
        mode,
        priced_at_size=requested,
        side=side.value,
        basis={"measured": MEASURED_BASIS, "modelled": MODELLED_BASIS},
        note=(
            "Ranked by measured premium as a fraction of the quoted price. Markets "
            "without an observed order book are priced from the venue summary and "
            "carry depth_known=false; their premium excludes depth impact and is "
            "therefore a floor."
        ),
    )


@router.get("/{market_id}/ladder")
def cost_ladder(
    market_id: int,
    side: Side = Query(Side.YES),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Cost per contract at each standard size, for charting the size effect.

    Kept separate from the detail endpoint so a chart can poll it without pulling
    the full payload.
    """
    row = _load_market(db, market_id, mode)
    market = _market_from_row(row)
    snapshot = latest_orderbook(db, row.id)
    book = None
    observed_at = row.quote_observed_at
    if snapshot is not None:
        candidate = orderbook_from_snapshot(db, snapshot, row)
        if not candidate.is_empty:
            book = candidate
            observed_at = snapshot.observed_at
    summary_ask = row.best_yes_ask if side == Side.YES else row.best_no_ask
    truth = analyse_cost(
        market,
        side,
        book=book,
        summary_ask=summary_ask,
        quote_observed_at=observed_at,
        ladder=DEFAULT_LADDER,
    )
    if truth is None:
        raise HTTPException(404, "no observed ask price for this side")
    return envelope(
        {
            "market": _identity(row),
            "quote_source": truth.quote_source,
            "depth_known": truth.depth_known,
            "nominal_price": truth.nominal_price,
            "max_fillable_size": truth.max_fillable_size,
            "ladder": [entry.as_dict() for entry in truth.ladder],
            "caveats": truth.caveats,
        },
        mode,
    )
