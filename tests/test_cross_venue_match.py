"""Both of this project's matching failures, pinned so neither can happen a third time.

The rain pairing and the baseball pairing were the same bug: a key that did not
identify the event. These tests are written from the actual data that produced each
one, not from an invented example.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pmvl_markets.arbitrage.cross_venue_match import (
    START_TIME_TOLERANCE,
    MatchStatus,
    prove_same_event,
    significant_words,
)

UTC = timezone.utc
SEP22 = datetime(2026, 9, 23, 1, 40, tzinfo=UTC)   # Sep 22 21:40 ET
SEP23 = SEP22 + timedelta(days=1)

KALSHI_TEXT = ("If Los Angeles A and A's collectively score more 9.5 runs in the "
               "Los Angeles A vs A's professional baseball game originally scheduled "
               "for Sep 22, 2026 at 9:40 PM EDT, then the market resolves to Yes.")
POLY_TEXT = ("Los Angeles Angels vs. Athletics: O/U 9.5 - resolve to Over if the "
             "Los Angeles Angels and Athletics combine to score 10 or more runs")


def _prove(**kw):
    base = dict(
        left_key="K", right_key="P",
        left_start=SEP22, right_start=SEP22,
        left_threshold=Decimal("9.5"), right_threshold=Decimal("9.5"),
        left_text=KALSHI_TEXT, right_text=POLY_TEXT,
        participants_verified=True,
    )
    base.update(kw)
    return prove_same_event(**base)


# ------------------------------------------------------- the bug that happened
def test_the_same_teams_on_the_next_day_are_refused():
    """The exact pairing that reported a 29-cent edge.

    The Angels and the Athletics play a series. Same teams, same line, one day
    apart - and (teams, line) cannot tell those two games apart.
    """
    result = _prove(right_start=SEP23)
    assert result.status is MatchStatus.REFUTED
    assert "different fixture" in result.reason
    assert not result.usable


def test_a_doubleheader_on_the_same_date_is_refused_too():
    """A date is not enough either; two games, same teams, same day, hours apart."""
    result = _prove(right_start=SEP22 + timedelta(hours=4))
    assert result.status is MatchStatus.REFUTED


def test_a_few_minutes_of_disagreement_is_still_one_game():
    """Venues round start times differently; they do not disagree by an hour."""
    result = _prove(right_start=SEP22 + timedelta(minutes=5))
    assert result.status is MatchStatus.PROVEN


def test_the_tolerance_boundary_is_exclusive():
    assert _prove(right_start=SEP22 + START_TIME_TOLERANCE).status is MatchStatus.PROVEN
    assert _prove(
        right_start=SEP22 + START_TIME_TOLERANCE + timedelta(seconds=1)
    ).status is MatchStatus.REFUTED


def test_a_missing_start_time_is_insufficient_not_refuted():
    """A data gap and a mismatch call for different work, so they are different answers."""
    result = _prove(right_start=None)
    assert result.status is MatchStatus.INSUFFICIENT_EVIDENCE


# ----------------------------------------------------- the real pair still works
def test_the_genuine_pair_proves_once_participants_are_verified():
    result = _prove()
    assert result.status is MatchStatus.PROVEN
    assert "participants verified by the caller" in result.reason


def test_the_venues_do_not_even_use_the_same_team_names():
    """Why word overlap cannot carry this. Kalshi says "A's", Polymarket "Athletics"."""
    shared = significant_words(KALSHI_TEXT) & significant_words(POLY_TEXT)
    assert "angels" not in shared
    assert "athletics" not in shared
    assert shared <= {"los", "angeles", "score", "collectively", "resolves", "resolve",
                      "market", "yes", "originally", "scheduled", "edt", "sep"}


def test_unverified_participants_are_insufficient_however_well_everything_else_agrees():
    """Fail closed. Same minute, same line, and still not proven."""
    result = _prove(participants_verified=False)
    assert result.status is MatchStatus.INSUFFICIENT_EVIDENCE
    assert "corroborate and do not prove" in result.reason


def test_two_los_angeles_teams_would_have_fooled_a_word_overlap_rule():
    """The failure one level in, which is why overlap is not allowed to be the proof.

    An Angels market and a Dodgers market share exactly the words an Angels market
    shares with itself across venues: los, angeles.
    """
    angels = significant_words("Los Angeles Angels vs. Athletics: O/U 9.5")
    dodgers = significant_words("Los Angeles Dodgers vs. Padres: O/U 9.5")
    assert angels & dodgers == {"los", "angeles"}


def test_different_lines_on_the_same_game_are_refused():
    result = _prove(right_threshold=Decimal("8.5"))
    assert result.status is MatchStatus.REFUTED
    assert "different claims" in result.reason


# --------------------------------------------- participants come from the venues
def test_a_shared_line_is_not_evidence_about_which_teams_are_playing():
    """Numbers are stripped before the participant check.

    Conflating "both say 9.5" with "both mean the same game" is precisely what
    produced the fake edge, so the number cannot be allowed to count as a name.
    """
    assert "9" not in significant_words("Over 9.5 runs")
    #: and the surrounding words are boilerplate, so the phrase names nobody at all
    assert significant_words("Over 9.5 runs") == set()


def test_an_unverified_pairing_reports_the_words_it_did_share():
    """The corroboration is still printed, so a reviewer can see how thin it was."""
    result = _prove(
        right_text="Miami Marlins vs. Chicago Cubs: O/U 9.5",
        participants_verified=False,
    )
    assert result.status is MatchStatus.INSUFFICIENT_EVIDENCE
    assert any(c.field == "shared_words" for c in result.checks)


def test_boilerplate_shares_nothing_because_it_names_no_one():
    """"professional baseball game total runs scored" is in every title."""
    assert significant_words("professional baseball game total runs scored") == set()


def test_every_check_is_recorded_even_on_a_refusal():
    """A refusal that does not say which field failed cannot be audited."""
    result = _prove(right_start=SEP23)
    assert result.checks
    assert any(c.field == "start_time" for c in result.checks)


def test_the_noise_list_is_ascii():
    """A stray character from a heredoc made it into this list once and did nothing
    visible - it simply sat in the set failing to match any word forever. A silent
    no-op in a filter is worse than a loud one."""
    from pmvl_markets.arbitrage.cross_venue_match import _NOISE

    assert all(w.isascii() and w.isalpha() for w in _NOISE), sorted(
        w for w in _NOISE if not (w.isascii() and w.isalpha())
    )
