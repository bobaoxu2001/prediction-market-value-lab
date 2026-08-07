"""ESPN's public scoreboard and schedule feeds.

Used by the sports base-rate model for two things, and nothing else: **which teams
are playing** (identification) and **what each team's record was before a given
instant** (evidence). No credentials, in keeping with every other source here.

## The look-ahead trap this module exists to close

ESPN's scoreboard reports a competitor's record *including the game being shown*.
Checked against the Toronto Blue Jays over three consecutive days in August 2026:

===========  ======  ==============
Date         Result  Record reported
===========  ======  ==============
2026-08-01   won     52-59
2026-08-02   lost    52-60
2026-08-03   won     53-60
===========  ======  ==============

Each row's record already contains that row's result. Using the scoreboard record to
forecast that same game therefore feeds the answer into the prediction - a win
literally adds one to the W column of the number being used to predict the win. It
would not error, and it would produce an excellent-looking sports model.

So the scoreboard is used **only for identification**, and :meth:`ESPNProvider.games_on`
strips scores and winners at the provider boundary rather than leaving them in the
payload for a caller to reach into by accident. Records come from
:meth:`ESPNProvider.team_record`, which counts completed games from the team's own
schedule with a strict ``before`` cutoff.

## Stability

These endpoints are public and unauthenticated but **undocumented and unsupported**.
ESPN can change or withdraw them without notice. Every method therefore degrades to
``None`` / empty rather than raising, and the model treats missing data as
``no_opinion``. A sports estimate disappearing is an acceptable failure; a wrong one
is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import ensure_utc, parse_ts

from .http import HttpClient, ProviderError

log = get_logger(__name__)

#: Kalshi series ticker -> the ESPN ``sport/league`` path segment.
#:
#: Only leagues where a simple win/loss record is a meaningful predictor are listed.
#: Soccer is deliberately absent: draws make a two-outcome Log5 the wrong model, and
#: a contract that can resolve NO because nobody won needs different handling.
LEAGUE_PATHS: dict[str, str] = {
    "KXMLBGAME": "baseball/mlb",
    "KXNBAGAME": "basketball/nba",
    "KXWNBAGAME": "basketball/wnba",
    "KXNFLGAME": "football/nfl",
    "KXNHLGAME": "hockey/nhl",
}

#: Long-run home win rate per league, used as the home-field adjustment.
#:
#: These are the *documented historical* rates, not fitted here. Fitting them from
#: the same season the model is forecasting would be a mild in-sample leak for a
#: parameter that barely moves, and hard-coding a published figure is both simpler
#: and easier for a reader to check. They are applied in log-odds space.
HOME_WIN_RATE: dict[str, float] = {
    "baseball/mlb": 0.540,
    "basketball/nba": 0.580,
    "basketball/wnba": 0.570,
    "football/nfl": 0.560,
    "hockey/nhl": 0.550,
}


@dataclass(frozen=True)
class ScheduledGame:
    """A fixture, with every result field deliberately absent.

    There is no ``score`` and no ``winner`` on this type by design. The scoreboard
    payload has both; stripping them here means a future caller cannot reach for
    them without changing this file, which is the point at which someone has to
    think about what they are doing.
    """

    start_time: datetime
    home_abbrev: str
    away_abbrev: str
    home_name: str
    away_name: str
    home_team_id: str
    away_team_id: str


@dataclass(frozen=True)
class TeamRecord:
    """Wins and losses over completed games strictly before a cutoff."""

    wins: int
    losses: int
    #: The cutoff the record was computed against, carried so it can be reported.
    before: datetime

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games if self.games else None


class ESPNProvider:
    """Read-only access to ESPN's public scoreboard and schedule feeds."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self._client = client or HttpClient(
            get_settings().espn_api_base,
            name="espn",
            rate_per_second=3.0,
            cache_ttl_seconds=900.0,
            # ESPN's edge 403s this project's descriptive User-Agent, and every
            # other string outside a small allowlist of known client tokens. See
            # the note in `HttpClient.__init__`.
            identify_self=False,
        )
        #: Season schedules are large (a few MB) and every team in a league is asked
        #: for repeatedly across one scan, so they are cached for the process.
        self._schedule_cache: dict[tuple[str, str, int], list[dict[str, Any]]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ identification
    async def games_on(self, league_path: str, day: date) -> list[ScheduledGame]:
        """Fixtures on a **local** calendar day. Identification only - no results.

        ``day`` is the league's own calendar date, not a UTC one, because that is
        what ESPN's ``dates`` parameter means: querying ``20260809`` returns the
        Houston-at-San Diego game that starts at 00:20Z on the *10th*, because it
        is a 20:20 first pitch on the 9th in Eastern time.

        Taking a ``date`` rather than a ``datetime`` is the fix for a bug worth
        recording: deriving the day from a UTC timestamp turned every West Coast
        night game into a query for the following day, which returned a perfectly
        valid list of other games that simply did not contain the fixture being
        priced. Kalshi stamps its tickers in Eastern time for the same reason, so
        the two now line up exactly and no adjacent-day search is needed.
        """
        try:
            data = await self._client.get_json(
                f"/{league_path}/scoreboard",
                params={"dates": day.strftime("%Y%m%d")},
                allow_404=True,
            )
        except ProviderError as exc:
            log.debug("espn scoreboard failed for %s: %s", league_path, exc)
            return []
        if not isinstance(data, dict):
            return []

        games: list[ScheduledGame] = []
        for event in data.get("events") or []:
            competitions = event.get("competitions") or []
            if not competitions:
                continue
            competition = competitions[0]
            start = parse_ts(competition.get("date") or event.get("date"))
            sides: dict[str, dict[str, Any]] = {}
            for competitor in competition.get("competitors") or []:
                side = competitor.get("homeAway")
                team = competitor.get("team") or {}
                if side in ("home", "away") and team:
                    sides[side] = team
            if start is None or "home" not in sides or "away" not in sides:
                continue

            home, away = sides["home"], sides["away"]
            games.append(
                ScheduledGame(
                    start_time=start,
                    home_abbrev=str(home.get("abbreviation") or "").upper(),
                    away_abbrev=str(away.get("abbreviation") or "").upper(),
                    home_name=str(home.get("displayName") or ""),
                    away_name=str(away.get("displayName") or ""),
                    home_team_id=str(home.get("id") or ""),
                    away_team_id=str(away.get("id") or ""),
                )
            )
        return games

    # ------------------------------------------------------------------ evidence
    async def team_record(
        self, league_path: str, team_id: str, *, before: datetime
    ) -> TeamRecord | None:
        """Wins and losses over games that had **finished** before ``before``.

        The cutoff is on the game's start time and is strict. A game starting at the
        same instant as the forecast is excluded: its result cannot have been known
        to anyone making a prediction at that moment.
        """
        cutoff = ensure_utc(before)
        if cutoff is None:
            return None

        events = await self._season_schedule(league_path, team_id, cutoff.year)
        if not events:
            return None

        wins = losses = 0
        for event in events:
            competitions = event.get("competitions") or []
            if not competitions:
                continue
            competition = competitions[0]
            start = parse_ts(competition.get("date") or event.get("date"))
            if start is None or start >= cutoff:
                continue
            status = (
                ((competition.get("status") or {}).get("type") or {}).get("name") or ""
            )
            if status != "STATUS_FINAL":
                continue

            for competitor in competition.get("competitors") or []:
                if str((competitor.get("team") or {}).get("id")) != str(team_id):
                    continue
                winner = competitor.get("winner")
                if winner is True:
                    wins += 1
                elif winner is False:
                    losses += 1
                # `winner` absent means a tie or an unresolved game; counted as
                # neither rather than guessed at.

        return TeamRecord(wins=wins, losses=losses, before=cutoff)

    async def _season_schedule(
        self, league_path: str, team_id: str, season: int
    ) -> list[dict[str, Any]]:
        key = (league_path, str(team_id), season)
        if key in self._schedule_cache:
            return self._schedule_cache[key]

        try:
            data = await self._client.get_json(
                f"/{league_path}/teams/{team_id}/schedule",
                params={"season": season},
                allow_404=True,
            )
        except ProviderError as exc:
            log.debug("espn schedule failed for %s/%s: %s", league_path, team_id, exc)
            self._schedule_cache[key] = []
            return []

        events = (data or {}).get("events") if isinstance(data, dict) else None
        result = events if isinstance(events, list) else []
        self._schedule_cache[key] = result
        return result
