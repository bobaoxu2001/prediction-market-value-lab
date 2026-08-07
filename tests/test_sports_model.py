"""Sports base-rate model: Log5, ticker identification, and the ESPN record trap."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, Platform
from pmvl_shared.schemas import NormalizedMarket

from pmvl_markets.probability.base import ModelContext
from pmvl_markets.probability.categories.sports import (
    MIN_GAMES,
    SHRINKAGE_GAMES,
    SportsBaseRateModel,
    apply_home_advantage,
    log5,
    parse_game_ticker,
    resolve_fixture,
    shrunk_win_rate,
)
from pmvl_markets.providers.espn import ScheduledGame, TeamRecord

UTC = timezone.utc
FIRST_PITCH = datetime(2026, 8, 10, 0, 20, tzinfo=UTC)  # 20:20 ET on 9 Aug


# ------------------------------------------------------------------------ log5
def test_log5_is_symmetric_around_equal_teams():
    assert log5(0.6, 0.6) == pytest.approx(0.5)


def test_log5_favours_the_better_team():
    assert log5(0.7, 0.3) > 0.8
    assert log5(0.3, 0.7) < 0.2


def test_log5_complements_correctly():
    assert log5(0.62, 0.41) + log5(0.41, 0.62) == pytest.approx(1.0)


def test_log5_survives_two_winless_teams():
    """Two 0.000 teams still have to play each other."""
    assert log5(0.0, 0.0) == 0.5
    assert log5(1.0, 1.0) == 0.5


def test_shrinkage_pulls_a_tiny_sample_toward_even():
    perfect = TeamRecord(wins=3, losses=0, before=FIRST_PITCH)
    assert shrunk_win_rate(perfect) == pytest.approx((3 + 8) / (3 + 16))
    # Nowhere near the 1.000 the raw record claims.
    assert shrunk_win_rate(perfect) < 0.6


def test_shrinkage_becomes_negligible_over_a_full_season():
    late = TeamRecord(wins=95, losses=55, before=FIRST_PITCH)
    assert shrunk_win_rate(late) == pytest.approx(95 / 150, abs=0.02)


def test_home_advantage_moves_a_coin_flip_more_than_a_near_certainty():
    """The reason the bump is applied in log-odds and not to the probability."""
    flip_gain = apply_home_advantage(0.50, 0.54) - 0.50
    sure_gain = apply_home_advantage(0.97, 0.54) - 0.97
    assert flip_gain > sure_gain > 0


def test_home_advantage_is_neutral_at_a_fifty_percent_rate():
    assert apply_home_advantage(0.63, 0.50) == pytest.approx(0.63)


# ------------------------------------------------------------ ticker parsing
def test_parse_game_ticker_reads_eastern_time():
    """Verified against ESPN: 26AUG092020HOUSD is a 00:20Z first pitch on 10 Aug."""
    parsed = parse_game_ticker("KXMLBGAME-26AUG092020HOUSD-SD")

    assert parsed is not None
    assert parsed["series"] == "KXMLBGAME"
    assert parsed["start_time"] == FIRST_PITCH
    assert parsed["teams"] == "HOUSD"
    assert parsed["pick"] == "SD"


def test_parse_game_ticker_reads_the_date_only_form():
    """NFL and WNBA stamp no time: `KXNFLGAME-26AUG15DALSEA-SEA`."""
    parsed = parse_game_ticker("KXNFLGAME-26AUG15DALSEA-SEA")

    assert parsed is not None
    assert parsed["event_date"] == date(2026, 8, 15)
    assert parsed["start_time"] is None
    assert parsed["teams"] == "DALSEA"
    assert parsed["pick"] == "SEA"


def test_parse_game_ticker_keeps_the_local_date_for_a_late_game():
    """The 20:20 ET first pitch is 00:20Z the next day; ESPN keys on the 9th.

    Deriving the scoreboard date from the UTC instant instead queried the 10th,
    which returned that day's other games - a non-empty list without the fixture.
    """
    parsed = parse_game_ticker("KXMLBGAME-26AUG092020HOUSD-SD")

    assert parsed["event_date"] == date(2026, 8, 9)
    assert parsed["start_time"] == FIRST_PITCH
    assert FIRST_PITCH.date() == date(2026, 8, 10)


def test_team_code_aliases_bridge_venue_and_espn_spellings():
    """Kalshi writes AZ and CWS where ESPN writes ARI and CHW."""
    games = [_game("LAD", "ARI"), _game("CLE", "CHW")]

    assert resolve_fixture("LADAZ", games) is games[0]
    assert resolve_fixture("CLECWS", games) is games[1]
    # The ESPN spelling still resolves, so the alias is additive, not a rename.
    assert resolve_fixture("LADARI", games) is games[0]


def test_parse_game_ticker_rejects_a_non_game_ticker():
    assert parse_game_ticker("KXGOLFMAJOR-26-SCHEFFLER") is None
    assert parse_game_ticker("KXMLBGAME-BOGUS") is None


def test_parse_game_ticker_rejects_an_impossible_date():
    assert parse_game_ticker("KXMLBGAME-26FEB302020HOUSD-SD") is None


def test_resolve_fixture_disambiguates_run_together_team_codes():
    """`HOUSD` could be HOU+SD or HO+USD; only the real schedule settles it."""
    games = [
        _game("HOU", "SD"),
        _game("DET", "SF"),
    ]
    fixture = resolve_fixture("HOUSD", games)

    assert fixture is not None
    assert (fixture.away_abbrev, fixture.home_abbrev) == ("HOU", "SD")


def test_resolve_fixture_refuses_an_ambiguous_match_with_no_time_to_go_on():
    games = [_game("HOU", "SD"), _game("HOU", "SD")]
    assert resolve_fixture("HOUSD", games) is None


def test_resolve_fixture_picks_the_right_day_of_a_series():
    """Teams play series, so the same matchup recurs across the candidate days."""
    yesterday = _game("HOU", "SD", start=FIRST_PITCH - timedelta(days=1))
    today = _game("HOU", "SD", start=FIRST_PITCH)
    fixture = resolve_fixture(
        "HOUSD", [yesterday, today], expected_start=FIRST_PITCH
    )

    assert fixture is today


def test_resolve_fixture_refuses_when_the_nearest_game_is_a_different_day():
    stale = _game("HOU", "SD", start=FIRST_PITCH - timedelta(days=2))
    assert resolve_fixture("HOUSD", [stale], expected_start=FIRST_PITCH) is None


def test_resolve_fixture_returns_none_when_nothing_matches():
    assert resolve_fixture("NYYBOS", [_game("HOU", "SD")]) is None


def _game(away: str, home: str, *, start: datetime = FIRST_PITCH) -> ScheduledGame:
    return ScheduledGame(
        start_time=start,
        home_abbrev=home,
        away_abbrev=away,
        home_name=f"{home} team",
        away_name=f"{away} team",
        home_team_id=f"id-{home}",
        away_team_id=f"id-{away}",
    )


# ------------------------------------------------------------------ the model
class _FakeESPN:
    """Stands in for the provider, and records what the model asked it for.

    The `before` cutoffs it captures are the evidence that the model requested
    records bounded by the evaluation instant rather than current ones.
    """

    def __init__(self, records: dict[str, tuple[int, int]], games=None) -> None:
        self._records = records
        self._games = games if games is not None else [_game("HOU", "SD")]
        self.record_calls: list[tuple[str, datetime]] = []

    async def games_on(self, league_path, day):
        return self._games

    async def team_record(self, league_path, team_id, *, before):
        self.record_calls.append((team_id, before))
        if team_id not in self._records:
            return None
        wins, losses = self._records[team_id]
        return TeamRecord(wins=wins, losses=losses, before=before)

    async def aclose(self):
        return None


def make_market(ticker: str = "KXMLBGAME-26AUG092020HOUSD-SD") -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id=ticker,
        series_ticker="KXMLBGAME",
        title="Houston vs San Diego Winner?",
        subtitle="San Diego",
        category=Category.SPORTS,
        open_time=FIRST_PITCH - timedelta(days=3),
        close_time=FIRST_PITCH,
        expected_resolution_time=FIRST_PITCH,
    )


def estimate(model: SportsBaseRateModel, *, now=None, as_of=None):
    ctx = ModelContext(market=make_market(), now=now, as_of=as_of)
    return asyncio.run(model.estimate(ctx))


@pytest.fixture
def enabled(monkeypatch):
    from pmvl_shared import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "sports_model_enabled", True, raising=False)
    return settings


def test_model_is_off_by_default(monkeypatch):
    """An unmeasured model must not join the edge-bearing estimate on its own."""
    from pmvl_shared import config

    monkeypatch.setattr(
        config.get_settings(), "sports_model_enabled", False, raising=False
    )
    model = SportsBaseRateModel(_FakeESPN({"id-SD": (60, 50), "id-HOU": (50, 60)}))
    result = estimate(model, now=FIRST_PITCH - timedelta(hours=3))

    assert result.probability is None
    assert "SPORTS_MODEL_ENABLED" in result.detail


def test_model_prices_a_game_from_records(enabled):
    model = SportsBaseRateModel(_FakeESPN({"id-SD": (70, 40), "id-HOU": (45, 65)}))
    result = estimate(model, now=FIRST_PITCH - timedelta(hours=3))

    # SD is both the better team and at home, so YES (San Diego) is a clear favourite.
    assert result.probability is not None
    assert result.probability > Decimal("0.65")
    assert result.data["pick_is_home"] is True
    assert result.data["home_record"] == "70-40"


def test_the_away_side_of_the_same_fixture_is_the_complement(enabled):
    home_side = SportsBaseRateModel(
        _FakeESPN({"id-SD": (70, 40), "id-HOU": (45, 65)})
    )
    away_side = SportsBaseRateModel(
        _FakeESPN({"id-SD": (70, 40), "id-HOU": (45, 65)})
    )
    p_sd = estimate(home_side, now=FIRST_PITCH - timedelta(hours=3)).probability
    ctx = ModelContext(
        market=make_market("KXMLBGAME-26AUG092020HOUSD-HOU"),
        now=FIRST_PITCH - timedelta(hours=3),
    )
    p_hou = asyncio.run(away_side.estimate(ctx)).probability

    assert float(p_sd) + float(p_hou) == pytest.approx(1.0, abs=0.002)


def test_confidence_stays_low_even_with_a_full_season(enabled):
    """Two integers must never outvote a physical forecast."""
    model = SportsBaseRateModel(_FakeESPN({"id-SD": (100, 40), "id-HOU": (40, 100)}))
    result = estimate(model, now=FIRST_PITCH - timedelta(hours=3))

    assert result.confidence <= SportsBaseRateModel.max_confidence
    assert result.confidence <= Decimal("0.30")


def test_declines_a_thin_record(enabled):
    model = SportsBaseRateModel(_FakeESPN({"id-SD": (3, 2), "id-HOU": (2, 3)}))
    result = estimate(model, now=FIRST_PITCH - timedelta(hours=3))

    assert result.probability is None
    assert "too few completed games" in result.detail


def test_declines_a_non_game_contract(enabled):
    model = SportsBaseRateModel(_FakeESPN({}))
    ctx = ModelContext(
        market=make_market("KXGOLFMAJOR-27-RORY"),
        now=FIRST_PITCH - timedelta(hours=3),
    )
    result = asyncio.run(model.estimate(ctx))

    assert result.probability is None
    assert "head-to-head game ticker" in result.detail


def test_declines_a_league_with_draws(enabled):
    """Two-outcome Log5 does not describe a match that can end level."""
    model = SportsBaseRateModel(_FakeESPN({}))
    ctx = ModelContext(
        market=make_market("KXEPLGAME-26AUG091200ARSCHE-ARS"),
        now=FIRST_PITCH - timedelta(hours=3),
    )
    result = asyncio.run(model.estimate(ctx))

    assert result.probability is None
    assert "no record-based model for series" in result.detail


def test_declines_once_the_game_has_started(enabled):
    model = SportsBaseRateModel(_FakeESPN({"id-SD": (70, 40), "id-HOU": (45, 65)}))
    result = estimate(model, now=FIRST_PITCH + timedelta(minutes=1))

    assert result.probability is None
    assert "already started" in result.detail


def test_declines_when_the_fixture_cannot_be_identified(enabled):
    model = SportsBaseRateModel(
        _FakeESPN({"id-SD": (70, 40)}, games=[_game("NYY", "BOS")])
    )
    result = estimate(model, now=FIRST_PITCH - timedelta(hours=3))

    assert result.probability is None
    assert "refusing to guess" in result.detail


# ----------------------------------------------------- the look-ahead defence
def test_records_are_requested_as_of_the_evaluation_instant(enabled):
    """The whole reason this model may declare `supports_as_of`.

    ESPN's scoreboard reports a record that INCLUDES the game being played, so a
    record fetched without a cutoff would contain the result being forecast. The
    model must ask for records bounded by the instant it is forecasting from.
    """
    fake = _FakeESPN({"id-SD": (70, 40), "id-HOU": (45, 65)})
    model = SportsBaseRateModel(fake)
    as_of = FIRST_PITCH - timedelta(days=2)

    estimate(model, as_of=as_of)

    assert fake.record_calls, "no record was requested"
    for _team_id, before in fake.record_calls:
        assert before == as_of


def test_live_path_bounds_records_at_now_not_at_the_game(enabled):
    """Even live, the cutoff is the present - never the game's own start time."""
    fake = _FakeESPN({"id-SD": (70, 40), "id-HOU": (45, 65)})
    model = SportsBaseRateModel(fake)
    now = FIRST_PITCH - timedelta(hours=5)

    estimate(model, now=now)

    for _team_id, before in fake.record_calls:
        assert before == now
        assert before < FIRST_PITCH


def test_model_declares_its_own_limitation_on_every_estimate(enabled):
    model = SportsBaseRateModel(_FakeESPN({"id-SD": (70, 40), "id-HOU": (45, 65)}))
    result = estimate(model, now=FIRST_PITCH - timedelta(hours=3))

    assert "injuries" in result.data["known_limitation"]
    assert result.data["records_counted_before"]
