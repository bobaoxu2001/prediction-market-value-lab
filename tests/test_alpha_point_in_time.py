"""A decision may only read what was knowable when it was made.

Every leakage bug has the same shape and the same symptom: the strategy looks
better than it was, and nothing in the output says why. The failure is silent by
construction, because using more information than was available never raises.

These tests exercise the four-timestamp model directly. The interesting cases are
the ones where the *convenient* timestamp and the *correct* one differ:

* a forecast published at 14:00 that this system could not read until 14:40;
* a revision published two weeks after the figure it revises;
* a backfilled row written today describing something from last month.

In each, sorting or filtering on the wrong field produces a strictly better
backtest, which is why the wrong field cannot be the one the API makes easy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_markets.alpha.pit import (
    AsOfView,
    LookaheadError,
    SourceObservation,
    decimal_value,
    independent_source_count,
    observation,
)

BASE = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def forecast(
    minutes_available: int,
    value: str,
    *,
    source: str = "nws",
    observed_offset: int = 0,
    event_offset: int = 0,
) -> SourceObservation[Decimal]:
    return observation(
        source_id=source,
        kind="source_estimate",
        value=Decimal(value),
        available_at=at(minutes_available),
        observed_at=at(minutes_available + observed_offset),
        event_time=at(minutes_available + event_offset),
    )


class TestAdmissibility:
    def test_an_observation_available_before_the_decision_is_readable(self) -> None:
        obs = forecast(0, "0.40")
        assert obs.known_by(at(30)) is True

    def test_an_observation_available_after_the_decision_is_not(self) -> None:
        obs = forecast(60, "0.40")
        assert obs.known_by(at(30)) is False

    def test_an_observation_available_at_exactly_the_decision_is_readable(self) -> None:
        """Inclusive, deliberately.

        Exclusive would silently discard the very update a source-lag strategy
        is reacting to - the one whose arrival IS the decision trigger.
        """
        assert forecast(30, "0.40").known_by(at(30)) is True

    def test_an_observation_with_no_availability_is_never_readable(self) -> None:
        """Absent provenance is not evidence of freshness.

        A backfilled row with no ``available_at`` treated as admissible is how a
        migration turns into a look-ahead.
        """
        orphan = SourceObservation(
            source_id="nws", kind="source_estimate", value=Decimal("0.4"),
            available_at=None,
        )
        assert orphan.known_by(at(9999)) is False


class TestAsOfView:
    def test_the_view_does_not_contain_inadmissible_observations(self) -> None:
        """Filtered at construction, not on every read.

        An observation that is merely ignored is one attribute access away from
        being used; one that is not in the object cannot be reached by taking a
        different route through the code.
        """
        view = AsOfView.build(at(30), [forecast(0, "0.40"), forecast(60, "0.55")])
        assert len(view) == 1
        assert view.latest("source_estimate").value == Decimal("0.40")

    def test_latest_returns_the_newest_ADMISSIBLE_observation(self) -> None:
        """Not the newest observation. That distinction is the entire bug class."""
        view = AsOfView.build(
            at(45),
            [forecast(0, "0.30"), forecast(30, "0.40"), forecast(60, "0.90")],
        )
        assert view.latest("source_estimate").value == Decimal("0.40")

    def test_previous_gives_the_value_before_the_most_recent_update(self) -> None:
        """A source-lag factor needs the change, so it needs two points.

        Reaching for "the value before the update" any other way is how a factor
        ends up comparing today's forecast against today's forecast.
        """
        view = AsOfView.build(
            at(45), [forecast(0, "0.30"), forecast(30, "0.40")]
        )
        assert view.previous("source_estimate").value == Decimal("0.30")

    def test_previous_is_none_with_only_one_observation(self) -> None:
        view = AsOfView.build(at(45), [forecast(0, "0.30")])
        assert view.previous("source_estimate") is None

    def test_require_raises_rather_than_returning_a_default(self) -> None:
        view = AsOfView.build(at(45), [])
        with pytest.raises(LookaheadError, match="did not exist yet"):
            view.require("source_estimate")

    def test_assert_admissible_rejects_an_externally_supplied_future_value(self) -> None:
        """The one route by which the future can still get in.

        A caller that obtained an observation outside the view has to hand it
        through this check, so the boundary is guarded rather than trusted.
        """
        view = AsOfView.build(at(30), [])
        with pytest.raises(LookaheadError, match="after the decision"):
            view.assert_admissible(forecast(60, "0.9"))

    def test_of_kind_returns_observations_oldest_first(self) -> None:
        view = AsOfView.build(
            at(90), [forecast(30, "0.40"), forecast(0, "0.30"), forecast(60, "0.50")]
        )
        assert [o.value for o in view.of_kind("source_estimate")] == [
            Decimal("0.30"), Decimal("0.40"), Decimal("0.50")
        ]


class TestRevisions:
    """A revised figure is two observations of one event, not one that changed."""

    def test_a_decision_before_the_revision_sees_the_original(self) -> None:
        first = observation(
            source_id="bls", kind="source_point_value", value=Decimal("0.3"),
            available_at=at(0), event_time=at(-1440),
        )
        revised = observation(
            source_id="bls", kind="source_point_value", value=Decimal("0.4"),
            available_at=at(1440), event_time=at(-1440),
        )
        view = AsOfView.build(at(60), [first, revised])
        assert view.latest("source_point_value").value == Decimal("0.3")

    def test_a_decision_after_the_revision_sees_the_revision(self) -> None:
        first = observation(
            source_id="bls", kind="source_point_value", value=Decimal("0.3"),
            available_at=at(0), event_time=at(-1440),
        )
        revised = observation(
            source_id="bls", kind="source_point_value", value=Decimal("0.4"),
            available_at=at(1440), event_time=at(-1440),
        )
        view = AsOfView.build(at(2000), [first, revised])
        assert view.latest("source_point_value").value == Decimal("0.4")

    def test_revision_handling_needs_no_special_case(self) -> None:
        """It falls out of availability filtering, which is why the field exists.

        A single ``timestamp`` plus a convention would have needed a revision
        branch somewhere, and that branch would have been the thing to get wrong.
        """
        first = observation(
            source_id="bls", kind="source_point_value", value=Decimal("0.3"),
            available_at=at(0), event_time=at(-1440),
        )
        revised = observation(
            source_id="bls", kind="source_point_value", value=Decimal("0.4"),
            available_at=at(1440), event_time=at(-1440),
        )
        # Same event_time on both; only availability separates them.
        assert first.event_time == revised.event_time


class TestLagMeasurement:
    def test_age_is_measured_from_availability(self) -> None:
        obs = forecast(0, "0.40")
        assert obs.age_at(at(90)) == timedelta(minutes=90)

    def test_source_age_is_measured_from_publication(self) -> None:
        """Different question, and the one a freshness factor wants.

        A forecast issued an hour ago that we ingested a minute ago is fresh to
        us and an hour old at the source.
        """
        obs = observation(
            source_id="nws", kind="source_estimate", value=Decimal("0.4"),
            available_at=at(60), observed_at=at(0),
        )
        assert obs.age_at(at(90)) == timedelta(minutes=30)
        assert obs.source_age_at(at(90)) == timedelta(minutes=90)

    def test_ingestion_lag_is_how_long_the_source_led_us(self) -> None:
        obs = observation(
            source_id="nws", kind="source_estimate", value=Decimal("0.4"),
            available_at=at(40), observed_at=at(0),
        )
        assert obs.ingestion_lag() == timedelta(minutes=40)

    def test_age_of_an_inadmissible_observation_is_none(self) -> None:
        """No plausible-looking age from something the caller may not read."""
        assert forecast(60, "0.4").age_at(at(30)) is None


class TestSourceIndependence:
    def test_repeated_pulls_of_one_source_count_once(self) -> None:
        """Ten pulls of one NWS gridpoint are one source.

        Counting them as ten would report agreement that does not exist, and
        agreement is exactly what a dispersion factor is used as evidence of.
        """
        observations = [forecast(m, "0.40") for m in range(0, 100, 10)]
        assert independent_source_count(observations) == 1

    def test_declared_mirrors_collapse_onto_their_upstream(self) -> None:
        observations = [
            forecast(0, "0.40", source="nws"),
            forecast(10, "0.40", source="weather_vendor"),
            forecast(20, "0.45", source="ecmwf"),
        ]
        assert independent_source_count(observations) == 3
        assert (
            independent_source_count(
                observations, mirrors={"weather_vendor": "nws"}
            )
            == 2
        )

    def test_no_mirrors_declared_counts_distinct_ids(self) -> None:
        """The honest default.

        Which feed mirrors which is a judgement somebody has to make; guessing
        it here would be a silent modelling decision inside a counting function.
        """
        observations = [
            forecast(0, "0.40", source="a"),
            forecast(10, "0.41", source="b"),
        ]
        assert independent_source_count(observations) == 2


class TestValueCoercion:
    def test_a_float_is_coerced_through_str(self) -> None:
        """Not Decimal(float), which preserves binary representation error.

        Money lives on a cent lattice here; 0.1 arriving as
        0.1000000000000000055511151231257827 changes verdicts.
        """
        obs = observation(
            source_id="s", kind="k", value=0.1, available_at=at(0)
        )
        assert decimal_value(obs) == Decimal("0.1")

    def test_a_bool_is_not_a_number(self) -> None:
        obs = observation(source_id="s", kind="k", value=True, available_at=at(0))
        assert decimal_value(obs) is None

    def test_unparseable_text_returns_none_rather_than_raising(self) -> None:
        obs = observation(source_id="s", kind="k", value="n/a", available_at=at(0))
        assert decimal_value(obs) is None


class TestConstructorSafety:
    def test_the_timestamps_are_keyword_only(self) -> None:
        """Four same-typed arguments in a row is a transposition waiting to happen.

        A swap of ``observed_at`` and ``available_at`` type-checks perfectly and
        inverts what the backtest is allowed to see.
        """
        with pytest.raises(TypeError):
            observation("nws", "source_estimate", Decimal("0.4"), at(0))  # type: ignore[misc]

    def test_observed_at_defaults_to_available_at(self) -> None:
        """A source with no separate publication time is not zero-lag by accident.

        Defaulting to ``available_at`` makes ``ingestion_lag`` zero, which is
        the truthful reading of "we have no evidence the source led us".
        """
        obs = observation(
            source_id="s", kind="k", value=Decimal("1"), available_at=at(10)
        )
        assert obs.observed_at == at(10)
        assert obs.ingestion_lag() == timedelta(0)
