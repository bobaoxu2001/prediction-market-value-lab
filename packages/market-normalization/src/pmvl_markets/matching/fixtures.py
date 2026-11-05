"""Structural identification of a sporting fixture across the two venues.

The venues title a single game in incompatible shapes, and token similarity cannot
bridge them:

    Kalshi      title "San Diego wins"                        outcomes ["Yes", "No"]
    Polymarket  title "San Diego Padres vs. San Francisco Giants"
                outcomes ["San Diego Padres", "San Francisco Giants"]

Their normalized token sets overlap at 0.286 - below the blocking floor - so the pair
is never even proposed, and sports is by far the largest overlap between the two
venues. Kalshi names the *outcome* and leaves the matchup to its rules text;
Polymarket names the *matchup* and leaves the outcome to its ``outcomes`` array.

What makes this tractable is that both venues restate the fixture in prose. Kalshi's
rules say "the San Diego vs San Francisco professional baseball game originally
scheduled for Sep 11, 2026 at 10:15 PM EDT"; Polymarket's say "In the upcoming MLB
game, scheduled for August 26 at 6:30 AM ET". So the matchup, the competition and the
date are recoverable from both sides without an event row, a third-party fixture feed,
or a hand-maintained team registry.

Four things must agree before two markets are called the same fixture, and every one
of them is load-bearing - each was added because its absence produced a demonstrable
false pair on real rows:

**The competition.** Not the sport: Kalshi names competitors by city alone
("seattle", "toronto"), so without a league a Kalshi MLB game and a Polymarket WNBA
game between the same two cities on the same night certify as one contest. Kalshi's
league is in its event ticker (``KXMLBGAME``), Polymarket's in its prose ("the
upcoming WNBA game"). Both sides must resolve one and they must agree; silence on
either side refuses the fixture rather than waiving the check.

**The competitors.** Matched by prefix-token containment rather than a registry, so
"chicago c" pairs with "Chicago Cubs" while "chicago c" and "Chicago White Sox" do
not. Tokens that *change* an identity rather than extend it ("state", "tech",
"southern") block the match, because "washington" and "washington state" are
different programs, not two spellings of one.

**The date, and the start time.** A series is several games on consecutive days, so
competitors alone would pair Monday's game with Tuesday's. A *doubleheader* is two
games between the same teams on one date, so the date alone is not enough either -
and doubleheaders are what postponements create, the case where these venues' rules
diverge most. For sports that play them, both venues must state a first pitch and the
two must agree; Kalshi's own "game 1 of the double header" marker is captured and must
match when either side carries it.

**The proposition.** Only a head-to-head moneyline. The same fixture also carries
totals, spreads and "team to advance"; a Polymarket title with a ``:`` suffix, or a
market on either venue that publishes a strike, is not a moneyline and is refused.

A fixture match establishes *subject identity* - these two contracts are about the
same side of the same contest. It deliberately does **not** license a risk-free claim:
:func:`pmvl_markets.matching.equivalence.score_equivalence` caps a fixture pair below
the strict verdict by construction. Kalshi bounds postponement to two days and then
resolves "to a fair price in accordance with the rules" while Polymarket waits
indefinitely and voids 50-50, so these legs really can diverge, and the cap means no
coincidence elsewhere in the scoring can ever print "executable" for one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from pmvl_shared.enums import Platform
from pmvl_shared.schemas import NormalizedMarket

from ..normalize.text import expand_entity_aliases, extract_dates

#: How far apart two stated start times may be and still describe one contest.
#: Compared with ``>=`` so the boundary itself separates rather than agrees: a
#: split doubleheader is commonly scheduled exactly three hours apart.
DOUBLEHEADER_SEPARATION_MINUTES = 180

#: Competitions, mapped to the sport they play. The league - not the sport - is the
#: identifying term: "baseball" does not distinguish MLB from the minors, and an
#: empty sport does not distinguish MLB from the WNBA, which is how a Kalshi
#: Seattle/Toronto baseball game certified as a Polymarket Seattle/Toronto
#: basketball game on real rows.
LEAGUE_SPORTS: dict[str, str] = {
    "MLB": "baseball", "KBO": "baseball", "NPB": "baseball",
    "NBA": "basketball", "WNBA": "basketball", "EUROLEAGUE": "basketball",
    "NFL": "football", "CFL": "football", "AFL": "football",
    "NHL": "hockey",
    "NRL": "rugby",
    "MLS": "soccer", "EPL": "soccer", "UEFA": "soccer", "LALIGA": "soccer",
    "BUNDESLIGA": "soccer", "SERIEA": "soccer", "LIGUE1": "soccer",
    "KLEAGUE": "soccer",
    "ATP": "tennis", "WTA": "tennis",
    "UFC": "mma",
}

_LEAGUE_IN_PROSE = re.compile(
    r"\b(" + "|".join(
        ["MLB", "WNBA", "NBA", "NFL", "NHL", "NRL", "MLS", "UFC", "ATP", "WTA",
         "EPL", "UEFA", "KBO", "NPB", "CFL", "EuroLeague",
         r"La\s*Liga", r"Bundesliga", r"Serie\s*A", r"Ligue\s*1", r"K-?League"]
    ) + r")\b",
    re.I,
)

#: Kalshi encodes its league in the event ticker: KXMLBGAME-26SEP112215SDSF.
_KALSHI_LEAGUE_TICKER = re.compile(r"^KX([A-Z0-9]+?)(?:GAME|MATCH)\b")

#: Sports whose fixtures both venues describe in prose, used only as a fallback when
#: neither side names a league (boxing, golf, unaffiliated matches).
_SPORTS = (
    "baseball", "football", "basketball", "hockey", "soccer", "tennis", "golf",
    "cricket", "rugby", "boxing",
)

#: Only these play two contests between the same teams on one date. For them a
#: stated first pitch is required on BOTH sides; demanding one for a sport that
#: cannot have a doubleheader would discard real pairs for no safety gain (every
#: Kalshi NFL market states no clock time at all).
_DOUBLEHEADER_SPORTS = frozenset({"baseball"})

#: Tokens that make a longer name a DIFFERENT entity rather than a fuller spelling
#: of the shorter one. "washington" and "washington state" are different programs;
#: "san diego" and "san diego padres" are one team.
_IDENTITY_CHANGING_TOKENS = frozenset({
    "state", "st", "tech", "a&m", "am", "southern", "northern", "eastern",
    "western", "central", "national", "international", "junior", "reserves",
    "women", "womens", "youth", "u21", "u23", "b",
})

#: Kalshi restates the whole fixture inside the YES condition of its rules:
#:   "If San Diego wins the San Diego vs San Francisco professional baseball game
#:    originally scheduled for Sep 11, 2026 at 10:15 PM EDT, then the market
#:    resolves to Yes."
#: The "wins" phrasing establishes a moneyline rather than a total or a spread. The
#: span is anchored to the end of the condition so a trailing qualifier - "after five
#: complete innings", "by 2 or more runs", "game 1 of the double header" - cannot be
#: silently skipped over, which would pair a derivative with a full-game moneyline.
_KALSHI_FIXTURE = re.compile(
    r"\bif\s+(?P<side>[^.]{2,40}?)\s+wins\s+the\s+"
    r"(?P<a>[^.]{2,40}?)\s+vs\.?\s+(?P<b>[^.]{2,40}?)\s+"
    r"(?:professional\s+|college\s+|international\s+|women'?s\s+|men'?s\s+)*"
    r"(?P<sport>" + "|".join(_SPORTS) + r")\s+game"
    r"(?P<qualifier>[^.,]*?)"
    r"\s*(?:originally\s+)?scheduled\s+for",
    re.I,
)

#: The only qualifier permitted between "game" and "scheduled for". Anything else -
#: an innings limit, a run line - means the market is not a full-game moneyline.
_DOUBLEHEADER_LEG = re.compile(r"\bgame\s*(?P<leg>[12])\s+of\s+the\s+double\s*header\b", re.I)

#: Polymarket puts the matchup in the title. A ``:`` suffix marks a derived
#: proposition on the same fixture - totals, spreads, "team to advance" - which is a
#: different question and must not be paired with a moneyline.
_POLY_FIXTURE = re.compile(r"^\s*(?P<a>[^:]{2,60}?)\s+vs\.?\s+(?P<b>[^:]{2,60}?)\s*$", re.I)

#: Both venues use this phrasing for the kickoff/first-pitch time. "scheduled to
#: begin on" is deliberately NOT accepted: it is series language, and a series-winner
#: market shares its start date with game one.
_SCHEDULED = re.compile(r"(?:originally\s+)?scheduled\s+for\s+", re.I)

#: Wording that means the market is about a multi-game series rather than a contest.
_SERIES_LANGUAGE = re.compile(r"\bbest[\s-]of\b|\bseries\b|\bto\s+advance\b", re.I)

#: "10:15 PM EDT", "6:30 AM ET", "11:00AM ET" - both venues quote a US Eastern clock,
#: so the stated times are directly comparable without conversion.
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\b")

#: Outcome labels that name a derived proposition rather than a competitor.
_NON_COMPETITOR_OUTCOMES = {
    "yes", "no", "over", "under", "draw", "tie", "neither", "other", "any other",
}


@dataclass(frozen=True)
class Fixture:
    """One side of one head-to-head contest, as a venue describes it."""

    #: Canonical league (``"MLB"``), or ``""`` when only a sport was recoverable.
    league: str
    #: Sport implied by the league, or read from prose when no league was named.
    sport: str
    #: The two competitors as written, lowercased, in the order the venue lists them.
    competitors: tuple[str, str]
    #: The date the venue states the contest is scheduled for, compared as written.
    scheduled: date | None
    #: Minutes past midnight of the stated start time, venue-local, or None when the
    #: venue gave only a date.
    start_minute: int | None
    #: "1" or "2" when the venue identifies which half of a doubleheader this is.
    doubleheader_leg: str
    #: Which competitor this particular market pays out on.
    side: str

    @property
    def is_usable(self) -> bool:
        """Whether this fixture carries enough to identify a contest.

        A competition is required. Without one the competitors are just two city
        names, and two different leagues playing the same two cities on one night are
        indistinguishable - a false hedge rather than a missed one.
        """
        return bool(
            self.competitors[0]
            and self.competitors[1]
            and self.scheduled
            and (self.league or self.sport)
        )


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^\w]+", expand_entity_aliases(name)) if t]


def _assignable(short: list[str], long: list[str]) -> bool:
    """Whether every short token prefixes a *distinct* long token."""
    if not short:
        return True
    if len(short) > len(long):
        return False
    head, rest = short[0], short[1:]
    for index, candidate in enumerate(long):
        if not candidate.startswith(head):
            continue
        if _assignable(rest, long[:index] + long[index + 1:]):
            return True
    return False


def competitors_match(left: str, right: str) -> bool:
    """Whether two competitor names denote the same team or player.

    Kalshi writes the city plus a one-letter disambiguator where two franchises share
    one ("Chicago C", "New York Y"); Polymarket writes the full franchise name. Rather
    than maintain a league registry that goes stale every expansion, each token of the
    shorter name must prefix a distinct token of the longer one.

    The leftover tokens of the longer name are then checked: a franchise nickname
    extends an identity ("san diego" -> "san diego padres"), but a qualifier replaces
    one ("washington" -> "washington state" is a different program entirely, and real
    collisions between the Kalshi NFL and Polymarket college catalogs were found this
    way). At least one token of four characters or more is required, so a name that is
    nothing but initials cannot match half the league.
    """
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    short, long = (
        (left_tokens, right_tokens)
        if len(left_tokens) <= len(right_tokens)
        else (right_tokens, left_tokens)
    )
    if not any(len(t) >= 4 for t in short):
        return False
    if not _assignable(short, long):
        return False
    # Whatever the longer name adds must extend the identity, not redefine it.
    leftover = [t for t in long if not any(t.startswith(s) for s in short)]
    return not any(t in _IDENTITY_CHANGING_TOKENS for t in leftover)


def _stated_time(text: str) -> int | None:
    """Minutes past midnight of the stated start time, or None."""
    match = _CLOCK.search(text or "")
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (1 <= hour <= 12 and minute < 60):
        return None
    hour %= 12
    if match.group(3).lower() == "p":
        hour += 12
    return hour * 60 + minute


#: A four-digit year stated alongside the date.
_EXPLICIT_YEAR = re.compile(r"\b(20\d{2})\b")

#: How far the stated contest date may sit from the venue's own timing fields.
#: Kalshi holds a market open for days past the game to cover its rescheduling
#: window, so this is generous; it exists to catch a date off by a *year*, not to
#: second-guess a venue by a day.
MAX_SCHEDULE_DRIFT_DAYS = 30


def _stated_schedule(
    text: str, reference: datetime | None = None
) -> tuple[date | None, int | None]:
    """The date and clock time the venue states the contest begins at.

    Deliberately not converted to UTC. Both venues quote a venue-local time with an
    explicit US Eastern marker, and a 10:15 PM EDT first pitch on 11 September is
    02:15 UTC on the 12th - so normalising one side and not the other would make two
    descriptions of the same game disagree by a day.

    Polymarket states no year in 97 of 99 real fixtures, and the shared date
    extractor falls back to the *current* year - which would make this field a
    function of the wall clock rather than of the stored row, so the same unchanged
    market names a different contest next January. When the venue states no year, it
    is resolved against the market's own timing instead.
    """
    match = _SCHEDULED.search(text or "")
    if match is None:
        return None, None
    tail = text[match.end():match.end() + 60]
    found = extract_dates(tail)
    if not found:
        return None, _stated_time(tail)

    anchor = reference.date() if reference is not None else None
    if not _EXPLICIT_YEAR.search(tail) and anchor is not None:
        found = [_closest_year(d, anchor) for d in found]
    # ``extract_dates`` returns its hits sorted, so the first is the EARLIEST rather
    # than the one the venue stated first. No real fixture tail currently carries two
    # dates (0 of 401 measured), but picking the one nearest the market's own timing
    # costs nothing and removes the dependence on that staying true.
    stated = min(found, key=lambda d: abs((d - anchor).days)) if anchor else found[0]
    return stated, _stated_time(tail)


def _closest_year(stated: date, reference: date) -> date:
    """Re-anchor a yearless date onto the year that puts it nearest ``reference``.

    A contest in late December referenced by a market closing in early January
    belongs to the previous year, so the neighbouring years are considered too.
    """
    best = stated
    best_gap: int | None = None
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidate = stated.replace(year=year)
        except ValueError:
            # 29 February in a non-leap year.
            continue
        gap = abs((candidate - reference).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = candidate, gap
    return best


def _league_from_prose(text: str) -> str:
    match = _LEAGUE_IN_PROSE.search(text or "")
    if match is None:
        return ""
    token = re.sub(r"[\s-]", "", match.group(1)).upper()
    return token if token in LEAGUE_SPORTS else ""


def _sport_from_prose(text: str) -> str:
    return next((s for s in _SPORTS if re.search(rf"\b{s}\b", text or "", re.I)), "")


def _reference_time(market: NormalizedMarket) -> datetime | None:
    """The venue's own sense of when this market is about, for anchoring a date."""
    return (
        market.event_occurrence_time
        or market.close_time
        or market.expected_resolution_time
    )


def _publishes_a_strike(market: NormalizedMarket) -> bool:
    """Whether the venue declared a numeric strike on this market.

    A market with a strike is a spread or a total, not a moneyline, whatever its
    prose reads like - and the venue's own structured metadata outranks a regex over
    its prose, the rule ``rules_for`` already applies to thresholds.
    """
    return market.floor_strike is not None or market.cap_strike is not None


def _can_match_kalshi_fixture(corpus: str | None) -> bool:
    """Cheap necessary condition for the fixture pattern, checked before the regex.

    ``_KALSHI_FIXTURE`` chains several lazy bounded quantifiers and only commits at
    "scheduled for", so text that starts to match and then does not - "If the
    Democratic candidate wins the Michigan vs Ohio professional football game and..."
    without the scheduling clause - makes the engine backtrack through every
    combination. Measured at 2.7ms per call on a 400-character partial match against
    0.03ms on text that never enters the pattern, and this runs over every market on
    both venues on every candidate-generation pass.

    Substring tests are linear and rule out the pathological inputs outright.
    """
    if not corpus:
        return False
    lowered = corpus.lower()
    return "wins the" in lowered and "scheduled for" in lowered

def _kalshi_fixture(market: NormalizedMarket) -> Fixture | None:
    corpus = market.settlement_rules_raw or market.description
    if not _can_match_kalshi_fixture(corpus):
        return None
    match = _KALSHI_FIXTURE.search(corpus)
    if match is None:
        return None

    # The only qualifier allowed between "game" and "scheduled for" identifies which
    # half of a doubleheader this is. Anything else ("after five complete innings",
    # "by 2 or more runs") means this is a derivative of the contest, not the contest.
    qualifier = (match.group("qualifier") or "").strip()
    leg = ""
    if qualifier:
        leg_match = _DOUBLEHEADER_LEG.search("game " + qualifier)
        if leg_match is None:
            return None
        leg = leg_match.group("leg")

    ticker_match = _KALSHI_LEAGUE_TICKER.match(market.platform_event_id or "")
    league = ""
    if ticker_match and ticker_match.group(1) in LEAGUE_SPORTS:
        league = ticker_match.group(1)
    league = league or _league_from_prose(corpus)

    scheduled, start_minute = _stated_schedule(corpus, _reference_time(market))
    sport = LEAGUE_SPORTS.get(league) or match.group("sport").lower()
    return Fixture(
        league=league,
        sport=sport,
        competitors=(match.group("a").strip().lower(), match.group("b").strip().lower()),
        scheduled=scheduled,
        start_minute=start_minute,
        doubleheader_leg=leg,
        side=match.group("side").strip().lower(),
    )


def _polymarket_fixture(market: NormalizedMarket) -> Fixture | None:
    match = _POLY_FIXTURE.search(market.title or "")
    if match is None:
        return None

    outcomes = [str(o).strip() for o in (market.outcomes or []) if str(o).strip()]
    if len(outcomes) != 2:
        return None
    if {o.lower() for o in outcomes} & _NON_COMPETITOR_OUTCOMES:
        # "Over"/"Under"/"Yes"/"No" mean the market asks something else about this
        # fixture, even when the title reads as a plain matchup.
        return None

    left, right = match.group("a").strip().lower(), match.group("b").strip().lower()
    # The outcomes must be the competitors named in the title. If they are not, the
    # title is describing a context ("US Open ... : X vs Y") whose outcomes mean
    # something else, and pairing it with a moneyline would be wrong.
    if not (competitors_match(left, outcomes[0]) and competitors_match(right, outcomes[1])):
        return None

    corpus = market.settlement_rules_raw or market.description
    if _SERIES_LANGUAGE.search(corpus or ""):
        # A series winner shares its start date with game one of that series.
        return None

    league = _league_from_prose(f"{market.title} {corpus}")
    scheduled, start_minute = _stated_schedule(corpus, _reference_time(market))
    return Fixture(
        league=league,
        sport=LEAGUE_SPORTS.get(league) or _sport_from_prose(corpus),
        competitors=(left, right),
        scheduled=scheduled,
        start_minute=start_minute,
        doubleheader_leg="",
        # The first outcome is the YES token on a Polymarket binary market.
        side=outcomes[0].lower(),
    )


def extract_fixture(market: NormalizedMarket) -> Fixture | None:
    """The head-to-head contest this market pays out on, if it is one.

    Returns ``None`` for everything that is not a two-way moneyline on a dated
    contest, which is the only proposition shape the two venues state comparably.
    """
    if _publishes_a_strike(market):
        return None
    if market.platform is Platform.KALSHI:
        fixture = _kalshi_fixture(market)
    elif market.platform is Platform.POLYMARKET:
        fixture = _polymarket_fixture(market)
    else:
        return None
    if fixture is None or not fixture.is_usable:
        return None
    # Corroborate the parsed date against the venue's own timing fields. The
    # fixture proposer bypasses the resolution-gap gate the token proposer applies
    # - it has to, because Kalshi holds a market open for days past the game - so
    # without this the contest date has no independent check at all.
    reference = _reference_time(market)
    if reference is not None:
        drift = abs((fixture.scheduled - reference.date()).days)
        if drift > MAX_SCHEDULE_DRIFT_DAYS:
            return None
    return fixture


def _competitions_agree(left: Fixture, right: Fixture) -> bool:
    """Whether the two fixtures belong to the same competition.

    Leagues are compared when both are known. A league against a bare sport is
    resolved through the league's own sport, so Polymarket prose naming "MLB" agrees
    with Kalshi prose naming "baseball". Neither side may be silent: with Kalshi
    naming competitors by city alone, an unchecked competition let a Seattle/Toronto
    baseball game certify as a Seattle/Toronto basketball game.
    """
    if left.league and right.league:
        return left.league == right.league
    left_sport = left.sport or LEAGUE_SPORTS.get(left.league, "")
    right_sport = right.sport or LEAGUE_SPORTS.get(right.league, "")
    if not left_sport or not right_sport:
        return False
    return left_sport == right_sport


def fixtures_agree(left: Fixture, right: Fixture) -> tuple[bool, bool]:
    """Whether two fixtures are the same contest, and whether polarity is inverted.

    Returns ``(agree, polarity_inverted)``. Polarity is inverted when the two markets
    pay out on *opposite* competitors, which is precisely the orientation a
    cross-venue hedge needs: long Kalshi "San Diego wins" against long Polymarket
    "San Francisco Giants".
    """
    if left.scheduled is None or right.scheduled is None:
        return False, False
    if left.scheduled != right.scheduled:
        return False, False
    if not _competitions_agree(left, right):
        return False, False

    # Doubleheaders: two contests between the same teams on one date, and what a
    # postponement creates. When either venue identifies which half it means, the
    # other must identify the same half.
    if left.doubleheader_leg or right.doubleheader_leg:
        if left.doubleheader_leg != right.doubleheader_leg:
            return False, False

    sport = left.sport or right.sport
    if sport in _DOUBLEHEADER_SPORTS:
        # For these, a stated first pitch is the only thing separating the two halves,
        # so a missing one refuses the fixture rather than waiving the check.
        if left.start_minute is None or right.start_minute is None:
            return False, False
    if left.start_minute is not None and right.start_minute is not None:
        if abs(left.start_minute - right.start_minute) >= DOUBLEHEADER_SEPARATION_MINUTES:
            return False, False

    direct = competitors_match(left.competitors[0], right.competitors[0]) and (
        competitors_match(left.competitors[1], right.competitors[1])
    )
    swapped = competitors_match(left.competitors[0], right.competitors[1]) and (
        competitors_match(left.competitors[1], right.competitors[0])
    )
    if not (direct or swapped):
        return False, False

    # Which competitor does each market pay out on? A market whose side matches
    # neither competitor, or both, is not usable: the hedge orientation is unknown.
    left_first = competitors_match(left.side, left.competitors[0])
    left_second = competitors_match(left.side, left.competitors[1])
    right_first = competitors_match(right.side, right.competitors[0])
    right_second = competitors_match(right.side, right.competitors[1])
    if left_first == left_second or right_first == right_second:
        return False, False

    # Normalise both sides onto *left*'s competitor ordering before comparing.
    right_on_left_first = right_first if direct else right_second
    same_side = left_first == right_on_left_first
    return True, not same_side
