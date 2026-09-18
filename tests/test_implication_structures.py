"""Same-venue implication structures: the floor, the proof, and the refusals.

The positives here are the least interesting part. What this route has to get right is
refusing: a pair whose strikes are ordered but whose settlement source differs, a pair
whose comparator makes the truth sets overlap without containment, a boundary where
``>=`` and ``>`` at the same strike do *not* imply, and a structure that is genuinely
riskless but does not clear its fees.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmvl_markets.arbitrage.implication import (
    FieldVerdict,
    ProofStatus,
    StrikeContract,
    implication_holds,
    prove_implication,
    scan_implication_family,
)
from pmvl_shared.enums import Category, DataProvenance, MarketStatus, Platform, Side
from pmvl_shared.schemas import BookLevel, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import utcnow

D = Decimal
CUTOFF = utcnow() + timedelta(hours=6)


def _market(mid, strike, *, comparator="greater", source="CF Benchmarks",
            event="KXBTCD-26SEP18", series="KXBTCD", cutoff=None, platform=Platform.KALSHI):
    return NormalizedMarket(
        platform=platform,
        platform_market_id=mid,
        platform_event_id=event,
        series_ticker=series,
        title=f"Bitcoin above {strike}?",
        category=Category.CRYPTO,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        strike_type=comparator,
        floor_strike=D(str(strike)),
        settlement_source=source,
        settlement_rules_raw=f"If BRTI is above {strike} at 7 PM EDT, resolves Yes.",
        resolution_hash=f"hash-{mid}",
        close_time=cutoff or CUTOFF,
        expected_resolution_time=(cutoff or CUTOFF) + timedelta(minutes=5),
        tick_size=D("0.01"),
        min_order_size=D("1"),
        fee_rate=D("0.07"),
        fee_type="quadratic",
    )


def _book(mid, *, yes_ask, no_ask, size=D("500"), age_seconds=0, platform=Platform.KALSHI):
    observed = utcnow() - timedelta(seconds=age_seconds)
    return OrderBook(
        platform=platform,
        platform_market_id=mid,
        observed_at=observed,
        yes_asks=[BookLevel(price=D(str(yes_ask)), size=size)],
        no_asks=[BookLevel(price=D(str(no_ask)), size=size)],
        yes_bids=[BookLevel(price=D(str(yes_ask)) - D("0.01"), size=size)],
        no_bids=[BookLevel(price=D(str(no_ask)) - D("0.01"), size=size)],
        provenance=DataProvenance.FIXTURE,
    )


def _rung(mid, strike, *, yes_ask, no_ask, **kw):
    mkw = {k: v for k, v in kw.items() if k in
           {"comparator", "source", "event", "series", "cutoff", "platform"}}
    bkw = {k: v for k, v in kw.items() if k in {"size", "age_seconds", "platform"}}
    m = _market(mid, strike, **mkw)
    return StrikeContract(
        market=m,
        book=_book(mid, yes_ask=yes_ask, no_ask=no_ask, **bkw),
        comparator=mkw.get("comparator", "greater"),
        threshold=D(str(strike)),
    )


# --------------------------------------------------------------- the arithmetic
def test_the_floor_is_one_dollar_per_set_and_the_pair_clears_it():
    """YES(weaker) + NO(stronger) for 90c pays at least $1 in every reachable state."""
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")

    certs = [c for c in scan_implication_family([weak, strong]) if c.accepted]
    assert len(certs) == 1
    cert = certs[0]

    assert cert.proof.status is ProofStatus.PROVEN
    assert cert.structural_status == "guaranteed"
    # 0.40 on the weaker YES + 0.48 on the stronger NO = 0.88 a set.
    assert cert.payoff_floor_usd == cert.quantity
    assert cert.net_floor_usd > 0
    roles = {leg["role"].split()[0]: leg for leg in cert.legs}
    assert roles["consequent"]["contract_key"] == "LOW"
    assert roles["consequent"]["side"] == Side.YES.value
    assert roles["antecedent"]["contract_key"] == "HIGH"
    assert roles["antecedent"]["side"] == Side.NO.value


def test_the_state_that_would_break_the_floor_is_pruned_and_the_profitable_one_is_not():
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    cert = [c for c in scan_implication_family([weak, strong]) if c.accepted][0]

    normal = cert.payoff_report["normal_settlement"]
    # Four joint states minus the impossible (HIGH yes, LOW no).
    assert normal["scenarios_considered"] == 3
    assert normal["scenarios_pruned"] == 1


def test_a_structure_that_cannot_beat_its_fees_is_reported_not_hidden():
    """Riskless and unprofitable is a real answer, and it must be visible."""
    weak = _rung("LOW", 70000, yes_ask="0.50", no_ask="0.51")
    strong = _rung("HIGH", 80000, yes_ask="0.50", no_ask="0.4999")

    certs = scan_implication_family([weak, strong])
    assert len(certs) == 1
    cert = certs[0]
    assert cert.proof.status is ProofStatus.PROVEN   # the structure is real
    assert cert.accepted is False                    # the trade is not
    assert "tolerance" in cert.rejection_reason or "floor" in cert.rejection_reason


def test_an_aligned_void_is_priced_and_disclosed_rather_than_ignored():
    """Same event, so a void strikes both legs at once - costing exactly the fees."""
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    cert = [c for c in scan_implication_family([weak, strong]) if c.accepted][0]

    void = cert.payoff_report["including_aligned_void"]
    # The stake comes back and the fee does not, so the void branch loses the fees.
    assert Decimal(void["worst_case_net_usd"]) < 0
    assert Decimal(void["worst_case_net_usd"]) >= -cert.fees_usd


# ------------------------------------------------------------------- boundaries
def test_equal_strikes_imply_only_in_the_direction_that_actually_contains():
    assert implication_holds("greater", D("70"), "greater_or_equal", D("70"))[0] is True
    assert implication_holds("greater_or_equal", D("70"), "greater", D("70"))[0] is False


def test_opposite_comparators_never_imply():
    holds, why = implication_holds("greater", D("70"), "less", D("80"))
    assert holds is False
    assert "opposite directions" in why


def test_a_reversed_strike_order_is_refuted_not_silently_flipped():
    lower = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    higher = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    proof = prove_implication(lower, higher)   # claims weaker implies stronger
    assert proof.status is ProofStatus.REFUTED
    assert any(c.name == "threshold_order" and c.verdict is FieldVerdict.MISMATCH
               for c in proof.checks)


# ------------------------------------------------------ the terms the floor needs
def test_a_different_settlement_source_refutes_the_implication():
    """Same strikes, different oracle: the pruned state becomes reachable again."""
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61", source="CF Benchmarks")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48", source="Coinbase spot")
    proof = prove_implication(strong, weak)
    assert proof.status is ProofStatus.REFUTED
    assert "settlement_source" in proof.reason
    assert scan_implication_family([weak, strong]) == []


def test_a_different_cutoff_refutes_the_implication():
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48",
                   cutoff=CUTOFF + timedelta(hours=3))
    proof = prove_implication(strong, weak)
    assert proof.status is ProofStatus.REFUTED
    assert "cutoff" in proof.reason


def test_a_missing_cutoff_is_insufficient_evidence_not_a_mismatch():
    """Absent is not contradictory, and the two need different work."""
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    stripped = strong.market.model_copy(update={"close_time": None})
    strong = StrikeContract(stripped, strong.book, strong.comparator, strong.threshold)

    proof = prove_implication(strong, weak)
    assert proof.status is ProofStatus.INSUFFICIENT_EVIDENCE
    assert any(c.name == "cutoff" and c.verdict is FieldVerdict.INSUFFICIENT
               for c in proof.checks)


def test_a_bucket_market_is_not_applicable_rather_than_failed():
    """A `between` rung is one cell of an exhaustive basket, not a half-line.

    Reporting it as a failure would put a well-formed market permanently in the
    reject pile for a route it was never a candidate for.
    """
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61", comparator="between")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    proof = prove_implication(strong, weak)
    assert proof.status is ProofStatus.NOT_APPLICABLE
    assert any(c.verdict is FieldVerdict.NOT_APPLICABLE for c in proof.checks)


def test_a_cross_venue_pair_is_not_applicable_to_this_route():
    """Cross-venue has its own, stricter gate; this route must not become a bypass."""
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    poly = _rung("POLY", 80000, yes_ask="0.42", no_ask="0.48", platform=Platform.POLYMARKET)
    proof = prove_implication(poly, weak)
    assert proof.status is ProofStatus.NOT_APPLICABLE
    assert "same-venue only" in proof.reason


# ------------------------------------------------------------------- execution
def test_a_stale_book_is_rejected_with_the_age_named():
    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61", age_seconds=9000)
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    cert = scan_implication_family([weak, strong], max_quote_age_seconds=300)[0]
    assert cert.accepted is False
    assert "old" in cert.rejection_reason


def test_size_is_capped_by_the_depth_that_still_clears_the_floor():
    """Available depth is not capacity: the size that is profitable is smaller.

    The weak book offers 100 at 0.40 and then 900 at 0.70. Against a 0.48 NO leg the
    blended cost crosses $1 at about 167 sets, so 1,000 is available and roughly 166
    is affordable. Sizing off availability is how a modelled edge becomes a real loss
    on the back half of the fill.
    """
    from pmvl_markets.arbitrage.implication import _accretive_size

    weak = StrikeContract(
        _market("LOW", 70000),
        OrderBook(
            platform=Platform.KALSHI, platform_market_id="LOW", observed_at=utcnow(),
            yes_asks=[BookLevel(price=D("0.40"), size=D("100")),
                      BookLevel(price=D("0.70"), size=D("900"))],
            no_asks=[BookLevel(price=D("0.61"), size=D("1000"))],
            yes_bids=[], no_bids=[], provenance=DataProvenance.FIXTURE,
        ),
        "greater", D("70000"),
    )
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48", size=D("1000"))

    affordable = _accretive_size(weak, strong, ceiling=D("10000"))
    # Only the 100 at 0.40 has a marginal pair price under $1 (0.40+0.48); the next
    # level at 0.70 would cost 1.18 a set, so it is refused rather than averaged in.
    assert affordable == D("100")

    cert = scan_implication_family([weak, strong])[0]
    assert cert.quantity == affordable


def test_an_empty_book_yields_no_structure():
    weak = StrikeContract(
        _market("LOW", 70000),
        OrderBook(platform=Platform.KALSHI, platform_market_id="LOW",
                  observed_at=utcnow(), provenance=DataProvenance.FIXTURE),
        "greater", D("70000"),
    )
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    cert = scan_implication_family([weak, strong])[0]
    assert cert.accepted is False
    assert cert.quantity == 0


def test_a_ladder_produces_one_certificate_per_provable_pair():
    rungs = [
        _rung("A", 60000, yes_ask="0.70", no_ask="0.31"),
        _rung("B", 70000, yes_ask="0.50", no_ask="0.51"),
        _rung("C", 80000, yes_ask="0.30", no_ask="0.71"),
    ]
    certs = scan_implication_family(rungs)
    assert len(certs) == 3          # A-B, A-C, B-C
    assert all(c.proof.status is ProofStatus.PROVEN for c in certs)


# ---------------------------------------------------------------------- safety
def test_the_scanner_reaches_no_venue_and_places_no_order():
    """Pure function of markets and books: nothing here can transact."""
    import inspect

    from pmvl_markets.arbitrage import implication

    source = inspect.getsource(implication)
    for forbidden in ("requests", "httpx", "urlopen", "submit", "place_order", "POST"):
        assert forbidden not in source


def test_a_certificate_round_trips_through_json():
    import json

    weak = _rung("LOW", 70000, yes_ask="0.40", no_ask="0.61")
    strong = _rung("HIGH", 80000, yes_ask="0.42", no_ask="0.48")
    cert = [c for c in scan_implication_family([weak, strong]) if c.accepted][0]
    blob = json.dumps(cert.as_dict())
    back = json.loads(blob)
    assert back["accepted"] is True
    assert back["proof"]["status"] == "proven"
    assert Decimal(back["net_floor_usd"]) == cert.net_floor_usd.quantize(Decimal("0.0001"))


def test_marginal_sizing_beats_average_sizing_on_a_thin_top_of_book():
    """The regression that produced a $60 loss on a real Solana ladder.

    Top of book is cheap and thin; the levels behind it are dear. Sizing by "largest
    size whose *average* cost is under $1" keeps buying past the point where the
    marginal set stops paying, diluting a 6c edge to under 1c and letting the fee
    overtake it. Sizing on the marginal set stops at the depth that actually pays.
    """
    weak = StrikeContract(
        _market("LOW", 106),
        OrderBook(
            platform=Platform.KALSHI, platform_market_id="LOW", observed_at=utcnow(),
            yes_asks=[BookLevel(price=D("0.09"), size=D("500")),
                      BookLevel(price=D("0.12"), size=D("9500"))],
            no_asks=[BookLevel(price=D("0.91"), size=D("10000"))],
            yes_bids=[], no_bids=[], provenance=DataProvenance.FIXTURE,
        ),
        "greater", D("106"),
    )
    strong = StrikeContract(
        _market("HIGH", 108),
        OrderBook(
            platform=Platform.KALSHI, platform_market_id="HIGH", observed_at=utcnow(),
            yes_asks=[BookLevel(price=D("0.14"), size=D("10000"))],
            no_asks=[BookLevel(price=D("0.85"), size=D("500")),
                     BookLevel(price=D("0.89"), size=D("9500"))],
            yes_bids=[], no_bids=[], provenance=DataProvenance.FIXTURE,
        ),
        "greater", D("108"),
    )

    cert = scan_implication_family([weak, strong])[0]
    # 0.09 + 0.85 = 0.94 a set on the first 500; the next pair costs 0.12 + 0.89 =
    # 1.01 and is refused. Averaging would have taken all 10,000.
    assert cert.quantity == D("500")
    assert cert.net_floor_usd > 0
    assert cert.structural_status == "guaranteed"
    assert cert.accepted is True
