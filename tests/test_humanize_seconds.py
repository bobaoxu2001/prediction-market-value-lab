"""The risk-flag age formatter.

Written after a live risk flag reached the page reading "oldest quote is
179581s old" - correct, and useless to a reader deciding whether to trust the
quote.
"""

import pytest

from pmvl_shared.timeutil import humanize_seconds


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (5, "5s"),
        (89, "89s"),
        # Crossing into minutes at 90s keeps the displayed number small rather
        # than switching at exactly 60 and printing "1m" for 61 seconds.
        (90, "2m"),
        (600, "10m"),
        (5400, "1.5h"),
        (7200, "2h"),
        # The case that prompted this: just over two days.
        (179581, "2.1d"),
    ],
)
def test_picks_a_unit_that_keeps_the_number_readable(seconds, expected):
    assert humanize_seconds(seconds) == expected


def test_trailing_zero_is_dropped():
    """"2h" not "2.0h" - the decimal implies a precision that is not there."""
    assert humanize_seconds(7200) == "2h"
    assert humanize_seconds(172800) == "2d"


def test_negative_ages_are_reported_as_magnitudes():
    """A clock-skewed observation should not print "-3.0h old"."""
    assert humanize_seconds(-10800) == humanize_seconds(10800)


@pytest.mark.parametrize("seconds", [1e5, 1e6, 1e7, 1e9])
def test_large_ages_never_render_in_seconds(seconds):
    """The specific failure this replaced: a raw six-figure second count."""
    rendered = humanize_seconds(seconds)
    assert not rendered.endswith("s"), rendered
    # Whatever unit is chosen, the number in front of it stays legible.
    assert len(rendered) <= 8, rendered
