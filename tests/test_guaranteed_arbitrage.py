"""The conditions under which a Kalshi/Polymarket pair may be called risk-free.

The scanner has always refused to label a cross-venue pair ``EXECUTABLE`` unless the
two venues were confirmed to settle identically *including* how each handles
cancellation. What it could not do was ever confirm that: the cancellation component
returned ``UNKNOWN`` unconditionally, so the strict verdict - and with it the only
label that claims a guaranteed payout - was unreachable by construction.

These tests pin both halves of the fix: that the component now reads each venue's own
wording and can return a real answer, and that every way of *failing* that check
still blocks the guarantee.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import (
    ArbitrageLabel,
    Category,
    MarketStatus,
    Platform,
    RuleCompatibility,
    Side,
)
from pmvl_shared.money import D
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from pmvl_markets.arbitrage.complete_set import scan_complete_set
from pmvl_markets.arbitrage.cross_platform import scan_cross_platform
from pmvl_markets.arbitrage.logical import ThresholdMarket, scan_monotonicity
from pmvl_markets.arbitrage.multi_outcome import OutcomeLeg, scan_multi_outcome
from pmvl_markets.matching.candidates import generate_candidates
from pmvl_markets.matching.equivalence import (
    ComponentResult,
    EquivalenceVerdict,
    postponement_is_material,
    score_equivalence,
)
from pmvl_markets.normalize.rules import (
    CANCELLATION_TRIGGERS,
    POSTPONEMENT_TRIGGERS,
    VoidPolicy,
    classify_void_language,
    normalize_rules,
)

from .conftest import make_book

# A threshold market on an index: nothing about it can be postponed, and both venues
# state the same void consequence.
BTC_RULES = (
    "This market will resolve to Yes if the CF Benchmarks BRTI closing price of "
    "Bitcoin on 2026-12-31 at 16:00 UTC is above 100000 USD. If the CF Benchmarks "
    "index is cancelled or unavailable, this market will be voided and all stakes "
    "refunded."
)
BTC_TITLE = "Will Bitcoin be above $100,000 on December 31?"


def btc_market(platform: Platform, market_id: str, *, rules: str = BTC_RULES, **kw):  # noqa: ANN001, ANN201
    defaults = dict(
        platform=platform,
        platform_market_id=market_id,
        title=BTC_TITLE,
        description=rules,
        settlement_rules_raw=rules,
        settlement_source="CF Benchmarks BRTI",
        category=Category.CRYPTO,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        floor_strike=Decimal("100000"),
        strike_type="greater",
        expected_resolution_time=utcnow() + timedelta(hours=6),
        close_time=utcnow() + timedelta(hours=6),
        volume_24h=Decimal("500000"),
        fee_rate=Decimal("0.07"),
        fee_type="quadratic" if platform is Platform.KALSHI else "general_fees",
        tick_size=Decimal("0.01") if platform is Platform.KALSHI else Decimal("0.001"),
    )
    defaults.update(kw)
    return NormalizedMarket(**defaults)


def scored(market_a: NormalizedMarket, market_b: NormalizedMarket):  # noqa: ANN201
    candidates = generate_candidates([market_a], [market_b])
    assert candidates, "the pair must be proposed before it can be scored"
    return score_equivalence(candidates[0])


def cheap_pair_books(market_a: NormalizedMarket, market_b: NormalizedMarket):  # noqa: ANN201
    """Books where A-YES at $0.40 and B-NO at $0.40 buy a $1 payout for $0.80."""
    book_a = make_book(
        platform=market_a.platform, market_id=market_a.platform_market_id,
        yes_asks=[("0.40", "5000")], no_asks=[("0.62", "5000")],
    )
    book_b = make_book(
        platform=market_b.platform, market_id=market_b.platform_market_id,
        yes_asks=[("0.62", "5000")], no_asks=[("0.40", "5000")],
    )
    return book_a, book_b


class TestVoidLanguageClassification:
    def test_reads_a_void_commitment(self) -> None:
        policy = classify_void_language(
            "If the game is cancelled, the contract will be voided and stakes refunded.",
            CANCELLATION_TRIGGERS,
        )
        assert policy is VoidPolicy.VOID_REFUND

    def test_reads_a_settle_anyway_commitment(self) -> None:
        policy = classify_void_language(
            "If the launch does not occur by the deadline, this market resolves to No.",
            CANCELLATION_TRIGGERS,
        )
        assert policy is VoidPolicy.RESOLVE_OUTRIGHT

    def test_reads_a_deadline_extension(self) -> None:
        policy = classify_void_language(
            "If the match is postponed, the market will remain open until the match "
            "is played.",
            POSTPONEMENT_TRIGGERS,
        )
        assert policy is VoidPolicy.EXTENDED

    def test_consequence_must_be_in_the_trigger_sentence(self) -> None:
        """A resolution rule elsewhere in the text is not a cancellation policy.

        Reading one as a commitment is how two venues that never addressed the
        question end up agreeing about it.
        """
        text = (
            "This market resolves to No if the price is at or below the strike. "
            "The exchange may cancel the series at its discretion."
        )
        assert classify_void_language(text, CANCELLATION_TRIGGERS) is VoidPolicy.UNSTATED

    def test_two_different_consequences_are_not_a_commitment(self) -> None:
        text = (
            "If the event is cancelled, the market will be voided. "
            "If the event does not occur by the deadline, the market resolves to No."
        )
        assert classify_void_language(text, CANCELLATION_TRIGGERS) is VoidPolicy.UNSTATED

    def test_silence_is_unstated(self) -> None:
        assert classify_void_language("", CANCELLATION_TRIGGERS) is VoidPolicy.UNSTATED

    def test_policies_stay_out_of_the_resolution_digest(self) -> None:
        """The digest is about who gets paid *normally*; void handling is its own gate.

        Folding it in would silently change every stored hash and make matched pairs
        read as newly-divergent.
        """
        with_clause = normalize_rules(title=BTC_TITLE, description=BTC_RULES)
        without = normalize_rules(
            title=BTC_TITLE,
            description=(
                "This market will resolve to Yes if the CF Benchmarks BRTI closing "
                "price of Bitcoin on 2026-12-31 at 16:00 UTC is above 100000 USD."
            ),
        )
        assert with_clause.cancellation_policy == VoidPolicy.VOID_REFUND.value
        assert without.cancellation_policy == VoidPolicy.UNSTATED.value
        assert with_clause.resolution_hash == without.resolution_hash


class TestCancellationComponent:
    def test_agreeing_venues_confirm_the_term(self) -> None:
        score = scored(
            btc_market(Platform.KALSHI, "KXBTC-26DEC31-T100000"),
            btc_market(Platform.POLYMARKET, "998877"),
        )
        component = score.component("cancellation")
        assert component is not None
        assert component.result is ComponentResult.MATCH
        assert score.verdict is EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT
        assert score.verdict.allows_guaranteed_arbitrage

    def test_one_silent_venue_leaves_the_term_unconfirmed(self) -> None:
        quiet = BTC_RULES.split(" If the CF Benchmarks")[0]
        score = scored(
            btc_market(Platform.KALSHI, "KXBTC-26DEC31-T100000"),
            btc_market(Platform.POLYMARKET, "998877", rules=quiet),
        )
        assert score.component("cancellation").result is ComponentResult.UNKNOWN
        assert score.verdict is EquivalenceVerdict.VERIFIED_EQUIVALENT_STANDARD
        assert not score.verdict.allows_guaranteed_arbitrage
        assert score.verdict.allows_hedged_position

    def test_contradicting_venues_are_a_mismatch(self) -> None:
        """One venue refunds, the other settles. Both legs can lose."""
        settles = BTC_RULES.replace(
            "this market will be voided and all stakes refunded",
            "this market resolves to No",
        )
        score = scored(
            btc_market(Platform.KALSHI, "KXBTC-26DEC31-T100000"),
            btc_market(Platform.POLYMARKET, "998877", rules=settles),
        )
        component = score.component("cancellation")
        assert component.result is ComponentResult.MISMATCH
        assert "void_refund" in component.detail and "resolve_outright" in component.detail
        assert score.verdict is EquivalenceVerdict.RELATED_NOT_EQUIVALENT
        assert not score.verdict.allows_hedged_position


class TestPostponementMateriality:
    def test_a_scheduled_occurrence_is_material(self) -> None:
        market = btc_market(
            Platform.KALSHI, "K1",
            title="Will the Yankees win the game on July 4?",
            category=Category.SPORTS,
        )
        assert postponement_is_material(market)

    def test_an_index_threshold_is_not(self) -> None:
        assert not postponement_is_material(btc_market(Platform.KALSHI, "K1"))

    def test_material_markets_need_postponement_confirmed(self) -> None:
        """Agreeing on cancellation is not enough when the event can be rescheduled."""
        rules = (
            "This market resolves Yes if the Yankees win the game on 2026-07-04. "
            "If the game is cancelled, the market will be voided and stakes refunded."
        )
        a = btc_market(
            Platform.KALSHI, "KXGAME-1", rules=rules,
            title="Will the Yankees win the game on July 4?",
            category=Category.SPORTS, floor_strike=None, strike_type=None,
            settlement_source="official league",
        )
        b = btc_market(
            Platform.POLYMARKET, "555", rules=rules,
            title="Will the Yankees win the game on July 4?",
            category=Category.SPORTS, floor_strike=None, strike_type=None,
            settlement_source="official league",
        )
        component = scored(a, b).component("cancellation")
        assert component.result is ComponentResult.UNKNOWN
        assert "scheduled occurrence" in component.detail


class TestCrossVenueLabel:
    def test_a_strict_pair_is_executable(self) -> None:
        """The end-to-end property: a genuine risk-free pair is named as one."""
        a = btc_market(Platform.KALSHI, "KXBTC-26DEC31-T100000")
        b = btc_market(Platform.POLYMARKET, "998877", yes_token_id="1", no_token_id="2")
        book_a, book_b = cheap_pair_books(a, b)

        result = scan_cross_platform(a, book_a, b, book_b, scored(a, b))

        assert result is not None
        assert result.label is ArbitrageLabel.EXECUTABLE
        assert result.net_profit_per_set > 0
        assert result.cost_breakdown["cancellation_status"] == "match"
        assert result.cost_breakdown["equivalence_verdict"] == "VERIFIED_EQUIVALENT_STRICT"
        assert not any("NOT a guaranteed profit" in flag for flag in result.risk_flags)

    def test_an_unconfirmed_void_term_is_not_executable(self) -> None:
        quiet = BTC_RULES.split(" If the CF Benchmarks")[0]
        a = btc_market(Platform.KALSHI, "KXBTC-26DEC31-T100000")
        b = btc_market(
            Platform.POLYMARKET, "998877", rules=quiet,
            yes_token_id="1", no_token_id="2",
        )
        book_a, book_b = cheap_pair_books(a, b)

        result = scan_cross_platform(a, book_a, b, book_b, scored(a, b))

        assert result is not None
        assert result.label is ArbitrageLabel.RULE_MISMATCH_RISK
        assert any("NOT a guaranteed profit" in flag for flag in result.risk_flags)

    def test_a_bare_match_verdict_can_never_buy_a_guarantee(self) -> None:
        """The older, less informative type carries no cancellation evidence."""
        from pmvl_markets.matching.verify import MatchVerdict

        a = btc_market(Platform.KALSHI, "KXBTC-26DEC31-T100000")
        b = btc_market(Platform.POLYMARKET, "998877", yes_token_id="1", no_token_id="2")
        book_a, book_b = cheap_pair_books(a, b)

        result = scan_cross_platform(
            a, book_a, b, book_b,
            MatchVerdict(
                rule_compatibility=RuleCompatibility.IDENTICAL,
                match_confidence=D("0.99"), time_compatibility=D("1"),
                source_compatibility=D("1"), polarity_inverted=False, outcome_mapping={},
            ),
        )
        assert result is not None
        assert result.label is not ArbitrageLabel.EXECUTABLE


class TestNetEconomicsSizing:
    """Gross-first sizing hides real arbitrage behind expensive marginal depth."""

    LADDER = [("0.30", "100"), ("0.49", "9900")]

    def test_complete_set_finds_the_profitable_prefix(self, kalshi_market) -> None:  # noqa: ANN001
        book = make_book(yes_asks=self.LADDER, no_asks=self.LADDER)
        result = scan_complete_set(kalshi_market, book)
        assert result is not None
        assert result.label is ArbitrageLabel.EXECUTABLE
        assert result.max_executable_sets == Decimal("100")
        assert result.max_net_profit > 0

    def test_cross_platform_finds_the_profitable_prefix(self) -> None:
        a = btc_market(Platform.KALSHI, "KXBTC-26DEC31-T100000")
        b = btc_market(Platform.POLYMARKET, "998877", yes_token_id="1", no_token_id="2")
        book_a = make_book(
            platform=a.platform, market_id=a.platform_market_id,
            yes_asks=self.LADDER, no_asks=[("0.62", "10000")],
        )
        book_b = make_book(
            platform=b.platform, market_id=b.platform_market_id,
            yes_asks=[("0.62", "10000")], no_asks=self.LADDER,
        )
        result = scan_cross_platform(a, book_a, b, book_b, scored(a, b))
        assert result is not None
        assert result.max_executable_sets == Decimal("100")
        assert result.max_net_profit > 0

    def test_a_uniformly_unprofitable_book_still_returns_nothing(self, kalshi_market) -> None:  # noqa: ANN001
        book = make_book(yes_asks=[("0.4975", "1000")], no_asks=[("0.4975", "1000")])
        assert scan_complete_set(kalshi_market, book) is None


class TestPerLegAvailability:
    """A basket is only complete if every leg can actually be bought."""

    def test_multi_outcome_blocks_a_leg_that_rejects_orders(
        self, kalshi_market, book_factory  # noqa: ANN001
    ) -> None:
        legs = []
        for index, accepting in enumerate((True, False)):
            market = kalshi_market.model_copy(
                update={
                    "platform_market_id": f"KXLEG-{index}",
                    "accepting_orders": accepting,
                }
            )
            legs.append(
                OutcomeLeg(
                    market=market,
                    book=book_factory(
                        market_id=f"KXLEG-{index}", yes_asks=[("0.30", "1000")]
                    ),
                    market_id=index + 1,
                )
            )
        result = scan_multi_outcome(
            legs, event_title="event", mutually_exclusive=True, exhaustive=True,
            negative_risk=True, expected_outcome_count=2,
        )
        assert result is not None
        assert result.label is ArbitrageLabel.EXECUTION_RISK
        assert any("not accepting orders" in flag for flag in result.risk_flags)

    def test_monotonicity_hedge_blocks_a_leg_that_rejects_orders(
        self, kalshi_market, book_factory  # noqa: ANN001
    ) -> None:
        weaker = ThresholdMarket(
            market=kalshi_market.model_copy(
                update={"platform_market_id": "KXLOW", "accepting_orders": False}
            ),
            book=book_factory(
                market_id="KXLOW", yes_asks=[("0.30", "1000")], no_asks=[("0.70", "1000")]
            ),
            threshold=Decimal("100"),
            market_id=1,
        )
        stronger = ThresholdMarket(
            market=kalshi_market.model_copy(update={"platform_market_id": "KXHIGH"}),
            book=book_factory(
                market_id="KXHIGH", yes_asks=[("0.60", "1000")], no_asks=[("0.40", "1000")]
            ),
            threshold=Decimal("200"),
            market_id=2,
        )
        results = scan_monotonicity([weaker, stronger], family_label="KXTEST")
        assert results
        blocked = [r for r in results if r.label is ArbitrageLabel.EXECUTION_RISK]
        assert blocked, [r.label.value for r in results]
        assert any("not accepting orders" in f for f in blocked[0].risk_flags)


class TestMatchedLegsGetBookCoverage:
    """A verified pair that never gets two books can never be scanned.

    The scoring and coverage reserves rank each venue independently by liquidity, so
    both halves of a pair winning their own venue's draw is a coincidence. Without a
    reserve the cross-venue scanner is starved by the fetch plan rather than by the
    market - and an arbitrage nobody priced looks exactly like an arbitrage that was
    not there.
    """

    def build(self, count: int, *, prefix: str, volume: str) -> list[NormalizedMarket]:
        return [
            btc_market(
                Platform.KALSHI, f"{prefix}-{i}",
                volume_24h=Decimal(volume),
                series_ticker=f"{prefix}-{i}",
            )
            for i in range(count)
        ]

    def test_a_matched_leg_outranks_a_more_liquid_unmatched_market(self) -> None:
        from pmvl_markets.ingest.runner import select_for_orderbooks

        liquid = self.build(20, prefix="LIQUID", volume="9000000")
        matched = self.build(1, prefix="MATCHED", volume="1000")

        chosen = select_for_orderbooks(
            liquid + matched, 10, matched_ids={"MATCHED-0"}
        )
        ids = {m.platform_market_id for m in chosen}
        assert "MATCHED-0" in ids, "the thin matched leg must survive the budget"
        assert len(chosen) <= 10

    def test_without_the_match_set_the_thin_leg_is_squeezed_out(self) -> None:
        """The behaviour being corrected, pinned so the reserve is not silently lost."""
        from pmvl_markets.ingest.runner import select_for_orderbooks

        liquid = self.build(20, prefix="LIQUID", volume="9000000")
        matched = self.build(1, prefix="MATCHED", volume="1000")

        chosen = select_for_orderbooks(liquid + matched, 10)
        assert "MATCHED-0" not in {m.platform_market_id for m in chosen}

    def test_the_reserve_cannot_consume_the_whole_budget(self) -> None:
        """A reserve is a floor, not a takeover: the other two passes keep their share."""
        from pmvl_markets.ingest.runner import select_for_orderbooks

        matched = self.build(40, prefix="MATCHED", volume="5000")
        liquid = self.build(20, prefix="LIQUID", volume="9000000")

        chosen = select_for_orderbooks(
            matched + liquid, 10,
            matched_ids={m.platform_market_id for m in matched},
        )
        assert len(chosen) == 10
        # The default 0.2 share caps the reserve at 2 of a 10-book budget. The
        # remaining 8 are still allocated by liquidity, which the deep unmatched
        # markets win.
        from_reserve = [m for m in chosen if m.platform_market_id.startswith("MATCHED")]
        assert len(from_reserve) == 2

    def test_withdrawn_matches_stop_reserving_budget(self, clean_db) -> None:  # noqa: ANN001
        """A pair that stopped matching must release its slot on the next cycle."""
        from sqlalchemy import select as sa_select

        from pmvl_markets.db_models import MarketMatch
        from pmvl_markets.ingest.runner import matched_market_ids
        from pmvl_markets.ingest.store import upsert_markets

        a = btc_market(Platform.KALSHI, "KXBTC-T100000")
        b = btc_market(Platform.POLYMARKET, "998877")
        upsert_markets(clean_db, [a, b])
        clean_db.flush()

        from pmvl_markets.arbitrage.pipeline import load_matchable_markets, refresh_matches

        refresh_matches(clean_db, load_matchable_markets(clean_db))
        clean_db.flush()
        assert matched_market_ids(clean_db) == {"KXBTC-T100000", "998877"}

        row = clean_db.scalar(sa_select(MarketMatch))
        row.rule_compatibility = RuleCompatibility.INCOMPATIBLE.value
        clean_db.flush()
        assert matched_market_ids(clean_db) == set()


class TestTickerAndNameAreTheSameSubject:
    """The venues do not share a naming convention, and recall depends on knowing it.

    Kalshi lists the ticker, Polymarket spells the name. A raw substring comparison
    reads them as two different subjects and rejects the pair on an entity mismatch
    that is not real - discarding genuine pairs without ever making a false one look
    good, which is how it survived a suite built around not over-claiming.
    """

    @pytest.mark.parametrize(
        ("kalshi_title", "poly_title"),
        [
            ("SOL price on Sep 12?", "Will the price of Solana be above $110?"),
            ("BTC price today?", "Will Bitcoin close above $100,000?"),
            ("ETH price today?", "Will Ethereum be above $4,000?"),
        ],
    )
    def test_ticker_and_name_do_not_read_as_an_entity_mismatch(
        self, kalshi_title: str, poly_title: str
    ) -> None:
        from pmvl_markets.matching.verify import unmatched_proper_nouns

        a = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="K", title=kalshi_title
        )
        b = NormalizedMarket(
            platform=Platform.POLYMARKET, platform_market_id="P", title=poly_title
        )
        assert unmatched_proper_nouns(a, b) == set()

    def test_genuinely_different_subjects_are_still_rejected(self) -> None:
        """The fix must not make every pair look related."""
        from pmvl_markets.matching.verify import unmatched_proper_nouns

        a = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="K",
            title="Will the Yankees win the World Series?",
        )
        b = NormalizedMarket(
            platform=Platform.POLYMARKET, platform_market_id="P",
            title="Will the Dodgers win the World Series?",
        )
        assert unmatched_proper_nouns(a, b) == {"yankees", "dodgers"}
