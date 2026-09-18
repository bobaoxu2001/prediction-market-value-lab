"""Exhaustive baskets: the tiling proof, both directions, and what must be refused.

The premise doing all the work is "exactly one rung settles YES". It is not implied by
sharing an event ticker, and the ways it fails are concrete: two rungs that overlap can
both win, and a gap between rungs can let every rung lose. Both break a different floor,
so both are tested separately.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmvl_markets.arbitrage.basket import (
    Bucket,
    interval_for,
    prove_partition,
    scan_basket,
)
from pmvl_shared.enums import Category, DataProvenance, MarketStatus, Platform, Side
from pmvl_shared.schemas import BookLevel, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import utcnow

D = Decimal
CUTOFF = utcnow() + timedelta(hours=6)
EVENT = "KXHIGHCHI-26SEP17"

#: The real Chicago temperature ladder: two tails and four closed buckets.
CHICAGO = [
    ("T74", "less", None, D("74")),
    ("B74", "between", D("74"), D("75")),
    ("B76", "between", D("76"), D("77")),
    ("B78", "between", D("78"), D("79")),
    ("B80", "between", D("80"), D("81")),
    ("T81", "greater", D("81"), None),
]


def _market(mid, comparator, floor, cap, *, event=EVENT, source="National Weather Service",
            cutoff=None):
    return NormalizedMarket(
        platform=Platform.KALSHI, platform_market_id=mid, platform_event_id=event,
        series_ticker="KXHIGHCHI", title=f"max temp {mid}", category=Category.WEATHER,
        status=MarketStatus.OPEN, accepting_orders=True,
        strike_type=comparator, floor_strike=floor, cap_strike=cap,
        settlement_source=source, close_time=cutoff or CUTOFF,
        expected_resolution_time=(cutoff or CUTOFF) + timedelta(minutes=5),
        tick_size=D("0.01"), min_order_size=D("1"),
        fee_rate=D("0.07"), fee_type="quadratic",
        settlement_rules_raw="Resolves to the NWS daily maximum temperature.",
        resolution_hash=f"h-{mid}",
    )


def _book(mid, yes_ask, no_ask, *, size=D("500"), age=0):
    return OrderBook(
        platform=Platform.KALSHI, platform_market_id=mid,
        observed_at=utcnow() - timedelta(seconds=age),
        yes_asks=[BookLevel(price=D(yes_ask), size=size)],
        no_asks=[BookLevel(price=D(no_ask), size=size)],
        yes_bids=[], no_bids=[], provenance=DataProvenance.FIXTURE,
    )


def _basket(spec=None, *, yes_ask="0.15", no_ask="0.86", **mkw):
    spec = spec or CHICAGO
    out = []
    for mid, comparator, floor, cap in spec:
        m = _market(mid, comparator, floor, cap, **mkw)
        out.append(Bucket(m, _book(mid, yes_ask, no_ask), interval_for(m)))
    return out


# ------------------------------------------------------------------- the proof
def test_the_real_chicago_ladder_tiles_the_integer_grid():
    proof = prove_partition(_basket())
    assert proof.proven is True
    assert proof.grid_step == D("1")
    assert any("grid of 1" in a for a in proof.assumptions)


def test_the_grid_assumption_is_stated_rather_than_buried():
    """Over the reals these rungs leave gaps; the claim only holds on a grid."""
    proof = prove_partition(_basket())
    assert proof.assumptions
    assert "settle every rung NO" in proof.assumptions[0]


def test_overlapping_rungs_are_refused_because_two_can_win():
    overlapping = [
        ("T74", "less", None, D("74")),
        ("B74", "between", D("74"), D("76")),
        ("B76", "between", D("75"), D("77")),   # shares 75-76 with the previous rung
        ("T77", "greater", D("77"), None),
    ]
    proof = prove_partition(_basket(overlapping))
    assert proof.proven is False
    assert "overlap" in proof.reason


def test_a_gap_wider_than_the_grid_is_refused_because_all_rungs_can_lose():
    holed = [
        ("T74", "less", None, D("74")),
        ("B74", "between", D("74"), D("75")),
        ("B80", "between", D("80"), D("81")),   # 76..79 is covered by nothing
        ("T81", "greater", D("81"), None),
    ]
    proof = prove_partition(_basket(holed))
    assert proof.proven is False
    assert "do not cover" in proof.reason


def test_a_basket_missing_a_tail_is_refused():
    no_tail = [s for s in CHICAGO if s[0] != "T81"]
    proof = prove_partition(_basket(no_tail))
    assert proof.proven is False
    assert "tail" in proof.reason


def test_rungs_from_different_events_are_refused():
    mixed = _basket()
    stray = _market("OTHER", "greater", D("81"), None, event="KXHIGHLAX-26SEP17")
    mixed[-1] = Bucket(stray, _book("OTHER", "0.15", "0.86"), interval_for(stray))
    proof = prove_partition(mixed)
    assert proof.proven is False
    assert "event" in proof.reason


def test_rungs_with_different_settlement_sources_are_refused():
    mixed = _basket()
    stray = _market("T81", "greater", D("81"), None, source="Weather Underground")
    mixed[-1] = Bucket(stray, _book("T81", "0.15", "0.86"), interval_for(stray))
    proof = prove_partition(mixed)
    assert proof.proven is False
    assert "settlement_source" in proof.reason


# ------------------------------------------------------------- both directions
def test_the_all_yes_basket_floors_at_one_dollar():
    """Six rungs at 15c is 90c for a dollar that one of them must pay."""
    certs = {c.direction: c for c in scan_basket(_basket(yes_ask="0.15", no_ask="0.90"))}
    cert = certs["all_yes"]
    assert cert.payoff_per_set == D("1")
    assert cert.structural_status == "guaranteed"
    assert cert.net_floor_usd > 0
    assert cert.accepted is True
    assert len(cert.legs) == 6
    assert all(leg["side"] == Side.YES.value for leg in cert.legs)


def test_the_all_no_basket_floors_at_n_minus_one():
    """Every rung but the winner pays, so six rungs floor at $5 a set."""
    certs = {c.direction: c for c in scan_basket(_basket(yes_ask="0.20", no_ask="0.80"))}
    cert = certs["all_no"]
    assert cert.payoff_per_set == D("5")
    assert cert.rung_count == 6
    # 6 x 80c = $4.80 against a $5 floor.
    assert cert.structural_status == "guaranteed"
    assert cert.net_floor_usd > 0
    assert all(leg["side"] == Side.NO.value for leg in cert.legs)


def test_both_directions_are_always_priced():
    certs = scan_basket(_basket())
    assert {c.direction for c in certs} == {"all_yes", "all_no"}


def test_a_basket_that_does_not_clear_is_reported_not_hidden():
    certs = {c.direction: c for c in scan_basket(_basket(yes_ask="0.18", no_ask="0.99"))}
    no_side = certs["all_no"]
    assert no_side.accepted is False
    assert no_side.rejection_reason


def test_an_unproven_partition_blocks_both_directions():
    holed = [
        ("T74", "less", None, D("74")),
        ("B74", "between", D("74"), D("75")),
        ("B80", "between", D("80"), D("81")),
        ("T81", "greater", D("81"), None),
    ]
    for cert in scan_basket(_basket(holed, yes_ask="0.01", no_ask="0.01")):
        assert cert.accepted is False
        assert "partition not proven" in cert.rejection_reason


# ------------------------------------------------------------------- execution
def test_marginal_sizing_stops_where_the_next_set_stops_paying():
    from pmvl_markets.arbitrage.basket import _accretive_basket_size

    buckets = []
    for mid, comparator, floor, cap in CHICAGO:
        m = _market(mid, comparator, floor, cap)
        book = OrderBook(
            platform=Platform.KALSHI, platform_market_id=mid, observed_at=utcnow(),
            yes_asks=[BookLevel(price=D("0.14"), size=D("200")),
                      BookLevel(price=D("0.20"), size=D("800"))],
            no_asks=[BookLevel(price=D("0.86"), size=D("1000"))],
            yes_bids=[], no_bids=[], provenance=DataProvenance.FIXTURE,
        )
        buckets.append(Bucket(m, book, interval_for(m)))

    # 6 x 0.14 = 0.84 a set clears the $1 floor; 6 x 0.20 = 1.20 does not.
    size = _accretive_basket_size(buckets, Side.YES, D("1"), D("10000"))
    assert size == D("200")


def test_a_stale_book_blocks_acceptance():
    """Prices chosen so both directions clear economically; only age should stop them."""
    buckets = [
        Bucket(_market(m, c, f, p), _book(m, "0.15", "0.80", age=9000), interval_for(_market(m, c, f, p)))
        for m, c, f, p in CHICAGO
    ]
    certs = scan_basket(buckets, max_quote_age_seconds=300)
    assert len(certs) == 2
    for cert in certs:
        assert cert.structural_status == "guaranteed"   # the structure is fine
        assert cert.accepted is False                   # the quote is not
        assert "old" in cert.rejection_reason


def test_the_basket_scanner_reaches_no_venue():
    import inspect

    from pmvl_markets.arbitrage import basket

    source = inspect.getsource(basket)
    for forbidden in ("requests", "httpx", "urlopen", "submit", "place_order"):
        assert forbidden not in source
