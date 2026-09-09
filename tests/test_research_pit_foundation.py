"""Attacking the Model-vs-Market foundation for leakage.

Every test here is an attempt to get future information into an observation. A
test that merely confirms the happy path would not have caught any of the bugs
this repository has actually had.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_markets.research.contract import (
    BucketKind,
    ContractParseError,
    ContractSpec,
    ladder_is_coherent,
    parse_contract,
)
from pmvl_markets.research.exclusions import DEFECT_CODES, Exclusion
from pmvl_markets.research.pit import (
    MAX_FORECAST_AGE,
    MAX_ORDERBOOK_AGE,
    select_forecast_as_of,
    select_orderbook_as_of,
)
from pmvl_markets.research.prices import MarketState, compute_edge
from pmvl_markets.research.probability import contract_probability

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


@dataclass
class Snap:
    available_at: datetime
    tag: str = ""


# ---------------------------------------------------------------------------
# Leakage attacks
# ---------------------------------------------------------------------------


class TestForecastCannotComeFromTheFuture:
    def test_a_forecast_issued_after_t_is_rejected(self) -> None:
        got = select_forecast_as_of([Snap(T0 + timedelta(seconds=1), "future")], cutoff=T0)
        assert not got.ok
        assert got.reason is Exclusion.NO_FORECAST_AVAILABLE

    def test_moving_t_earlier_excludes_newly_future_forecasts(self) -> None:
        """The core invariant, stated as a monotonicity property."""
        snaps = [Snap(T0 - timedelta(hours=h), f"h{h}") for h in (1, 3, 5)]
        late = select_forecast_as_of(snaps, cutoff=T0)
        early = select_forecast_as_of(snaps, cutoff=T0 - timedelta(hours=2))
        assert late.record.tag == "h1"
        assert early.record.tag == "h3"

    def test_the_latest_legal_forecast_wins_not_the_closest(self) -> None:
        snaps = [Snap(T0 - timedelta(hours=6)), Snap(T0 - timedelta(minutes=5))]
        got = select_forecast_as_of(snaps, cutoff=T0)
        assert got.age_seconds == pytest.approx(300, abs=1)

    def test_a_forecast_exactly_at_t_is_admissible(self) -> None:
        """Available AT the cutoff was available."""
        assert select_forecast_as_of([Snap(T0)], cutoff=T0).ok

    def test_a_forecast_without_a_timestamp_is_never_assumed_available(self) -> None:
        got = select_forecast_as_of([Snap(None)], cutoff=T0)  # type: ignore[arg-type]
        assert not got.ok

    def test_a_forecast_older_than_the_bound_is_refused_not_stretched(self) -> None:
        got = select_forecast_as_of(
            [Snap(T0 - MAX_FORECAST_AGE - timedelta(minutes=1))], cutoff=T0
        )
        assert got.reason is Exclusion.FORECAST_TOO_STALE

    def test_a_revision_does_not_rewrite_an_earlier_observation(self) -> None:
        """A later revision must not become the answer for an earlier cutoff."""
        original = Snap(T0 - timedelta(hours=2), "original")
        revision = Snap(T0 + timedelta(hours=1), "revision")
        got = select_forecast_as_of([original, revision], cutoff=T0)
        assert got.record.tag == "original"


class TestMarketCannotComeFromTheFuture:
    def test_a_book_captured_after_t_is_rejected(self) -> None:
        got = select_orderbook_as_of([Snap(T0 + timedelta(seconds=1))], cutoff=T0)
        assert not got.ok

    def test_a_missing_book_is_never_filled_from_later(self) -> None:
        """The gap must stay a gap."""
        got = select_orderbook_as_of(
            [Snap(T0 + timedelta(seconds=30)), Snap(T0 + timedelta(seconds=60))],
            cutoff=T0,
        )
        assert got.reason is Exclusion.NO_ORDERBOOK
        assert got.record is None

    def test_a_book_beyond_the_staleness_bound_is_refused(self) -> None:
        got = select_orderbook_as_of(
            [Snap(T0 - MAX_ORDERBOOK_AGE - timedelta(seconds=1))], cutoff=T0
        )
        assert got.reason is Exclusion.ORDERBOOK_TOO_STALE

    def test_selection_is_deterministic_across_repeated_calls(self) -> None:
        snaps = [Snap(T0 - timedelta(seconds=s), f"s{s}") for s in (10, 20, 30)]
        first = select_orderbook_as_of(snaps, cutoff=T0)
        second = select_orderbook_as_of(list(snaps), cutoff=T0)
        assert first.record.tag == second.record.tag


class TestSettlementIsOnlyALabel:
    def test_the_probability_layer_cannot_see_an_outcome(self) -> None:
        """Signature-level proof: there is nowhere to pass one."""
        import inspect

        params = set(inspect.signature(contract_probability).parameters)
        for banned in ("settlement", "outcome", "result", "realized", "settled_at"):
            assert not any(banned in p for p in params), f"{banned} reachable"

    def test_the_pit_module_never_reads_settlement_tables(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/pit.py"
        ).read_text()
        for banned in ("settlements", "settled_at", "result"):
            assert banned not in source

    def test_probability_is_unchanged_by_any_outcome(self) -> None:
        spec = ContractSpec(BucketKind.BETWEEN, Decimal("87"), Decimal("88"))
        kwargs = dict(
            forecast_high_f=Decimal("87.5"),
            forecast_available_at=T0 - timedelta(hours=1),
            information_cutoff=T0,
            lead_days=1,
        )
        assert contract_probability(spec, **kwargs).probability == (
            contract_probability(spec, **kwargs).probability
        )

    def test_a_forecast_after_the_cutoff_raises_rather_than_silently_using_it(
        self,
    ) -> None:
        spec = ContractSpec(BucketKind.BETWEEN, Decimal("87"), Decimal("88"))
        with pytest.raises(ValueError, match="not available"):
            contract_probability(
                spec,
                forecast_high_f=Decimal("87.5"),
                forecast_available_at=T0 + timedelta(seconds=1),
                information_cutoff=T0,
                lead_days=1,
            )


# ---------------------------------------------------------------------------
# Contract semantics
# ---------------------------------------------------------------------------


class TestContractBoundaries:
    def test_between_is_inclusive_at_both_ends(self) -> None:
        """"87 to 88" means 87 or 88. A half-open reading shifts every bucket."""
        spec = parse_contract(strike_type="between", floor_strike=87, cap_strike=88)
        assert spec.contains(87) and spec.contains(88)
        assert not spec.contains(86) and not spec.contains(89)

    def test_greater_is_STRICT_not_inclusive(self) -> None:
        """The rule reads "is greater than 90", so 90 does not pay.

        Treating floor_strike as inclusive is a one-degree error on every
        threshold contract, always overstating YES. It is invisible in the
        ticker (T90) and in the structured field (floor_strike=90), and shows up
        only in the rule text.
        """
        spec = parse_contract(strike_type="greater", floor_strike=90, cap_strike=None)
        assert not spec.contains(90), "90 must NOT pay a 'greater than 90' contract"
        assert spec.contains(91) and spec.contains(200)

    def test_less_is_STRICT_not_inclusive(self) -> None:
        """Symmetrically: "is less than 79" means 78 or below."""
        spec = parse_contract(strike_type="less", floor_strike=None, cap_strike=79)
        assert not spec.contains(79), "79 must NOT pay a 'less than 79' contract"
        assert spec.contains(78)

    def test_t90_and_b89_5_are_not_the_same_contract(self) -> None:
        """The distinction the whole parser exists to preserve."""
        above = parse_contract(strike_type="greater", floor_strike=90, cap_strike=None)
        bucket = parse_contract(strike_type="between", floor_strike=89, cap_strike=90)
        assert above.contains(95) and not bucket.contains(95)
        # 90 pays the bucket and not the threshold - the exact degree where the
        # two contracts disagree, and where an inclusive reading would merge
        # them.
        assert bucket.contains(90) and not above.contains(90)

    def test_the_parser_agrees_with_the_venue_rule_text(self) -> None:
        """Conformance against real settlement wording, not our own restatement.

        Reads the operational database when present. This is what caught the
        strict/inclusive bug: the structured fields and the ticker both look
        inclusive, and only the prose says otherwise.
        """
        import sqlite3

        db = Path("data/pmvl.db")
        if not db.exists():
            pytest.skip("operational database not present")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT strike_type, floor_strike, cap_strike, settlement_rules_raw "
            "FROM markets WHERE series_ticker LIKE 'KXHIGH%' "
            "AND settlement_rules_raw IS NOT NULL LIMIT 400"
        ).fetchall()
        con.close()
        checked = 0
        for kind, floor, cap, rule in rows:
            text = (rule or "").lower()
            try:
                spec = parse_contract(
                    strike_type=kind, floor_strike=floor, cap_strike=cap
                )
            except ContractParseError:
                continue
            if kind == "greater" and "greater than" in text:
                assert not spec.contains(int(float(floor))), (
                    f"rule says 'greater than {floor}' but {floor} pays"
                )
                checked += 1
            elif kind == "less" and "less than" in text:
                assert not spec.contains(int(float(cap))), (
                    f"rule says 'less than {cap}' but {cap} pays"
                )
                checked += 1
        assert checked > 0, "no threshold contracts available to conform against"

    @pytest.mark.parametrize(
        "kind,floor,cap",
        [("between", 87, None), ("between", None, 88), ("greater", None, None),
         ("less", None, None), ("mystery", 1, 2)],
    )
    def test_an_unresolvable_contract_is_refused_not_guessed(
        self, kind, floor, cap  # noqa: ANN001
    ) -> None:
        with pytest.raises(ContractParseError):
            parse_contract(strike_type=kind, floor_strike=floor, cap_strike=cap)

    def test_inverted_bounds_are_refused(self) -> None:
        with pytest.raises(ContractParseError):
            parse_contract(strike_type="between", floor_strike=90, cap_strike=87)


class TestLadderCoherence:
    def test_a_contiguous_ladder_is_coherent(self) -> None:
        specs = [
            ContractSpec(BucketKind.BETWEEN, Decimal(a), Decimal(b))
            for a, b in ((85, 86), (87, 88), (89, 90))
        ]
        ok, why = ladder_is_coherent(specs)
        assert ok, why

    def test_an_overlap_is_caught(self) -> None:
        specs = [
            ContractSpec(BucketKind.BETWEEN, Decimal(85), Decimal(88)),
            ContractSpec(BucketKind.BETWEEN, Decimal(87), Decimal(90)),
        ]
        ok, why = ladder_is_coherent(specs)
        assert not ok and "overlap" in why

    def test_a_gap_is_caught(self) -> None:
        specs = [
            ContractSpec(BucketKind.BETWEEN, Decimal(85), Decimal(86)),
            ContractSpec(BucketKind.BETWEEN, Decimal(90), Decimal(91)),
        ]
        ok, why = ladder_is_coherent(specs)
        assert not ok and "gap" in why


# ---------------------------------------------------------------------------
# Price semantics
# ---------------------------------------------------------------------------


class TestMidpointIsNotExecutable:
    def _state(self) -> MarketState:
        return MarketState(
            yes_bid=Decimal("0.40"), yes_ask=Decimal("0.46"),
            no_bid=Decimal("0.54"), no_ask=Decimal("0.60"),
        )

    def test_mid_and_executable_are_different_numbers(self) -> None:
        s = self._state()
        assert s.mid == Decimal("0.43")
        assert s.executable_yes_buy == Decimal("0.46")
        assert s.mid != s.executable_yes_buy

    def test_the_edge_object_keeps_them_apart(self) -> None:
        """Collapsing these is how a diagnostic becomes a profit claim."""
        edge = compute_edge(Decimal("0.50"), self._state())
        assert edge.raw_mid == Decimal("0.07")
        assert edge.executable_yes == Decimal("0.04")
        assert edge.raw_mid > edge.executable_yes

    def test_the_venue_fee_is_charged_against_the_executable_legs(self) -> None:
        """The frozen protocol declares this; the code did not implement it.

        protocol.py sets fee_treatment to
        "kalshi_taker_fee_applied_to_executable_only", and that string is inside
        the protocol hash. compute_edge nevertheless returned model - ask with
        no fee term, so every executable edge was overstated by the whole fee.
        """
        edge = compute_edge(Decimal("0.50"), self._state())
        # 0.07 * 1 * 0.46 * 0.54 = 0.017388, ceiled to the whole cent = 0.02.
        assert edge.fee_per_contract_yes == Decimal("0.02")
        assert edge.executable_yes == Decimal("0.04")
        assert edge.executable_yes_net == Decimal("0.02")
        assert edge.executable_yes_net == edge.executable_yes - edge.fee_per_contract_yes

    def test_the_fee_is_not_charged_against_the_midpoint(self) -> None:
        """raw_mid is a diagnostic against a price nobody offers.

        Charging a fee there would imply the midpoint was executable, which is
        the confusion this module exists to prevent. "executable_only" in the
        protocol string is load-bearing.
        """
        state = self._state()
        cheap = compute_edge(Decimal("0.50"), state, fee_rate=Decimal("0.001"))
        dear = compute_edge(Decimal("0.50"), state, fee_rate=Decimal("0.20"))
        assert cheap.raw_mid == dear.raw_mid
        assert cheap.executable_yes == dear.executable_yes
        assert cheap.executable_yes_net > dear.executable_yes_net

    def test_an_edge_smaller_than_the_fee_is_a_loss_not_a_signal(self) -> None:
        """The reason the missing fee term mattered.

        At the money Kalshi's taker fee is 1.75 probability points against a
        median quoted spread of 1.00, so a gross edge can be positive while the
        trade loses money. Gross-only reporting turns that loss into a signal.
        """
        thin = MarketState(
            yes_bid=Decimal("0.44"), yes_ask=Decimal("0.46"),
            no_bid=Decimal("0.54"), no_ask=Decimal("0.60"),
        )
        edge = compute_edge(Decimal("0.475"), thin)
        assert edge.executable_yes > 0        # looks tradeable
        assert edge.executable_yes_net < 0    # is not

    def test_an_unpriced_leg_has_no_net_edge_rather_than_a_free_one(self) -> None:
        state = MarketState(
            yes_bid=None, yes_ask=Decimal("0.46"), no_bid=None, no_ask=None,
        )
        edge = compute_edge(Decimal("0.50"), state)
        assert edge.executable_no is None
        assert edge.executable_no_net is None
        assert edge.fee_per_contract_no is None

    def test_the_per_contract_fee_falls_with_order_size(self) -> None:
        """Kalshi ceils to the whole cent on the whole order.

        Size 1 therefore pays the most per contract, which is why it is the
        default: it cannot understate the fee, and median depth at the touch on
        these series is a few dollars anyway.
        """
        state = self._state()
        one = compute_edge(Decimal("0.50"), state, contracts=Decimal(1))
        many = compute_edge(Decimal("0.50"), state, contracts=Decimal(100))
        assert one.fee_per_contract_yes > many.fee_per_contract_yes
        assert one.executable_yes_net < many.executable_yes_net

    def test_the_no_leg_is_fee_charged_at_the_no_offer(self) -> None:
        """The NO side is a separate trade at a separate price.

        Its fee must be computed at the NO offer, not at the YES offer: the
        quadratic fee peaks at 50c, so charging the wrong leg's price gets the
        magnitude wrong in whichever direction the book is skewed.
        """
        edge = compute_edge(Decimal("0.30"), self._state())
        # no_ask is 0.60, so buying NO costs 0.60 and pays 1 if it settles NO.
        assert edge.executable_no == Decimal("0.10")
        # 0.07 * 1 * 0.60 * 0.40 = 0.0168, ceiled to the whole cent = 0.02.
        assert edge.fee_per_contract_no == Decimal("0.02")
        assert edge.executable_no_net == Decimal("0.08")

    def test_a_zero_gross_edge_is_negative_once_the_fee_is_charged(self) -> None:
        """Fair value at the offer is not a free trade."""
        edge = compute_edge(Decimal("0.46"), self._state())
        assert edge.executable_yes == Decimal("0.00")
        assert edge.executable_yes_net < 0

    def test_a_zero_fee_rate_leaves_the_edge_untouched(self) -> None:
        """A fee-free series must not be charged a fee by default."""
        edge = compute_edge(Decimal("0.50"), self._state(), fee_rate=Decimal("0"))
        assert edge.fee_per_contract_yes == Decimal("0")
        assert edge.executable_yes_net == edge.executable_yes
        assert edge.executable_no_net == edge.executable_no

    def test_the_serialised_edge_carries_gross_and_net_separately(self) -> None:
        """A consumer must never have to guess which one it was handed."""
        payload = compute_edge(Decimal("0.50"), self._state()).as_dict()
        assert payload["executable_yes_edge"] == "0.04"
        assert payload["executable_yes_edge_net"] == "0.02"
        assert payload["fee_per_contract_yes"] == "0.02"
        assert payload["raw_mid_edge"] == "0.07"
        assert all(v is None or isinstance(v, str) for v in payload.values())

    def test_an_unpriced_leg_serialises_as_null_not_zero(self) -> None:
        payload = compute_edge(
            Decimal("0.50"),
            MarketState(yes_bid=None, yes_ask=Decimal("0.46"), no_bid=None, no_ask=None),
        ).as_dict()
        assert payload["executable_no_edge"] is None
        assert payload["executable_no_edge_net"] is None
        assert payload["fee_per_contract_no"] is None

    def test_no_side_uses_the_quoted_no_ask_not_one_minus_yes_bid(self) -> None:
        """1 - yes_bid = 0.60 coincides here; the code must not rely on that."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/prices.py"
        ).read_text()
        tree = ast.parse(source)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "compute_edge"
        )
        body = ast.unparse(fn)
        assert "no_ask" in body
        assert "1 - state.yes_bid" not in body.replace(" ", "")

    def test_a_crossed_book_is_rejected(self) -> None:
        bad = MarketState(Decimal("0.60"), Decimal("0.40"), None, None)
        assert bad.validate() is Exclusion.BOOK_INVALID

    def test_a_book_with_nothing_buyable_has_no_executable_price(self) -> None:
        s = MarketState(Decimal("0.40"), None, Decimal("0.54"), None)
        assert s.validate() is Exclusion.NO_EXECUTABLE_PRICE

    def test_out_of_range_prices_are_rejected(self) -> None:
        assert MarketState(None, Decimal("1.4"), None, None).validate() is (
            Exclusion.BOOK_INVALID
        )

    def test_missing_quotes_yield_none_not_zero(self) -> None:
        s = MarketState(None, Decimal("0.46"), None, None)
        assert s.mid is None and s.spread is None
        assert compute_edge(Decimal("0.5"), s).raw_mid is None


class TestProbabilityUnits:
    def test_everything_is_a_probability_not_cents(self) -> None:
        spec = ContractSpec(BucketKind.BETWEEN, Decimal("87"), Decimal("88"))
        p = contract_probability(
            spec,
            forecast_high_f=Decimal("87.5"),
            forecast_available_at=T0 - timedelta(hours=1),
            information_cutoff=T0,
            lead_days=1,
        ).probability
        assert Decimal(0) <= p <= Decimal(1)

    def test_the_integer_continuity_correction_is_applied(self) -> None:
        """A one-degree bucket centred on the forecast must not be near-zero."""
        spec = ContractSpec(BucketKind.BETWEEN, Decimal("88"), Decimal("88"))
        p = contract_probability(
            spec,
            forecast_high_f=Decimal("88"),
            forecast_available_at=T0 - timedelta(hours=1),
            information_cutoff=T0,
            lead_days=0,
        ).probability
        assert p > Decimal("0.15"), "continuity correction missing; bucket collapsed"

    def test_wider_lead_time_widens_the_distribution(self) -> None:
        spec = ContractSpec(BucketKind.BETWEEN, Decimal("88"), Decimal("88"))
        near, far = (
            contract_probability(
                spec, forecast_high_f=Decimal("88"),
                forecast_available_at=T0 - timedelta(hours=1),
                information_cutoff=T0, lead_days=lead,
            ).probability
            for lead in (0, 5)
        )
        assert near > far

    def test_the_provisional_sigma_is_labelled_everywhere(self) -> None:
        """Nobody may mistake a documented prior for a fitted estimate."""
        spec = ContractSpec(BucketKind.BETWEEN, Decimal("87"), Decimal("88"))
        est = contract_probability(
            spec, forecast_high_f=Decimal("87.5"),
            forecast_available_at=T0 - timedelta(hours=1),
            information_cutoff=T0, lead_days=1,
        )
        assert est.provisional is True
        assert "provisional" in est.model_version


class TestExclusionDiscipline:
    def test_defect_and_shortfall_classes_are_disjoint_and_meaningful(self) -> None:
        assert DEFECT_CODES
        assert Exclusion.NO_SETTLEMENT not in DEFECT_CODES
        assert Exclusion.BOOK_AFTER_SETTLEMENT not in DEFECT_CODES
        assert Exclusion.CONTRACT_PARSE_FAILED in DEFECT_CODES

    def test_every_code_is_unique(self) -> None:
        values = [e.value for e in Exclusion]
        assert len(values) == len(set(values))


class TestSettlementParity:
    """A model can be well calibrated against the wrong number."""

    def _classify(self, **kw):  # noqa: ANN003, ANN201
        from pmvl_markets.research.parity import classify

        base = dict(
            settlement_vendor="national_weather_service",
            settlement_product_code="CLIPHL",
            archived_observation_product="observations",
            has_daily_summary=True,
        )
        base.update(kw)
        return classify(**base)

    def test_missing_daily_summary_is_a_mismatch_not_unknown(self) -> None:
        """The settled quantity has never been observed. That is not ambiguity."""
        from pmvl_markets.research.parity import ADMISSIBLE, Parity

        parity, why = self._classify(has_daily_summary=False)
        assert parity is Parity.SETTLEMENT_MISMATCH
        assert parity not in ADMISSIBLE
        assert "daily summary" in why

    def test_a_vendor_relay_over_an_unknown_product_is_a_mismatch(self) -> None:
        """With no archived product to compare, a relay is not admissible.

        Refined in 4C0.5: once the SAME product is archived, the relay becomes a
        MODELLING_APPROXIMATION - the value should be identical, but relay
        timing and revision handling stay unmeasured. Without the archived
        product there is nothing to make even that claim against.
        """
        from pmvl_markets.research.parity import ADMISSIBLE, Parity

        parity, _ = self._classify(settlement_vendor="the_weather_company")
        assert parity is Parity.SETTLEMENT_MISMATCH
        assert parity not in ADMISSIBLE

    def test_a_relay_of_the_archived_product_is_an_approximation(self) -> None:
        from pmvl_markets.research.parity import Parity

        parity, why = self._classify(
            settlement_vendor="the_weather_company", archived_product_code="CLIPHL"
        )
        assert parity is Parity.MODELLING_APPROXIMATION
        assert "UNMEASURED" in why

    def test_matching_source_and_product_is_exact(self) -> None:
        from pmvl_markets.research.parity import ADMISSIBLE, Parity

        parity, _ = self._classify()
        assert parity is Parity.EXACT_MATCH and parity in ADMISSIBLE

    def test_absent_provenance_is_unknown(self) -> None:
        from pmvl_markets.research.parity import ADMISSIBLE, Parity

        parity, _ = self._classify(
            settlement_vendor=None, settlement_product_code=None
        )
        assert parity is Parity.UNKNOWN and parity not in ADMISSIBLE

    def test_the_live_universe_is_currently_inadmissible(self) -> None:
        """Records the state this phase found, so a regression is visible.

        Every open contract settles via The Weather Company on a CLI daily
        maximum, and the archive holds hourly NWS observations only.
        """
        from pmvl_markets.research.parity import ADMISSIBLE

        parity, _ = self._classify(
            settlement_vendor="the_weather_company", has_daily_summary=False
        )
        assert parity not in ADMISSIBLE
