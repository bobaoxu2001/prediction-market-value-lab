"""Negative controls: break one condition at a time and require a refusal.

A pipeline that only ever says yes to good inputs has not been tested. These construct
inputs that are wrong in exactly one way and assert the refusal, because a scanner that
still profits from a deliberately broken input has a bug that no amount of passing
happy-path tests would reveal.

The simultaneity control is here because it caught a live one: every positive-net-floor
structure in the stored-book scan was built from legs quoted 454 to 10,762 seconds
apart, and the largest of them reported "+$33.48 guaranteed" from a Solana quote three
hours older than its partner.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmvl_markets.arbitrage.implication import (
    ProofStatus,
    StrikeContract,
    prove_implication,
    scan_implication_family,
)
from pmvl_markets.arbitrage.profit_proof import (
    Gate,
    GateResult,
    ProofLevel,
    classify,
)
from pmvl_markets.arbitrage.profit_proof import net_floor_gate, simultaneity_gate
from pmvl_shared.enums import Category, DataProvenance, MarketStatus, Platform
from pmvl_shared.schemas import BookLevel, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import utcnow

D = Decimal
CUTOFF = utcnow() + timedelta(hours=6)


def _market(mid, strike, comparator="greater", source="CF Benchmarks", event="EVT"):
    return NormalizedMarket(
        platform=Platform.KALSHI, platform_market_id=mid, platform_event_id=event,
        series_ticker="KXT", title=f"above {strike}", category=Category.CRYPTO,
        status=MarketStatus.OPEN, accepting_orders=True,
        strike_type=comparator, floor_strike=D(str(strike)),
        settlement_source=source, close_time=CUTOFF,
        expected_resolution_time=CUTOFF, tick_size=D("0.01"), min_order_size=D("1"),
        fee_rate=D("0.07"), fee_type="quadratic",
    )


def _rung(mid, strike, yes_ask, no_ask, *, age=0, size=D("5000"), **mk):
    m = _market(mid, strike, **mk)
    book = OrderBook(
        platform=Platform.KALSHI, platform_market_id=mid,
        observed_at=utcnow() - timedelta(seconds=age),
        yes_asks=[BookLevel(price=D(yes_ask), size=size)],
        no_asks=[BookLevel(price=D(no_ask), size=size)],
        yes_bids=[], no_bids=[], provenance=DataProvenance.FIXTURE,
    )
    return StrikeContract(m, book, mk.get("comparator", "greater"), D(str(strike)))


def _profitable_pair(**kw):
    """A structure that genuinely clears, so a control can break exactly one thing."""
    return [_rung("LOW", 70000, "0.30", "0.71", **kw),
            _rung("HIGH", 80000, "0.32", "0.60", **kw)]


def test_the_control_pair_really_does_clear():
    """Guard the guards: if this stops passing, every control below is vacuous."""
    certs = [c for c in scan_implication_family(_profitable_pair()) if c.accepted]
    assert len(certs) == 1
    assert certs[0].net_floor_usd > 0
    assert certs[0].structural_status == "guaranteed"


# ------------------------------------------------------------ one broken thing
def test_non_simultaneous_legs_are_refused():
    """The control that caught the real bug: legs from different market states."""
    legs = [
        _rung("LOW", 70000, "0.30", "0.71", age=0),
        _rung("HIGH", 80000, "0.32", "0.60", age=10_762),   # the real KXSOLD skew
    ]
    certs = scan_implication_family(legs, max_quote_age_seconds=86_400)
    assert len(certs) == 1
    cert = certs[0]
    assert cert.net_floor_usd > 0            # the arithmetic still "works"
    assert cert.snapshot_skew_seconds >= 10_000
    assert cert.accepted is False            # and it is still refused
    assert "never coexisted" in cert.rejection_reason


def test_a_reversed_implication_is_refused():
    lower, higher = _profitable_pair()
    assert prove_implication(lower, higher).status is ProofStatus.REFUTED


def test_a_different_settlement_source_is_refused():
    legs = [_rung("LOW", 70000, "0.30", "0.71"),
            _rung("HIGH", 80000, "0.32", "0.60", source="Coinbase spot")]
    assert scan_implication_family(legs) == []


def test_extra_fees_flip_a_winner_to_a_loser():
    """Profitability must be sensitive to the fee, or the fee is not being charged."""
    base = [c for c in scan_implication_family(_profitable_pair()) if c.accepted][0]
    dear = _profitable_pair()
    dear = [
        StrikeContract(
            c.market.model_copy(update={"fee_rate": D("0.70")}),  # 10x
            c.book, c.comparator, c.threshold,
        )
        for c in dear
    ]
    stressed = scan_implication_family(dear)[0]
    assert base.net_floor_usd > 0
    assert stressed.net_floor_usd < base.net_floor_usd
    assert stressed.accepted is False


def test_insufficient_depth_shrinks_size_rather_than_inventing_it():
    thin = [_rung("LOW", 70000, "0.30", "0.71", size=D("7")),
            _rung("HIGH", 80000, "0.32", "0.60", size=D("5000"))]
    cert = [c for c in scan_implication_family(thin) if c.accepted][0]
    assert cert.quantity == D("7")          # the thin leg sets the size
    assert cert.payoff_floor_usd == D("7")


def test_an_empty_side_yields_no_structure():
    broken = _profitable_pair()
    empty = OrderBook(
        platform=Platform.KALSHI, platform_market_id="HIGH", observed_at=utcnow(),
        yes_asks=[BookLevel(price=D("0.32"), size=D("100"))], no_asks=[],
        yes_bids=[], no_bids=[], provenance=DataProvenance.FIXTURE,
    )
    broken[1] = StrikeContract(broken[1].market, empty, "greater", D("80000"))
    cert = scan_implication_family(broken)[0]
    assert cert.accepted is False
    assert cert.quantity == 0


def test_a_stale_book_is_refused_even_when_both_legs_are_equally_stale():
    """Equal staleness gives zero skew; age is a separate gate and must still fire."""
    legs = _profitable_pair(age=9000)
    cert = scan_implication_family(legs, max_quote_age_seconds=300)[0]
    assert cert.snapshot_skew_seconds is not None
    assert cert.snapshot_skew_seconds < 5     # simultaneous...
    assert cert.accepted is False             # ...but both are old
    assert "old" in cert.rejection_reason


# ------------------------------------------------------------- the verdict ladder
def test_a_failed_simultaneity_gate_caps_the_verdict_below_executable():
    gates = [
        Gate("structure_proven", GateResult.PASS, "", ProofLevel.STRUCTURAL_PROOF_ONLY),
        net_floor_gate(D("4700"), D("4633.04"), D("33.48")),
        simultaneity_gate(10_762.0, 1.0),
    ]
    bundle = classify(gates)
    assert bundle.verdict is ProofLevel.NET_ARBITRAGE_OBSERVED
    assert "10762" in bundle.reason or "10762.0" in bundle.reason


def test_a_negative_net_floor_caps_below_net_arbitrage():
    gates = [
        Gate("structure_proven", GateResult.PASS, "", ProofLevel.STRUCTURAL_PROOF_ONLY),
        net_floor_gate(D("15.00"), D("14.96"), D("0.18")),
        simultaneity_gate(0.0, 1.0),
    ]
    bundle = classify(gates)
    assert bundle.verdict is ProofLevel.GROSS_ARBITRAGE_OBSERVED


def test_an_unmeasured_gate_is_not_established_rather_than_passed():
    """"We did not check" must never read the same as "we checked and it was fine"."""
    gates = [
        Gate("structure_proven", GateResult.PASS, "", ProofLevel.STRUCTURAL_PROOF_ONLY),
        net_floor_gate(D("100"), D("90"), D("1")),
        simultaneity_gate(None, 1.0),
    ]
    bundle = classify(gates)
    assert bundle.verdict is ProofLevel.NET_ARBITRAGE_OBSERVED
    assert bundle.unestablished
    assert bundle.failed == []


def test_every_gate_passing_is_the_only_route_to_stable_profit_evidence():
    gates = [
        Gate("a", GateResult.PASS, "", ProofLevel.STRUCTURAL_PROOF_ONLY),
        Gate("b", GateResult.PASS, "", ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED),
        Gate("c", GateResult.PASS, "", ProofLevel.STABLE_PROFIT_EVIDENCE),
    ]
    assert classify(gates).verdict is ProofLevel.STABLE_PROFIT_EVIDENCE
    # And a single NOT_ESTABLISHED anywhere is enough to stop it.
    gates[1] = Gate("b", GateResult.NOT_ESTABLISHED, "no forward data",
                    ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED)
    assert classify(gates).verdict is not ProofLevel.STABLE_PROFIT_EVIDENCE


def test_no_gates_is_insufficient_evidence_not_a_pass():
    assert classify([]).verdict is ProofLevel.INSUFFICIENT_EVIDENCE
