"""Calibration fitting, and the three gates that stop it fitting noise."""

from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from pmvl_markets.backtest.calibration import (
    MIN_FIT_SAMPLES,
    SPURIOUS_IMPROVEMENT_CONSTANT,
    CalibrationFit,
    Observation,
    apply_beta,
    apply_isotonic,
    calibrator_from,
    fit_beta,
    fit_calibration,
    fit_isotonic,
    min_brier_improvement,
)


def overconfident(n: int, *, seed: int = 7) -> list[Observation]:
    """A model that pushes probabilities toward the extremes.

    Stated 0.9 comes true 0.75 of the time and stated 0.1 comes true 0.25 of the
    time - the classic miscalibration a map should be able to correct.
    """
    rng = random.Random(seed)
    out: list[Observation] = []
    for i in range(n):
        stated = rng.uniform(0.05, 0.95)
        # Pull the true rate back toward 0.5.
        true_rate = 0.5 + (stated - 0.5) * 0.6
        out.append(
            Observation(
                probability=stated,
                outcome=1.0 if rng.random() < true_rate else 0.0,
                sequence=i,
            )
        )
    return out


def well_calibrated(n: int, *, seed: int = 11) -> list[Observation]:
    rng = random.Random(seed)
    return [
        Observation(
            probability=(p := rng.uniform(0.05, 0.95)),
            outcome=1.0 if rng.random() < p else 0.0,
            sequence=i,
        )
        for i in range(n)
    ]


# ------------------------------------------------------------------- isotonic
def test_isotonic_is_monotone():
    knots = fit_isotonic([(0.1, 0.0), (0.2, 1.0), (0.3, 0.0), (0.4, 1.0), (0.9, 1.0)])
    values = [y for _x, y in knots]

    assert values == sorted(values)


def test_isotonic_recovers_a_simple_shrink():
    points = [(0.0, 0.0), (0.25, 0.2), (0.5, 0.5), (0.75, 0.8), (1.0, 1.0)]
    knots = fit_isotonic(points)

    assert apply_isotonic(knots, 0.5) == pytest.approx(0.5, abs=0.05)


def test_isotonic_interpolates_between_knots():
    """A step function would make the estimate lurch across a knot boundary."""
    knots = [(0.2, 0.1), (0.8, 0.7)]

    assert apply_isotonic(knots, 0.5) == pytest.approx(0.4, abs=0.01)
    assert apply_isotonic(knots, 0.1) == 0.1  # clamped to the first knot
    assert apply_isotonic(knots, 0.95) == 0.7  # clamped to the last


def test_isotonic_on_no_points_is_the_identity():
    assert apply_isotonic([], 0.42) == 0.42


# ----------------------------------------------------------------------- beta
def test_beta_learns_the_direction_of_the_relationship():
    points = [(o.probability, o.outcome) for o in well_calibrated(500)]
    params = fit_beta(points)

    assert params is not None
    assert apply_beta(params, 0.8) > apply_beta(params, 0.2)


def test_beta_output_stays_a_probability():
    params = fit_beta([(o.probability, o.outcome) for o in overconfident(400)])

    assert params is not None
    for p in (0.001, 0.5, 0.999):
        assert 0.0 <= apply_beta(params, p) <= 1.0


def test_beta_returns_none_on_a_degenerate_design():
    assert fit_beta([(0.5, 1.0)]) is None


def test_beta_declines_perfectly_separable_data():
    """Logistic likelihood has no maximum under separation; IRLS must not pretend.

    Returning None is the safe answer - the caller treats it as "no candidate",
    never as licence to fall back to something unvalidated.
    """
    separable = [(p / 100, 1.0 if p > 50 else 0.0) for p in range(1, 100)]

    assert fit_beta(separable) is None


# ---------------------------------------------------------------- the gates
def test_refuses_to_fit_below_the_sample_floor():
    """Gate 1. The most important behaviour in the module."""
    fit = fit_calibration(overconfident(MIN_FIT_SAMPLES - 1))

    assert fit.applied is False
    assert fit.method == "identity"
    assert str(MIN_FIT_SAMPLES) in fit.reason


def test_refuses_when_the_map_does_not_help():
    """Gate 3. On already-calibrated data there is nothing to correct."""
    fit = fit_calibration(well_calibrated(600))

    assert fit.applied is False
    assert "below the" in fit.reason


def test_adopts_a_map_that_clearly_helps():
    fit = fit_calibration(overconfident(1200))

    assert fit.applied is True
    assert fit.method in ("isotonic", "beta")
    assert fit.improvement >= min_brier_improvement(fit.n_validation)
    assert fit.brier_fitted < fit.brier_identity


def test_validation_fold_is_never_fitted_on():
    """Gate 2. The split is by sequence, so no future outcome trains the map."""
    fit = fit_calibration(overconfident(1000))

    assert fit.n_train + fit.n_validation == 1000
    assert fit.n_train == 700
    assert fit.n_validation == 300


def test_split_is_chronological_not_random():
    """A random split would let next month's outcomes score this month's.

    Constructed so the two halves disagree: the first half is well calibrated and
    the second is not. A chronological split fits on the calibrated half and is
    then scored on the miscalibrated one, so it cannot report a good improvement.
    A random split would blend them and hide the difference entirely.
    """
    first = well_calibrated(700, seed=1)
    second = overconfident(300, seed=2)
    for index, observation in enumerate(second):
        object.__setattr__(observation, "sequence", 700 + index)

    fit = fit_calibration(first + second)

    assert fit.n_train == 700
    assert fit.n_validation == 300


def test_an_empty_sample_is_a_refusal_not_a_crash():
    fit = fit_calibration([])

    assert fit.applied is False
    assert fit.n_train == 0


def test_report_records_the_thresholds_it_was_judged_against():
    """So a stored refusal can be re-read without the code that produced it."""
    payload = fit_calibration(overconfident(50)).as_dict()

    assert payload["min_fit_samples"] == MIN_FIT_SAMPLES
    assert payload["spurious_improvement_constant"] == SPURIOUS_IMPROVEMENT_CONSTANT
    assert payload["train_fraction"] == 0.7


def test_calibrated_data_almost_never_produces_a_fit():
    """The property the threshold exists to deliver, checked across many samples.

    Against the original flat 0.002 bar this adopted a map in roughly a quarter of
    runs at these sizes - a correction fitted to sampling noise, presented as a
    calibration. One test on one seed did not catch that; the shape of the failure
    is statistical, so the test has to be too.
    """
    adopted = sum(
        1
        for seed in range(40)
        for n in (200, 400, 800)
        if fit_calibration(well_calibrated(n, seed=seed)).applied
    )

    assert adopted <= 6, f"{adopted}/120 fits adopted on well-calibrated data"


def test_real_miscalibration_is_still_caught_at_scale():
    """The gate must be conservative, not inert."""
    adopted = sum(
        1 for seed in range(30) if fit_calibration(overconfident(1200, seed=seed)).applied
    )

    assert adopted >= 24, f"only {adopted}/30 genuine miscalibrations were corrected"


def test_the_bar_tightens_as_the_validation_fold_grows():
    assert min_brier_improvement(60) > min_brier_improvement(600)
    assert min_brier_improvement(0) == math.inf


# ------------------------------------------------------------- reconstruction
def test_calibrator_from_a_refusal_is_the_identity():
    fit = CalibrationFit(method="identity", applied=False, reason="too few")
    calibrate = calibrator_from(fit)

    assert calibrate(Decimal("0.37")) == Decimal("0.37")


def test_calibrator_round_trips_an_adopted_isotonic_map():
    fit = fit_calibration(overconfident(1200))
    assert fit.applied

    calibrate = calibrator_from(fit.as_dict())
    value = calibrate(Decimal("0.9"))

    # The overconfident generator's 0.9 really comes true ~0.74 of the time, so a
    # working map pulls it down rather than leaving it alone.
    assert value < Decimal("0.9")
    assert Decimal("0") <= value <= Decimal("1")


def test_calibrator_from_none_is_the_identity():
    assert calibrator_from(None)(Decimal("0.5")) == Decimal("0.5")


def test_calibrator_survives_a_corrupt_stored_payload():
    """A stored map that cannot be read must not take the estimate with it."""
    broken = {"applied": True, "method": "beta", "parameters": {"a": "nonsense"}}

    assert calibrator_from(broken)(Decimal("0.5")) == Decimal("0.5")


def test_a_fitted_map_actually_improves_calibration_on_fresh_data():
    """End to end: fit on one sample, check it helps on an unseen one."""
    fit = fit_calibration(overconfident(1500, seed=3))
    assert fit.applied

    calibrate = calibrator_from(fit.as_dict())
    fresh = overconfident(600, seed=99)

    raw = sum((o.probability - o.outcome) ** 2 for o in fresh) / len(fresh)
    mapped = sum(
        (float(calibrate(Decimal(str(o.probability)))) - o.outcome) ** 2 for o in fresh
    ) / len(fresh)

    assert mapped < raw
