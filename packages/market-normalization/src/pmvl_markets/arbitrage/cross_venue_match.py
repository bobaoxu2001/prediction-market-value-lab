"""Pairing two venues' markets, with the key required to identify the event.

This module exists because the same mistake has now been made twice in this project,
in two different costumes.

The first: twenty-seven "will it rain in <city>" markets all carry ``greater`` and a
strike of ``0``, so ``[X > 0] implies [X > 0]`` held for every pair of cities and the
scan priced rain in Dallas as settling rain in Milwaukee.

The second: a Kalshi baseball total was matched to a Polymarket one on (teams, line)
with no date. Those teams play a series - consecutive days, same line - so it paired
September 23's game against September 22's and reported a 29-cent edge.

Both are the same error. The key was not sufficient to identify the event, and nothing
in the code required it to be. So the requirement is explicit here: a pair is refused
unless the two markets agree on a start time, on a threshold, and on their participants
- and participants are checked against **the venues' own text**, never against a
hand-written abbreviation table, because a table that says ``sox -> CWS`` will happily
match the Red Sox and no test of mine would notice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

__all__ = [
    "MatchStatus",
    "MatchRefusal",
    "CrossVenueCandidate",
    "prove_same_event",
    "significant_words",
]

#: Words that appear in nearly every sports market title on either venue and so carry
#: no evidence about *which* game is meant.
_NOISE = frozenset(
    """the a an of in on at vs versus and or to for by with will be is are game match
    over under total points runs scored combined more less than mlb nba nfl nhl
    baseball basketball football hockey soccer tennis
    professional league first second half inning innings quarter period""".split()
)

#: How far apart two venues' stated start times may be and still be one game. Venues
#: round differently and occasionally disagree by a few minutes on a delayed start;
#: they do not disagree by an hour, and a doubleheader's games are hours apart.
START_TIME_TOLERANCE = timedelta(minutes=30)


class MatchStatus(StrEnum):
    PROVEN = "proven"
    REFUTED = "refuted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class MatchRefusal:
    """Why a candidate pair was not accepted. Kept, never discarded."""

    field: str
    detail: str


@dataclass
class CrossVenueCandidate:
    """A proposed pairing and the evidence for or against it."""

    left_key: str
    right_key: str
    status: MatchStatus
    reason: str
    checks: list[MatchRefusal] = field(default_factory=list)
    shared_participants: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status is MatchStatus.PROVEN


def significant_words(text: str) -> set[str]:
    """Lowercased words that could name a participant, with numbers removed.

    Numbers go because the line is checked separately and a shared ``9.5`` is not
    evidence about which teams are playing - that conflation is what produced the
    twenty-nine cent edge.
    """
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _NOISE}


def prove_same_event(
    *,
    left_key: str,
    right_key: str,
    left_start: datetime | None,
    right_start: datetime | None,
    left_threshold: Decimal | None,
    right_threshold: Decimal | None,
    left_text: str,
    right_text: str,
    participants_verified: bool = False,
) -> CrossVenueCandidate:
    """Accept a pairing only with a start time, a threshold, and verified participants.

    ``participants_verified`` is a parameter the caller must justify, and it defaults
    to False, because **text overlap cannot establish team identity between these two
    venues**. Kalshi writes "Los Angeles A" and "A's"; Polymarket writes "Los Angeles
    Angels" and "Athletics". The only words they share are *los* and *angeles* - which
    the Dodgers share too. A word-overlap test would have rated a Dodgers market and an
    Angels market as the same participants, which is the identical failure to the one
    this module was written to stop, one level further in.

    So the overlap is computed and reported as corroboration, and it is not allowed to
    be the proof. Something that actually resolves the names - a venue-published team
    id, or an operator who checked - has to say so.

    Every check that fails is recorded rather than short-circuited away, because "we
    looked and it did not match" and "we never looked" are different findings and only
    one of them says anything about the market.
    """
    checks: list[MatchRefusal] = []

    def refuse(status: MatchStatus, reason: str) -> CrossVenueCandidate:
        return CrossVenueCandidate(left_key, right_key, status, reason, checks)

    if left_start is None or right_start is None:
        checks.append(MatchRefusal("start_time", "one side has no scheduled start"))
        return refuse(
            MatchStatus.INSUFFICIENT_EVIDENCE,
            "a pair with no start time on both sides cannot be shown to be one game; "
            "teams play each other on consecutive days and in doubleheaders",
        )

    skew = abs(left_start - right_start)
    if skew > START_TIME_TOLERANCE:
        checks.append(
            MatchRefusal("start_time", f"{left_start.isoformat()} vs {right_start.isoformat()}")
        )
        return refuse(
            MatchStatus.REFUTED,
            f"scheduled starts differ by {skew}, which is more than one game's worth "
            "of rounding - this is a different fixture",
        )
    checks.append(MatchRefusal("start_time", f"within {skew}"))

    if left_threshold is None or right_threshold is None:
        checks.append(MatchRefusal("threshold", "one side has no threshold"))
        return refuse(
            MatchStatus.INSUFFICIENT_EVIDENCE,
            "no threshold on one side, so the two contracts may be asking different "
            "questions about the same game",
        )
    if left_threshold != right_threshold:
        checks.append(MatchRefusal("threshold", f"{left_threshold} vs {right_threshold}"))
        return refuse(
            MatchStatus.REFUTED, "the thresholds differ, so these are different claims"
        )
    checks.append(MatchRefusal("threshold", str(left_threshold)))

    shared = significant_words(left_text) & significant_words(right_text)
    checks.append(
        MatchRefusal("shared_words", f"corroborating only: {sorted(shared)}")
    )

    if not participants_verified:
        return refuse(
            MatchStatus.INSUFFICIENT_EVIDENCE,
            "start time and threshold agree, but nothing has established that the two "
            "contracts name the same participants - the venues use different names for "
            "the same team, so their shared words "
            f"({sorted(shared)}) corroborate and do not prove it",
        )

    return CrossVenueCandidate(
        left_key,
        right_key,
        MatchStatus.PROVEN,
        f"same start time (within {skew}), same threshold {left_threshold}, and "
        "participants verified by the caller",
        checks,
        tuple(sorted(shared)),
    )
