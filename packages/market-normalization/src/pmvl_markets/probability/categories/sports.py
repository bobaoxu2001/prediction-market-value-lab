"""Sports base-rate model: Log5 on team records, with home-field advantage.

For a head-to-head contract ("Houston vs San Diego Winner?", YES = San Diego) the
probability comes from the two teams' win rates over completed games, combined with
Bill James's Log5:

    P(A beats B) = (pA - pA*pB) / (pA + pB - 2*pA*pB)

then adjusted for home field in log-odds space. Both inputs are counts of games that
had already finished, so the estimate never observes a prediction-market price and
can legitimately support an edge.

## What this model is, honestly

It is a **weak** prior. Win-loss record ignores starting pitcher, injuries, rest,
travel, bullpen state and every in-season roster change - all of which the betting
market prices and this does not. The expectation going in is that it does **not**
beat the market, and the point of shipping it is that this is now a measurable claim
rather than an assumption: :mod:`pmvl_markets.retrodiction` scores it against the
market's own price, and ``SPORTS_MODEL_ENABLED`` decides whether it is allowed to
produce recommendations on the strength of that measurement.

Confidence is capped low and the interval is wide, so even where it is used it
cannot dominate a pool. That is not a fudge to make it look safe: a model built from
two integers should not be able to outvote one built from a physical forecast.

## Identification, and why it is anchored to the schedule

Kalshi encodes everything needed in the ticker:

    KXMLBGAME-26AUG092020HOUSD-SD
    \\_______/ \\____/\\__/\\___/ \\/
     series     date  time teams  the team YES backs

The two team codes are variable length and run together (``HOUSD`` is ``HOU`` +
``SD``, but ``LAALAD`` is ``LAA`` + ``LAD``), so splitting the string by guesswork is
ambiguous. Instead the date is parsed and the real fixture list for that day is
fetched: the correct split is the one that reproduces an actual scheduled game. That
also validates the home/away convention rather than assuming it - the first code is
the away side, verified against ESPN for three separate fixtures.

An unresolvable ticker is ``no_opinion``. Guessing which team a contract is on would
invert the estimate half the time, which is worse than silence.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, clamp_prob, quantize_prob
from pmvl_shared.timeutil import ensure_utc, utcnow

from ...providers.espn import (
    HOME_WIN_RATE,
    LEAGUE_PATHS,
    ESPNProvider,
    ScheduledGame,
    TeamRecord,
)
from ..base import (
    ModelContext,
    ModelEstimate,
    ProbabilityModel,
    lookahead_guard,
    no_opinion,
)

log = get_logger(__name__)

#: Kalshi stamps game tickers in US Eastern time, confirmed against ESPN: ticker
#: ``26AUG091605DETSF`` is a 16:05 ET first pitch, which ESPN reports as 20:05Z.
TICKER_TIMEZONE = ZoneInfo("America/New_York")

#: Two shapes are in use, and the time is what differs:
#:
#:     KXMLBGAME-26AUG092020HOUSD-SD     date + HHMM + teams
#:     KXNFLGAME-26AUG15DALSEA-SEA       date + teams
#:
#: The optional ``\d{4}`` is unambiguous because team codes are letters, so the
#: digits can only be a time.
_GAME_TICKER_RE = re.compile(
    r"^(?P<series>KX[A-Z]+GAME)-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?:(?P<hh>\d{2})(?P<mi>\d{2}))?"
    r"(?P<teams>[A-Z]+)-"
    r"(?P<pick>[A-Z]+)$"
)

#: Team codes where the venue and ESPN disagree, mapped venue -> ESPN.
#:
#: Found by diffing every code in the live board against ESPN's own abbreviations
#: rather than assembled from memory; on MLB exactly two differ. Anything not
#: listed is assumed identical, and a code that then fails to identify a fixture
#: produces ``no_opinion`` rather than a guess.
TEAM_CODE_ALIASES: dict[str, str] = {
    # MLB
    "AZ": "ARI",    # Arizona Diamondbacks
    "CWS": "CHW",   # Chicago White Sox
    # WNBA
    "PDX": "POR",   # Portland Fire
    "CONN": "CON",  # Connecticut Sun
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: Strength of the shrinkage prior, in pseudo-games at .500 added to each side.
#:
#: A team that is 3-0 has a win rate of 1.0, and Log5 against it returns a certainty
#: that is entirely an artefact of a three-game sample. Beta(k, k) shrinkage pulls
#: early-season records toward .500 and becomes negligible once a real season has
#: accumulated. Eight is roughly a fortnight of baseball and a fifth of an NFL
#: season, which is the right order for both.
SHRINKAGE_GAMES = 8

#: Below this many completed games the record is not evidence of anything.
MIN_GAMES = 10


def log5(p_a: float, p_b: float) -> float:
    """P(A beats B) given each side's win rate against an average opponent.

    Degenerate when both sides are 0 or both are 1 - two winless teams must still
    produce a game - so those collapse to a coin flip rather than dividing by zero.
    """
    denominator = p_a + p_b - 2.0 * p_a * p_b
    if denominator <= 0:
        return 0.5
    return (p_a - p_a * p_b) / denominator


def shrunk_win_rate(record: TeamRecord, *, k: int = SHRINKAGE_GAMES) -> float:
    """Win rate pulled toward .500 by ``k`` pseudo-games on each side."""
    return (record.wins + k) / (record.games + 2 * k)


def apply_home_advantage(probability: float, home_rate: float) -> float:
    """Nudge a neutral-site probability toward the home side, in log-odds space.

    Additive in log-odds rather than in probability because the same advantage
    should move a coin flip much further than it moves a near-certainty. Adding
    0.04 to a probability of 0.97 would be a bigger real change than adding it to
    0.50, which is not what a home crowd does.
    """
    if not (0.0 < probability < 1.0):
        return probability
    if not (0.0 < home_rate < 1.0):
        return probability
    bump = math.log(home_rate / (1.0 - home_rate))
    odds = math.log(probability / (1.0 - probability))
    return 1.0 / (1.0 + math.exp(-(odds + bump)))


def parse_game_ticker(ticker: str) -> dict[str, object] | None:
    """Series, the league's local game date, the start if stated, and team codes.

    ``event_date`` is an Eastern-time calendar date and is always present;
    ``start_time`` is a UTC instant and is present only for the series that stamp a
    time. Keeping them separate matters: the date is what ESPN's scoreboard is
    keyed on, and the instant is only a refinement for picking one game out of a
    series.
    """
    match = _GAME_TICKER_RE.match(ticker.strip().upper())
    if match is None:
        return None
    month = _MONTHS.get(match.group("mon"))
    if month is None:
        return None

    hour, minute = match.group("hh"), match.group("mi")
    try:
        event_date = date(2000 + int(match.group("yy")), month, int(match.group("dd")))
        start_time = (
            ensure_utc(
                datetime(
                    event_date.year,
                    event_date.month,
                    event_date.day,
                    int(hour),
                    int(minute),
                    tzinfo=TICKER_TIMEZONE,
                )
            )
            if hour is not None and minute is not None
            else None
        )
    except ValueError:
        return None

    return {
        "series": match.group("series"),
        "event_date": event_date,
        "start_time": start_time,
        "teams": match.group("teams"),
        "pick": match.group("pick"),
    }


#: How far a candidate fixture's start may sit from the ticker's stamp and still be
#: considered the same game. Generous enough to absorb a schedule change or a
#: rounded ticker stamp, far tighter than the gap between games in a series.
FIXTURE_TIME_TOLERANCE = timedelta(hours=6)


#: ESPN code -> every code the venue might write it as, including itself.
_ACCEPTED_CODES: dict[str, set[str]] = {}
for _venue_code, _espn_code in TEAM_CODE_ALIASES.items():
    _ACCEPTED_CODES.setdefault(_espn_code, {_espn_code}).add(_venue_code)


def _codes_for(espn_abbrev: str) -> set[str]:
    return _ACCEPTED_CODES.get(espn_abbrev, {espn_abbrev})


def _possible_segments(game: ScheduledGame) -> set[str]:
    """Every ``away+home`` string this fixture could be written as.

    Expanding the fixture is the tractable direction. The reverse - splitting
    ``LADAZ`` into two codes - cannot be done without already knowing them, since
    the codes are variable length and run together with no separator.
    """
    return {
        f"{away}{home}"
        for away in _codes_for(game.away_abbrev)
        for home in _codes_for(game.home_abbrev)
    }


def resolve_fixture(
    teams_segment: str,
    games: list[ScheduledGame],
    *,
    expected_start: datetime | None = None,
) -> ScheduledGame | None:
    """The scheduled game whose away+home abbreviations spell ``teams_segment``.

    Resolving by reproduction rather than by splitting. ``HOUSD`` is ambiguous in
    isolation - it could be ``HOU``+``SD`` or ``HO``+``USD`` - and unambiguous once
    only one real fixture concatenates to it.

    Teams play series, so the same matchup recurs on consecutive days and the
    candidate list spans two UTC days. ``expected_start`` breaks that tie by start
    time; without it, or if the nearest candidate is still further away than
    :data:`FIXTURE_TIME_TOLERANCE`, the identification is refused rather than
    guessed. Picking the wrong day of a series would pair the contract with the
    wrong game and, on the historical path, with the wrong records.
    """
    matches = [
        game for game in games if teams_segment in _possible_segments(game)
    ]
    if not matches:
        return None
    if len(matches) == 1 and expected_start is None:
        return matches[0]
    if expected_start is None:
        return None

    nearest = min(matches, key=lambda g: abs(g.start_time - expected_start))
    if abs(nearest.start_time - expected_start) > FIXTURE_TIME_TOLERANCE:
        return None
    return nearest


class SportsBaseRateModel(ProbabilityModel):
    """Log5 over win-loss records for head-to-head game contracts."""

    name = "sports_base_rate"
    categories = (Category.SPORTS,)
    independent = True
    #: Deliberately low. This is two integers per team against a market that prices
    #: starting pitchers and injury reports.
    max_confidence = Decimal("0.30")
    #: Records are counted from completed games with a strict cutoff, and the
    #: scoreboard is used only for identification. See `providers.espn`.
    supports_as_of = True

    def __init__(self, provider: ESPNProvider | None = None) -> None:
        self._provider = provider or ESPNProvider()

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        if (declined := lookahead_guard(self, ctx)) is not None:
            return declined

        settings = get_settings()
        if not getattr(settings, "sports_model_enabled", False):
            return no_opinion(
                "sports base-rate model is disabled (SPORTS_MODEL_ENABLED=false); "
                "it is enabled only on evidence that it beats the market price"
            )

        market = ctx.market
        parsed = parse_game_ticker(market.platform_market_id)
        if parsed is None:
            return no_opinion(
                "not a recognised head-to-head game ticker; this model prices only "
                "single-game winner contracts, not futures, props or field markets"
            )

        league_path = LEAGUE_PATHS.get(str(parsed["series"]))
        if league_path is None:
            return no_opinion(
                f"no record-based model for series {parsed['series']}; leagues with "
                "draws are excluded because a two-outcome Log5 does not describe them"
            )

        event_date: date = parsed["event_date"]  # type: ignore[assignment]
        ticker_start: datetime | None = parsed["start_time"]  # type: ignore[assignment]
        evaluated_at = ensure_utc(ctx.evaluation_time or utcnow())

        # ESPN keys its scoreboard on the league's own local date, and so does the
        # Kalshi ticker, so one request on that date is the whole candidate set.
        games = await self._provider.games_on(league_path, event_date)
        fixture = resolve_fixture(
            str(parsed["teams"]), games, expected_start=ticker_start
        )
        if fixture is None:
            return no_opinion(
                f"could not match ticker teams {parsed['teams']!r} to exactly one "
                f"{league_path} fixture on {event_date.isoformat()}; refusing "
                "to guess which side the contract is on"
            )

        # Prefer the fixture's own start over the ticker's, and require one: the
        # date-only series (NFL, WNBA) have no time in the ticker at all, and
        # "has the game started" is not answerable without it.
        start_time = fixture.start_time
        if evaluated_at >= start_time:
            return no_opinion(
                "the game has already started at the evaluation instant; a pre-game "
                "record is not a forecast of a game in progress"
            )

        pick = str(parsed["pick"])
        if pick in _codes_for(fixture.home_abbrev):
            pick_is_home = True
        elif pick in _codes_for(fixture.away_abbrev):
            pick_is_home = False
        else:
            return no_opinion(
                f"the contract's team code {pick!r} is neither side of the matched "
                f"fixture ({fixture.away_abbrev} at {fixture.home_abbrev})"
            )

        home_record = await self._provider.team_record(
            league_path, fixture.home_team_id, before=evaluated_at
        )
        away_record = await self._provider.team_record(
            league_path, fixture.away_team_id, before=evaluated_at
        )
        if home_record is None or away_record is None:
            return no_opinion("ESPN schedule unavailable for one or both teams")
        if home_record.games < MIN_GAMES or away_record.games < MIN_GAMES:
            return no_opinion(
                f"too few completed games to form a record "
                f"({fixture.away_abbrev} {away_record.games}, "
                f"{fixture.home_abbrev} {home_record.games}; minimum {MIN_GAMES})"
            )

        home_rate = shrunk_win_rate(home_record)
        away_rate = shrunk_win_rate(away_record)
        league_home_rate = HOME_WIN_RATE.get(league_path, 0.54)

        p_home = apply_home_advantage(log5(home_rate, away_rate), league_home_rate)
        probability = p_home if pick_is_home else 1.0 - p_home

        # Interval from the binomial error on each record, propagated the same way
        # the crypto model propagates volatility error: re-evaluate at the bounds.
        stdev = _record_uncertainty(
            home_record, away_record, league_home_rate, pick_is_home
        )

        confidence = float(self.max_confidence)
        # Fewer games, less trust - on top of the shrinkage already applied.
        sample = min(home_record.games, away_record.games)
        if sample < 30:
            confidence *= max(0.4, sample / 30.0)

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(D(str(probability)))),
            confidence=quantize_prob(D(str(confidence))),
            stdev=max(D(str(stdev)), Decimal("0.04")),
            independent=True,
            detail=(
                f"Log5 {fixture.away_abbrev} ({away_record.wins}-{away_record.losses}) "
                f"at {fixture.home_abbrev} ({home_record.wins}-{home_record.losses}), "
                f"home edge {league_home_rate:.3f} -> P({pick})={probability:.3f}"
            ),
            data_freshness_seconds=0,
            data={
                "league": league_path,
                "fixture": f"{fixture.away_abbrev}@{fixture.home_abbrev}",
                "pick": pick,
                "pick_is_home": pick_is_home,
                "home_record": f"{home_record.wins}-{home_record.losses}",
                "away_record": f"{away_record.wins}-{away_record.losses}",
                "records_counted_before": home_record.before.isoformat(),
                "home_win_rate_assumption": f"{league_home_rate:.3f}",
                "shrinkage_pseudo_games": SHRINKAGE_GAMES,
                "model": "log5_base_rate",
                "known_limitation": (
                    "record only; ignores starting pitcher, injuries, rest and "
                    "roster changes, all of which the market prices"
                ),
            },
        )


def _record_uncertainty(
    home: TeamRecord,
    away: TeamRecord,
    league_home_rate: float,
    pick_is_home: bool,
) -> float:
    """Half-width of the estimate under one binomial standard error per record."""

    def probability_at(home_rate: float, away_rate: float) -> float:
        p_home = apply_home_advantage(log5(home_rate, away_rate), league_home_rate)
        return p_home if pick_is_home else 1.0 - p_home

    def standard_error(record: TeamRecord) -> float:
        rate = shrunk_win_rate(record)
        n = record.games + 2 * SHRINKAGE_GAMES
        return math.sqrt(max(rate * (1.0 - rate), 1e-9) / max(n, 1))

    home_rate, away_rate = shrunk_win_rate(home), shrunk_win_rate(away)
    home_se, away_se = standard_error(home), standard_error(away)

    high = probability_at(
        min(0.999, home_rate + home_se), max(0.001, away_rate - away_se)
    )
    low = probability_at(
        max(0.001, home_rate - home_se), min(0.999, away_rate + away_se)
    )
    return abs(high - low) / 2.0
