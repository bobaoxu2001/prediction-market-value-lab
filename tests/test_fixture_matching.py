"""Matching a game the two venues title incompatibly.

Kalshi names the outcome and hides the matchup in its rules text; Polymarket names
the matchup and hides the outcome in an array. Their normalized titles overlap at
0.286, so no threshold tuning proposes a real pair - the subject has to be identified
structurally instead.

The danger in doing that is obvious and is what most of this file tests: a structural
matcher that is too loose pairs two *different* contests and presents two independent
bets as a hedge. Baseball teams play three- and four-game series, so "same two teams"
is not "same game".

Rule text below is taken from the wording the venues actually publish.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, MarketStatus, Platform, RuleCompatibility
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from pmvl_markets.matching.candidates import generate_candidates
from pmvl_markets.matching.equivalence import (
    ComponentResult,
    EquivalenceVerdict,
    score_equivalence,
)
from pmvl_markets.matching.fixtures import (
    competitors_match,
    extract_fixture,
    fixtures_agree,
)
from pmvl_markets.matching.verify import verify_match

KALSHI_RULES = (
    "If {side} wins the {a} vs {b} professional baseball game originally scheduled "
    "for {when}, then the market resolves to Yes.\n\n"
    "The following market refers to the {a} vs {b} professional baseball game "
    "originally scheduled for {when}. If this game is postponed or delayed, the "
    "market will remain open and close after the rescheduled game has finished "
    "(within two days). If the game is cancelled or rescheduled to over two days "
    "away, the market will resolve to a fair price in accordance with the rules."
)

POLY_RULES = (
    "This market refers to the baseball game between {a} and {b}, scheduled for "
    "{when}. If the game is postponed, this market will remain open until the game "
    "has been completed. If the game is canceled entirely, with no make-up game, "
    "this market will resolve 50-50."
)


def kalshi_game(
    side: str = "San Diego",
    a: str = "San Diego",
    b: str = "San Francisco",
    when: str = "Sep 11, 2026 at 10:15 PM EDT",
    market_id: str = "KXMLBGAME-26SEP112215SDSF-SD",
) -> NormalizedMarket:
    rules = KALSHI_RULES.format(side=side, a=a, b=b, when=when)
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id=market_id,
        platform_event_id="KXMLBGAME-26SEP112215SDSF",
        title=f"{side} wins",
        subtitle=side,
        description=rules,
        settlement_rules_raw=rules,
        category=Category.SPORTS,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        strike_type="structured",
        expected_resolution_time=utcnow() + timedelta(hours=6),
        close_time=utcnow() + timedelta(days=4),
        event_occurrence_time=utcnow() + timedelta(hours=5),
        volume_24h=Decimal("900000"),
    )


def poly_game(
    a: str = "San Diego Padres",
    b: str = "San Francisco Giants",
    when: str = "September 11, 2026 at 10:15 PM ET",
    market_id: str = "4246052",
    title: str | None = None,
    outcomes: list[str] | None = None,
) -> NormalizedMarket:
    rules = POLY_RULES.format(a=a, b=b, when=when)
    return NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_market_id=market_id,
        title=title if title is not None else f"{a} vs. {b}",
        description=rules,
        settlement_rules_raw=rules,
        category=Category.SPORTS,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        outcomes=outcomes if outcomes is not None else [a, b],
        yes_token_id="111",
        no_token_id="222",
        expected_resolution_time=utcnow() + timedelta(hours=6),
        close_time=utcnow() + timedelta(hours=6),
        volume_24h=Decimal("700000"),
    )


class TestCompetitorNames:
    """Kalshi writes the city plus a disambiguating initial; Polymarket the franchise."""

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("san diego", "San Diego Padres", True),
            ("chicago c", "Chicago Cubs", True),
            ("new york y", "New York Yankees", True),
            ("new york m", "New York Mets", True),
            ("los angeles d", "Los Angeles Dodgers", True),
            ("cleveland", "Cleveland Guardians", True),
            # The disambiguator is load-bearing: these share a city and must not pair.
            ("chicago c", "Chicago White Sox", False),
            ("new york y", "New York Mets", False),
            ("los angeles d", "Los Angeles Angels", False),
            ("boston", "Miami Marlins", False),
            # Nothing but an initial could prefix half the league.
            ("c", "Chicago Cubs", False),
        ],
    )
    def test_prefix_token_matching(self, left: str, right: str, expected: bool) -> None:
        assert competitors_match(left, right) is expected


class TestFixtureExtraction:
    def test_kalshi_fixture_comes_from_the_rules_text(self) -> None:
        """The title says only "San Diego wins" - the matchup is in the prose."""
        fixture = extract_fixture(kalshi_game())
        assert fixture is not None
        assert fixture.sport == "baseball"
        assert fixture.competitors == ("san diego", "san francisco")
        assert fixture.scheduled == date(2026, 9, 11)
        assert fixture.side == "san diego"

    def test_polymarket_fixture_comes_from_the_title_and_outcomes(self) -> None:
        fixture = extract_fixture(poly_game())
        assert fixture is not None
        assert fixture.competitors == ("san diego padres", "san francisco giants")
        assert fixture.scheduled == date(2026, 9, 11)
        # The first outcome is the YES token on a Polymarket binary market.
        assert fixture.side == "san diego padres"

    @pytest.mark.parametrize(
        ("title", "outcomes"),
        [
            # A colon suffix marks a different question about the same contest.
            ("San Diego Padres vs. San Francisco Giants: O/U 8.5", ["Over", "Under"]),
            ("Viking FK vs. GNK Dinamo Zagreb: Team to Advance", ["Viking FK", "GNK Dinamo Zagreb"]),
            # Competitor-shaped title but the outcomes ask something else.
            ("San Diego Padres vs. San Francisco Giants", ["Yes", "No"]),
            ("San Diego Padres vs. San Francisco Giants", ["Over", "Under"]),
        ],
    )
    def test_derived_propositions_are_not_moneylines(
        self, title: str, outcomes: list[str]
    ) -> None:
        """Totals and spreads are different questions about the same game."""
        assert extract_fixture(poly_game(title=title, outcomes=outcomes)) is None

    def test_outcomes_must_name_the_competitors_in_the_title(self) -> None:
        """A title describing a context, with outcomes meaning something else."""
        market = poly_game(
            title="US Open: Qinwen Zheng vs Clara Burel",
            outcomes=["Straight sets", "Three sets"],
        )
        assert extract_fixture(market) is None

    def test_a_fixture_without_a_date_is_unusable(self) -> None:
        market = kalshi_game(when="an unspecified evening")
        assert extract_fixture(market) is None


class TestFixtureAgreement:
    def test_the_same_contest_agrees(self) -> None:
        agree, inverted = fixtures_agree(
            extract_fixture(kalshi_game()), extract_fixture(poly_game())
        )
        assert agree
        # Both markets pay out on San Diego, so YES on one does NOT hedge YES on the
        # other - the scanner must buy one YES against the other's NO.
        assert inverted is False

    def test_opposite_sides_invert_polarity(self) -> None:
        """Kalshi long San Francisco against Polymarket long San Diego."""
        agree, inverted = fixtures_agree(
            extract_fixture(
                kalshi_game(side="San Francisco", market_id="KXMLBGAME-26SEP112215SDSF-SF")
            ),
            extract_fixture(poly_game()),
        )
        assert agree
        assert inverted is True

    def test_the_same_teams_on_a_different_day_do_not_agree(self) -> None:
        """The guard that matters most: a series is several games, not one.

        Without the date requirement, Monday's game pairs with Tuesday's and two
        independent bets get presented as a locked box.
        """
        agree, _ = fixtures_agree(
            extract_fixture(kalshi_game(when="Sep 12, 2026 at 10:15 PM EDT")),
            extract_fixture(poly_game(when="September 11, 2026 at 10:15 PM ET")),
        )
        assert agree is False

    def test_the_two_halves_of_a_doubleheader_do_not_agree(self) -> None:
        """Same teams, same date, different game - and the date alone cannot tell.

        A doubleheader is what a postponement creates, which is exactly the situation
        where these two venues' settlement rules diverge most. Cross-pairing its two
        games would present two independent bets as a hedge.
        """
        agree, _ = fixtures_agree(
            extract_fixture(kalshi_game(when="Sep 11, 2026 at 1:10 PM EDT")),
            extract_fixture(poly_game(when="September 11, 2026 at 7:10 PM ET")),
        )
        assert agree is False

    def test_minor_disagreement_about_first_pitch_still_agrees(self) -> None:
        """The venues need not state the same minute for the same game."""
        agree, _ = fixtures_agree(
            extract_fixture(kalshi_game(when="Sep 11, 2026 at 10:15 PM EDT")),
            extract_fixture(poly_game(when="September 11, 2026 at 10:05 PM ET")),
        )
        assert agree is True

    def test_different_competitors_do_not_agree(self) -> None:
        agree, _ = fixtures_agree(
            extract_fixture(kalshi_game(side="Boston", a="Boston", b="Miami")),
            extract_fixture(poly_game()),
        )
        assert agree is False

    def test_a_different_sport_does_not_agree(self) -> None:
        poly = poly_game()
        poly = poly.model_copy(
            update={
                "settlement_rules_raw": poly.settlement_rules_raw.replace(
                    "baseball", "basketball"
                )
            }
        )
        agree, _ = fixtures_agree(extract_fixture(kalshi_game()), extract_fixture(poly))
        assert agree is False


class TestCandidateGeneration:
    def test_a_fixture_pair_is_proposed_that_word_overlap_would_miss(self) -> None:
        candidates = generate_candidates([kalshi_game()], [poly_game()])
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.fixture is not None
        # The pair is proposed despite token similarity below the verifier's floor.
        assert candidate.token_similarity < 0.35

    def test_a_different_day_gets_no_fixture_identity(self) -> None:
        """Word overlap may still propose it; it must not be granted the identity.

        Without the fixture the pair keeps every ordinary gate - and it fails them,
        because the cutoffs are days apart and the franchise nicknames appear on one
        side only. What must never happen is the pair inheriting the relaxations that
        only a confirmed same-contest match earns.
        """
        candidates = generate_candidates(
            [kalshi_game(when="Sep 12, 2026 at 10:15 PM EDT")], [poly_game()]
        )
        assert all(c.fixture is None for c in candidates)
        for candidate in candidates:
            assert verify_match(candidate).rule_compatibility is (
                RuleCompatibility.INCOMPATIBLE
            )

    def test_a_totals_market_gets_no_fixture_identity(self) -> None:
        candidates = generate_candidates(
            [kalshi_game()],
            [poly_game(
                title="San Diego Padres vs. San Francisco Giants: O/U 8.5",
                outcomes=["Over", "Under"],
            )],
        )
        assert all(c.fixture is None for c in candidates)


class TestVerificationOfFixturePairs:
    def candidate(self, **kw):  # noqa: ANN201
        cands = generate_candidates([kalshi_game(**kw)], [poly_game()])
        assert cands, "the pair must be proposed before it can be verified"
        return cands[0]

    def test_inapplicable_terms_no_longer_demote(self) -> None:
        """A moneyline has no strike, comparator or measurement basis.

        Reporting those as unverified is a category error, and it demoted every
        sports pair for failing to state terms that do not exist.
        """
        verdict = verify_match(self.candidate())
        for absent in (
            "threshold_unknown",
            "comparator_unknown",
            "measurement_basis_unknown",
            "entity_mismatch",
            "low_title_similarity",
            "low_entity_overlap",
            "cutoff_differs_days",
        ):
            assert absent not in verdict.demotion_codes, verdict.demotion_codes
        assert verdict.rule_compatibility is not RuleCompatibility.INCOMPATIBLE

    def test_polarity_comes_from_the_fixture_not_a_comparator(self) -> None:
        verdict = verify_match(
            self.candidate(side="San Francisco", market_id="KXMLBGAME-26SEP112215SDSF-SF")
        )
        assert verdict.polarity_inverted is True

    def test_subject_identity_is_structural(self) -> None:
        score = score_equivalence(self.candidate())
        assert score.component("entity").result is ComponentResult.MATCH
        assert score.component("event").result is ComponentResult.MATCH
        assert "same contest" in score.component("entity").detail

    def test_settlement_source_and_cancellation_are_still_gates(self) -> None:
        """The relaxations must not become a back door to a risk-free claim.

        Kalshi bounds postponement to two days and then resolves "to a fair price in
        accordance with the rules"; Polymarket waits indefinitely and voids 50-50.
        That difference is real, and it is what keeps the pair off the executable
        label.
        """
        score = score_equivalence(self.candidate())
        assert score.component("cancellation").result is not ComponentResult.MATCH
        assert not score.verdict.allows_guaranteed_arbitrage
        assert score.verdict is EquivalenceVerdict.PROBABLE_MATCH

    def test_a_contradicting_void_policy_still_rejects(self) -> None:
        """Fixture identity does not excuse a stated contradiction."""
        poly = poly_game()
        poly = poly.model_copy(
            update={
                "settlement_rules_raw": poly.settlement_rules_raw.replace(
                    "this market will resolve 50-50", "this market will resolve to No"
                )
            }
        )
        kalshi = kalshi_game()
        kalshi = kalshi.model_copy(
            update={
                "settlement_rules_raw": kalshi.settlement_rules_raw.replace(
                    "the market will resolve to a fair price in accordance with the rules",
                    "the market will be voided and all stakes refunded",
                )
            }
        )
        cands = generate_candidates([kalshi], [poly])
        assert cands
        score = score_equivalence(cands[0])
        assert score.component("cancellation").result is ComponentResult.MISMATCH
        assert not score.verdict.allows_hedged_position


class TestRelaxationsAreScopedToFixtures:
    """The negative control: none of this may leak to an ordinary pair."""

    def ordinary_pair(self) -> tuple[NormalizedMarket, NormalizedMarket]:
        # Deliberately non-numeric, so the threshold really is unestablished on both
        # sides rather than merely equal.
        rules = "This market resolves Yes if the incumbent is declared the winner."
        common = dict(
            title="Will the incumbent governor be re-elected in November?",
            description=rules,
            settlement_rules_raw=rules,
            category=Category.FINANCE,
            status=MarketStatus.OPEN,
            accepting_orders=True,
            expected_resolution_time=utcnow() + timedelta(hours=6),
            close_time=utcnow() + timedelta(hours=6),
            volume_24h=Decimal("100000"),
        )
        return (
            NormalizedMarket(
                platform=Platform.KALSHI, platform_market_id="KXIDX-T5000", **common
            ),
            NormalizedMarket(
                platform=Platform.POLYMARKET, platform_market_id="9911", **common
            ),
        )

    def test_an_ordinary_pair_still_reports_unknown_terms(self) -> None:
        a, b = self.ordinary_pair()
        cands = generate_candidates([a], [b])
        assert cands
        candidate = cands[0]
        assert candidate.fixture is None

        verdict = verify_match(candidate)
        score = score_equivalence(candidate, verdict)
        # Neither side states a threshold, and that is still missing information
        # rather than an inapplicable term.
        assert score.component("threshold").result is ComponentResult.UNKNOWN
        assert "threshold_unknown" in verdict.demotion_codes
        assert not score.verdict.allows_guaranteed_arbitrage


class TestTheHedgeIsActuallyAHedge:
    """The property the whole change rests on: exactly one leg pays, in every state.

    A wrong polarity is the worst outcome available here. It would report buying the
    same side of the same contest twice, call it a locked box, and lose the entire
    stake whenever the position is wrong. Both orientations are settled by hand
    against both possible winners.
    """

    def books(self, kalshi_id: str, poly_id: str, *, inverted: bool):  # noqa: ANN201
        from pmvl_shared.schemas import BookLevel, OrderBook

        def book(platform, market_id, yes, no):  # noqa: ANN001, ANN202
            return OrderBook(
                platform=platform,
                platform_market_id=market_id,
                observed_at=utcnow(),
                yes_asks=[BookLevel(price=Decimal(yes), size=Decimal("5000"))],
                no_asks=[BookLevel(price=Decimal(no), size=Decimal("5000"))],
                yes_bids=[],
                no_bids=[],
            )

        kalshi_book = book(Platform.KALSHI, kalshi_id, "0.40", "0.62")
        # Which combination is the hedge depends on polarity, so price the side this
        # orientation actually needs.
        poly_book = book(
            Platform.POLYMARKET, poly_id,
            "0.40" if inverted else "0.62",
            "0.62" if inverted else "0.40",
        )
        return kalshi_book, poly_book

    @pytest.mark.parametrize(
        ("kalshi_pays_on", "expect_inverted"),
        [("San Diego", False), ("San Francisco", True)],
    )
    def test_exactly_one_leg_pays_in_every_state(
        self, kalshi_pays_on: str, expect_inverted: bool
    ) -> None:
        from pmvl_shared.enums import Side

        from pmvl_markets.arbitrage.cross_platform import scan_cross_platform

        kalshi = kalshi_game(
            side=kalshi_pays_on,
            market_id=f"KXMLBGAME-26SEP112215SDSF-{kalshi_pays_on[:2].upper()}",
        )
        poly = poly_game()
        candidates = generate_candidates([kalshi], [poly])
        assert candidates
        candidate = candidates[0]
        assert candidate.fixture_polarity_inverted is expect_inverted

        kalshi_book, poly_book = self.books(
            kalshi.platform_market_id, poly.platform_market_id, inverted=expect_inverted
        )
        result = scan_cross_platform(
            kalshi, kalshi_book, poly, poly_book, score_equivalence(candidate),
            market_a_id=1, market_b_id=2,
        )
        assert result is not None, "the mispriced pair must report an opportunity"

        sides = {leg.platform.value: leg.side for leg in result.legs}
        for winner in ("San Diego", "San Francisco"):
            # Kalshi YES pays iff the team its market names wins. Polymarket YES is
            # outcomes[0], which is the San Diego Padres.
            kalshi_yes_pays = kalshi_pays_on == winner
            poly_yes_pays = winner == "San Diego"
            kalshi_pays = (
                kalshi_yes_pays if sides["kalshi"] is Side.YES else not kalshi_yes_pays
            )
            poly_pays = (
                poly_yes_pays if sides["polymarket"] is Side.YES else not poly_yes_pays
            )
            assert int(kalshi_pays) + int(poly_pays) == 1, (
                f"{winner} wins: kalshi_pays={kalshi_pays} poly_pays={poly_pays} "
                f"legs={sides}"
            )


class TestFindingsFromAdversarialReview:
    """Regressions for false matches an adversarial pass produced on real rows.

    Every case here was reproduced against live data before it was fixed. They are
    kept as tests because each one is a way of presenting two independent bets as a
    hedge, which is the worst thing this codebase can do.
    """

    def test_two_leagues_playing_the_same_two_cities_are_not_one_contest(self) -> None:
        """The one that mattered most.

        Kalshi names competitors by city alone, and Polymarket's prose names the
        LEAGUE ("the upcoming WNBA game") rather than the sport - so the old
        sport-agreement check was inert for 80% of real Polymarket fixtures. A Kalshi
        MLB Seattle/Toronto game certified as a Polymarket WNBA Toronto Tempo /
        Seattle Storm game, inverted, tradeable, on real rows whose stated start
        times are already inside the tolerance.
        """
        mlb_rules = (
            "If Seattle wins the Seattle vs Toronto professional baseball game "
            "originally scheduled for Aug 28, 2026 at 7:15 PM EDT, then the market "
            "resolves to Yes."
        )
        kalshi = kalshi_game(side="Seattle", a="Seattle", b="Toronto")
        kalshi = kalshi.model_copy(
            update={
                "platform_market_id": "KXMLBGAME-26AUG281915SEATOR-SEA",
                "platform_event_id": "KXMLBGAME-26AUG281915SEATOR",
                "title": "Seattle wins",
                "subtitle": "Seattle",
                "settlement_rules_raw": mlb_rules,
                "description": mlb_rules,
            }
        )
        wnba_rules = (
            "In the upcoming WNBA game, scheduled for August 28 at 10:00PM ET: "
            "If the Toronto Tempo win, this market resolves to Toronto Tempo."
        )
        poly = poly_game(a="Toronto Tempo", b="Seattle Storm")
        poly = poly.model_copy(
            update={"settlement_rules_raw": wnba_rules, "description": wnba_rules}
        )

        left, right = extract_fixture(kalshi), extract_fixture(poly)
        assert left is not None and right is not None
        assert left.league == "MLB" and right.league == "WNBA"
        agree, _ = fixtures_agree(left, right)
        assert agree is False
        assert generate_candidates([kalshi], [poly]) == [] or all(
            c.fixture is None for c in generate_candidates([kalshi], [poly])
        )

    @pytest.mark.parametrize(
        ("shorter", "longer"),
        [
            ("washington", "washington state"),
            ("kansas", "kansas state"),
            ("oklahoma", "oklahoma state"),
            ("michigan", "central michigan"),
            ("georgia", "georgia southern"),
        ],
    )
    def test_a_qualifier_changes_the_identity_it_does_not_extend_it(
        self, shorter: str, longer: str
    ) -> None:
        """Real collisions existed between the Kalshi NFL and college catalogs."""
        assert competitors_match(shorter, longer) is False

    def test_a_market_publishing_a_strike_is_not_a_moneyline(self) -> None:
        """The venue's structured metadata outranks a regex over its prose.

        Without this the fixture branch printed "not applicable: a moneyline has no
        strike" for a run-line market that carried one, and paired it with a
        full-game moneyline.
        """
        spread = kalshi_game().model_copy(
            update={"floor_strike": Decimal("1.5"), "strike_type": "greater"}
        )
        assert extract_fixture(spread) is None

    def test_a_trailing_qualifier_is_not_skipped_over(self) -> None:
        """"after five complete innings" is a different question about the game."""
        rules = KALSHI_RULES.format(
            side="San Diego", a="San Diego", b="San Francisco",
            when="Sep 11, 2026 at 10:15 PM EDT",
        ).replace(
            "professional baseball game originally scheduled",
            "professional baseball game after five complete innings originally scheduled",
            1,
        )
        market = kalshi_game().model_copy(
            update={"settlement_rules_raw": rules, "description": rules}
        )
        assert extract_fixture(market) is None

    def test_the_two_halves_of_a_doubleheader_are_distinguished_by_name(self) -> None:
        """Kalshi states which game it means; that marker must not be discarded."""
        rules = KALSHI_RULES.format(
            side="Boston", a="Boston", b="New York Y",
            when="Aug 29, 2026 at 1:05 PM EDT",
        ).replace(
            "professional baseball game originally scheduled",
            "professional baseball game 1 of the double header originally scheduled",
            1,
        )
        game_one = kalshi_game().model_copy(
            update={"settlement_rules_raw": rules, "description": rules}
        )
        fixture = extract_fixture(game_one)
        assert fixture is not None
        assert fixture.doubleheader_leg == "1"

        # The counterpart names no half, so which game it means is unknown.
        poly = poly_game(
            a="Boston Red Sox", b="New York Yankees",
            when="August 29, 2026 at 1:05 PM ET",
        )
        agree, _ = fixtures_agree(fixture, extract_fixture(poly))
        assert agree is False

    def test_a_baseball_pair_without_a_stated_first_pitch_is_refused(self) -> None:
        """Only the clock separates the halves of a doubleheader."""
        poly = poly_game(when="September 11, 2026")
        left, right = extract_fixture(kalshi_game()), extract_fixture(poly)
        assert right is not None and right.start_minute is None
        agree, _ = fixtures_agree(left, right)
        assert agree is False

    def test_exactly_at_the_separation_boundary_the_games_are_different(self) -> None:
        agree, _ = fixtures_agree(
            extract_fixture(kalshi_game(when="Sep 11, 2026 at 1:05 PM EDT")),
            extract_fixture(poly_game(when="September 11, 2026 at 4:05 PM ET")),
        )
        assert agree is False

    def test_a_series_market_is_not_a_game(self) -> None:
        poly = poly_game()
        poly = poly.model_copy(
            update={
                "settlement_rules_raw": poly.settlement_rules_raw
                + " This market resolves on the best-of-seven series."
            }
        )
        assert extract_fixture(poly) is None

    def test_a_fixture_pair_can_never_be_called_guaranteed(self) -> None:
        """Capped below the strict verdict by construction, not by coincidence.

        Six of the eight components are decided structurally for a fixture pair, so
        only the settlement source and the void handling are genuinely evaluated -
        and both fall to ordinary venue prose. The cap is also right on the merits:
        Kalshi extends a postponed market two days then resolves "to a fair price",
        while Polymarket waits until the game is played.
        """
        poly = poly_game()
        poly = poly.model_copy(
            update={
                "settlement_rules_raw": poly.settlement_rules_raw.replace(
                    "this market will resolve 50-50",
                    "this market will be voided and all stakes refunded",
                )
                + " The resolution source is official MLB information.",
                "settlement_source": "official league",
            }
        )
        kalshi = kalshi_game()
        kalshi = kalshi.model_copy(
            update={
                "settlement_rules_raw": kalshi.settlement_rules_raw.replace(
                    "the market will resolve to a fair price in accordance with the rules",
                    "the market will be voided and all stakes refunded",
                ),
                "settlement_source": "official league",
            }
        )
        cands = generate_candidates([kalshi], [poly])
        assert cands and cands[0].fixture is not None
        score = score_equivalence(cands[0])
        assert not score.verdict.allows_guaranteed_arbitrage, score.verdict


class TestTheInstructionNamesTheOutcome:
    """"YES" and "NO" name opposite teams on a market whose outcomes are teams.

    Half of all fixture pairs are polarity-inverted, so the same Polymarket market
    appears as YES in one opportunity and NO in another. A reader resolving "YES" the
    obvious way buys the wrong side and is unhedged, so the leg has to say which
    outcome it actually buys.
    """

    def opportunity(self, kalshi_pays_on: str):  # noqa: ANN201
        from pmvl_shared.schemas import BookLevel, OrderBook

        from pmvl_markets.arbitrage.cross_platform import scan_cross_platform

        kalshi = kalshi_game(
            side=kalshi_pays_on,
            market_id=f"KXMLBGAME-26SEP112215SDSF-{kalshi_pays_on[:2].upper()}",
        )
        poly = poly_game()
        candidates = generate_candidates([kalshi], [poly])
        assert candidates
        candidate = candidates[0]
        inverted = candidate.fixture_polarity_inverted

        def book(platform, market_id, yes, no):  # noqa: ANN001, ANN202
            return OrderBook(
                platform=platform, platform_market_id=market_id, observed_at=utcnow(),
                yes_asks=[BookLevel(price=Decimal(yes), size=Decimal("4000"))],
                no_asks=[BookLevel(price=Decimal(no), size=Decimal("4000"))],
                yes_bids=[], no_bids=[],
            )

        result = scan_cross_platform(
            kalshi,
            book(Platform.KALSHI, kalshi.platform_market_id, "0.40", "0.62"),
            poly,
            book(
                Platform.POLYMARKET, poly.platform_market_id,
                "0.40" if inverted else "0.62",
                "0.62" if inverted else "0.40",
            ),
            score_equivalence(candidate),
        )
        assert result is not None
        return result

    def test_a_team_named_leg_carries_its_outcome(self) -> None:
        result = self.opportunity("San Diego")
        poly_leg = next(l for l in result.legs if l.platform is Platform.POLYMARKET)
        assert poly_leg.outcome_label == "San Francisco Giants"

    def test_the_inverted_orientation_names_the_other_team(self) -> None:
        result = self.opportunity("San Francisco")
        poly_leg = next(l for l in result.legs if l.platform is Platform.POLYMARKET)
        assert poly_leg.outcome_label == "San Diego Padres"

    def test_a_yes_no_market_adds_no_noise(self) -> None:
        """Kalshi's outcomes really are Yes/No, so the side says everything."""
        result = self.opportunity("San Diego")
        kalshi_leg = next(l for l in result.legs if l.platform is Platform.KALSHI)
        assert kalshi_leg.outcome_label == ""

    def test_the_headline_distinguishes_the_two_orientations(self) -> None:
        """Previously both read "kalshi YES / polymarket YES"."""
        assert "Philadelphia" not in self.opportunity("San Diego").title
        inverted = self.opportunity("San Francisco").title
        assert "San Diego Padres" in inverted


class TestTheContestDateIsAPropertyOfTheRow:
    """Not of the wall clock.

    97 of 99 real Polymarket fixtures state no year, and the shared date extractor
    falls back to the current one - so the same unchanged market named a different
    contest depending on when it was parsed, and a stored match could not be
    reproduced from the row it came from.
    """

    def poly_with_timing(self, when: str, close: datetime) -> NormalizedMarket:
        return poly_game(when=when).model_copy(
            update={
                "close_time": close,
                "expected_resolution_time": close + timedelta(hours=3),
                "event_occurrence_time": None,
            }
        )

    def test_a_yearless_date_is_anchored_to_the_market_not_to_today(self) -> None:
        market = self.poly_with_timing(
            "December 28 at 7:05 PM ET",
            datetime(2027, 12, 28, 23, 5, tzinfo=UTC),
        )
        fixture = extract_fixture(market)
        assert fixture is not None
        assert fixture.scheduled == date(2027, 12, 28)

    def test_a_contest_in_december_resolved_by_a_january_close(self) -> None:
        """The neighbouring year has to be considered, not just the close's own."""
        market = self.poly_with_timing(
            "December 31 at 8:00 PM ET",
            datetime(2028, 1, 1, 4, 0, tzinfo=UTC),
        )
        fixture = extract_fixture(market)
        assert fixture is not None
        assert fixture.scheduled == date(2027, 12, 31)

    def test_an_explicit_year_is_trusted_over_the_anchor(self) -> None:
        market = self.poly_with_timing(
            "September 11, 2026 at 10:15 PM ET",
            datetime(2026, 9, 12, 4, 0, tzinfo=UTC),
        )
        assert extract_fixture(market).scheduled == date(2026, 9, 11)

    def test_a_date_implausibly_far_from_the_market_is_refused(self) -> None:
        """The fixture proposer bypasses the resolution-gap gate, so this is the
        only corroboration the contest date gets."""
        market = self.poly_with_timing(
            "March 3, 2019 at 7:05 PM ET",
            datetime(2026, 9, 12, 4, 0, tzinfo=UTC),
        )
        assert extract_fixture(market) is None
