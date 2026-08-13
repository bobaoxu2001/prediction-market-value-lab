"""Fitting a calibration map, and refusing to when the data cannot support one.

Calibration *measurement* has been live since the first release: the reliability
curve and Brier-versus-market on ``/backtest`` say whether stated probabilities
match realised frequencies. Fitting - learning a correction and applying it - was
left out, because it needs a walk-forward set of real settled recommendations and
there was none.

This module is that fitting step. The important part of it is the refusal.

## Why refusing is most of the job

A calibration map fitted on a handful of points does not fix miscalibration; it
memorises noise and then applies that noise to every future estimate with the
authority of a "calibrated" label. Isotonic regression is especially good at this
- with 30 points it will happily produce a step function that reproduces the
training set exactly and generalises to nothing.

So three gates stand in front of every fit:

1. **Sample size.** Below :data:`MIN_FIT_SAMPLES` in the training fold, nothing is
   fitted and the identity map is returned with a reason.
2. **Walk-forward validation.** The map is fitted on the earlier portion and
   scored on the later one, never on the data it was fitted to.
3. **It has to actually help, by more than noise would.** A fitted map is adopted
   only if it improves Brier on the held-out fold by more than
   :func:`min_brier_improvement`, a bar that tightens as the fold grows. Identity
   is the default and the map has to beat it.

A run that fits nothing is a successful run. ``CalibrationFit.applied`` is False
and ``reason`` says why, and that is a more useful artefact than a curve nobody
should trust.

## Why pure Python

Isotonic via pool-adjacent-violators is about twenty lines and beta calibration is
an IRLS loop. The quant stack lives in ``requirements-dev.txt`` and is deliberately
absent from what the API installs; a fitted map is data the API *reads*, so keeping
the fitting dependency-free avoids the question of which side of that line this
lands on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Sequence

from pmvl_shared.logging_setup import get_logger

log = get_logger(__name__)

#: Minimum points in the *training* fold before any fit is attempted.
#:
#: 150 rather than a rounder, smaller number because the thing being estimated is a
#: whole function, not a parameter. At 150 with a 70/30 split there are ~45
#: validation points, which is enough to reject a map that is clearly worse and not
#: enough to certify a subtle improvement - which is why the improvement threshold
#: below is not merely "greater than zero".
MIN_FIT_SAMPLES = 150

#: Numerator of the adoption threshold, which scales as ``c / sqrt(n_validation)``.
#:
#: This started as a flat 0.002 and that was wrong, in the direction that matters.
#: Feeding :func:`fit_calibration` *genuinely well-calibrated* random data - where
#: the correct answer is always "fit nothing" - and reading the distribution of the
#: improvement it reports:
#:
#: ====== ============= ============= =============
#: n      n_validation  p95 spurious  max spurious
#: ====== ============= ============= =============
#: 200    60            +0.0073       +0.0099
#: 400    120           +0.0061       +0.0125
#: 600    180           +0.0022       +0.0043
#: 1000   300           +0.0018       +0.0048
#: 2000   600           +0.0011       +0.0023
#: ====== ============= ============= =============
#:
#: Against a flat 0.002, roughly a quarter of runs on calibrated data adopted a map
#: at n=200-400. The map was fitting sampling noise and the gate was waving it
#: through, which is the exact failure this module is supposed to prevent.
#:
#: The spurious improvement falls off like one over the root of the validation
#: size, so the threshold does too. 0.08 sits above every maximum observed above,
#: which is deliberately conservative: adopting a map that does nothing is a
#: silent, permanent distortion of every future estimate, while declining a real
#: improvement costs one more month of waiting.
#:
#: Re-measured over 60 runs per cell after the change, which is what the trade
#: actually buys:
#:
#: ====== ================== ================== ==================
#: n      adopted when       adopted when       adopted when badly
#:        well calibrated    miscalibrated      miscalibrated
#: ====== ================== ================== ==================
#: 200    0/60               27/60              -
#: 400    2/60               30/60              57/60
#: 600    0/60               40/60              -
#: 1000   1/60               57/60              59/60
#: 2000   0/60               56/60              -
#: ====== ================== ================== ==================
#:
#: Mild miscalibration goes uncorrected until the sample is large, and that is the
#: intended shape: the cost of missing it is a slightly wide interval, and the cost
#: of the alternative is a fabricated correction applied to live money decisions.
SPURIOUS_IMPROVEMENT_CONSTANT = 0.08


def min_brier_improvement(n_validation: int) -> float:
    """Held-out improvement a map must beat, given the validation fold's size."""
    if n_validation <= 0:
        return float("inf")
    return SPURIOUS_IMPROVEMENT_CONSTANT / math.sqrt(n_validation)


#: Fraction of the (time-ordered) sample used for fitting.
TRAIN_FRACTION = 0.7

_EPS = 1e-6


@dataclass(frozen=True)
class Observation:
    """One settled prediction: what was claimed, what happened, and when."""

    probability: float
    outcome: float
    #: Ordering key for the walk-forward split. Ties keep their input order.
    sequence: int


@dataclass
class CalibrationFit:
    """The outcome of a fitting attempt, adopted or not."""

    method: str
    applied: bool
    reason: str
    parameters: dict[str, Any] = field(default_factory=dict)
    n_train: int = 0
    n_validation: int = 0
    brier_identity: float | None = None
    brier_fitted: float | None = None
    improvement: float | None = None
    #: The bar this run's improvement had to clear, which depends on fold size.
    threshold: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "applied": self.applied,
            "reason": self.reason,
            "parameters": self.parameters,
            "n_train": self.n_train,
            "n_validation": self.n_validation,
            "brier_identity": _round(self.brier_identity),
            "brier_fitted": _round(self.brier_fitted),
            "brier_improvement": _round(self.improvement),
            "min_fit_samples": MIN_FIT_SAMPLES,
            "min_brier_improvement": _round(self.threshold),
            "spurious_improvement_constant": SPURIOUS_IMPROVEMENT_CONSTANT,
            "train_fraction": TRAIN_FRACTION,
        }


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _clamp(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, p))


# --------------------------------------------------------------------- isotonic
def fit_isotonic(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Pool adjacent violators, returning ``(probability, calibrated)`` knots.

    Isotonic regression finds the monotone step function minimising squared error.
    Monotonicity is the assumption that matters: a calibration map is allowed to
    say "when this model says 0.7 the truth is nearer 0.6", and is not allowed to
    say "0.7 means less than 0.6 does", which would be fitting noise about
    particular buckets rather than a systematic bias.
    """
    ordered = sorted(points, key=lambda pair: pair[0])
    if not ordered:
        return []

    # Each block holds (sum of outcomes, count, right-hand x of the block).
    blocks: list[list[float]] = []
    for x, y in ordered:
        blocks.append([y, 1.0, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
            merged_y = blocks[-2][0] + blocks[-1][0]
            merged_n = blocks[-2][1] + blocks[-1][1]
            right = blocks[-1][2]
            blocks.pop()
            blocks.pop()
            blocks.append([merged_y, merged_n, right])

    return [(block[2], block[0] / block[1]) for block in blocks]


def apply_isotonic(knots: Sequence[tuple[float, float]], probability: float) -> float:
    """Piecewise-constant lookup with linear interpolation between knots.

    Interpolating rather than stepping because a step function turns a one-basis-
    point move in the input into a jump in the output, and the ranking layer is
    downstream of this - an estimate should not lurch because it crossed a knot
    that exists only because of where the training points happened to fall.
    """
    if not knots:
        return probability
    if probability <= knots[0][0]:
        return knots[0][1]
    if probability >= knots[-1][0]:
        return knots[-1][1]

    for index in range(1, len(knots)):
        left_x, left_y = knots[index - 1]
        right_x, right_y = knots[index]
        if probability <= right_x:
            if right_x == left_x:
                return right_y
            weight = (probability - left_x) / (right_x - left_x)
            return left_y + weight * (right_y - left_y)
    return knots[-1][1]


# ------------------------------------------------------------------------ beta
def fit_beta(points: Sequence[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Beta calibration: logistic regression on ``[ln p, -ln(1-p)]``.

    Kull, Silva Filho and Flach's family. Two shape parameters plus an intercept,
    so unlike Platt scaling it can correct a map that is over-confident at one end
    and under-confident at the other, and unlike isotonic it cannot carve itself
    into the training set - three parameters is a hard ceiling on how much noise
    it can absorb.

    Fitted by IRLS. Returns ``None`` if it fails to converge or the design is
    degenerate, which the caller treats as "no fit", never as an excuse to fall
    back to something unvalidated.
    """
    rows = [
        (math.log(_clamp(p)), -math.log(1.0 - _clamp(p)), y) for p, y in points
    ]
    if len(rows) < 3:
        return None

    beta = [0.0, 0.0, 0.0]  # a, b, intercept
    for _iteration in range(50):
        # Accumulate the normal equations for one Newton step.
        gradient = [0.0, 0.0, 0.0]
        hessian = [[0.0] * 3 for _ in range(3)]
        for x1, x2, y in rows:
            features = (x1, x2, 1.0)
            z = beta[0] * x1 + beta[1] * x2 + beta[2]
            mu = 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, z))))
            weight = max(mu * (1.0 - mu), 1e-9)
            residual = y - mu
            for i in range(3):
                gradient[i] += features[i] * residual
                for j in range(3):
                    hessian[i][j] += weight * features[i] * features[j]

        step = _solve3(hessian, gradient)
        if step is None:
            return None
        beta = [beta[i] + step[i] for i in range(3)]
        if max(abs(s) for s in step) < 1e-8:
            break
    else:
        return None

    if any(not math.isfinite(value) for value in beta):
        return None
    return beta[0], beta[1], beta[2]


def apply_beta(parameters: tuple[float, float, float], probability: float) -> float:
    a, b, c = parameters
    p = _clamp(probability)
    z = a * math.log(p) + b * (-math.log(1.0 - p)) + c
    return 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, z))))


def _solve3(
    matrix: list[list[float]], vector: list[float]
) -> list[float] | None:
    """Gaussian elimination with partial pivoting on a 3x3 system."""
    augmented = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for column in range(3):
        pivot_row = max(range(column, 3), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot_row][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot_row] = (
            augmented[pivot_row],
            augmented[column],
        )
        for row in range(column + 1, 3):
            factor = augmented[row][column] / augmented[column][column]
            for k in range(column, 4):
                augmented[row][k] -= factor * augmented[column][k]

    solution = [0.0, 0.0, 0.0]
    for row in range(2, -1, -1):
        total = augmented[row][3] - sum(
            augmented[row][k] * solution[k] for k in range(row + 1, 3)
        )
        solution[row] = total / augmented[row][row]
    return solution


# --------------------------------------------------------------------- fitting
def _brier(pairs: Sequence[tuple[float, float]], mapper: Callable[[float], float]) -> float:
    return sum((mapper(p) - y) ** 2 for p, y in pairs) / len(pairs)


def fit_calibration(observations: Sequence[Observation]) -> CalibrationFit:
    """Fit isotonic and beta walk-forward, adopt the better one, or neither.

    The split is by ``sequence``, not random: a random split lets a map trained on
    next month's outcomes score this month's, which is the whole failure the
    snapshot backtest was designed to make impossible and would be silly to
    reintroduce here.
    """
    ordered = sorted(observations, key=lambda o: o.sequence)
    total = len(ordered)
    if total < MIN_FIT_SAMPLES:
        return CalibrationFit(
            method="identity",
            applied=False,
            reason=(
                f"{total} settled observations; {MIN_FIT_SAMPLES} needed before a "
                "calibration map can be fitted without memorising noise"
            ),
            n_train=total,
        )

    split = int(total * TRAIN_FRACTION)
    train = [(_clamp(o.probability), o.outcome) for o in ordered[:split]]
    validation = [(_clamp(o.probability), o.outcome) for o in ordered[split:]]
    if not validation:
        return CalibrationFit(
            method="identity",
            applied=False,
            reason="no validation fold after the walk-forward split",
            n_train=len(train),
        )

    baseline = _brier(validation, lambda p: p)
    candidates: list[tuple[str, dict[str, Any], Callable[[float], float]]] = []

    knots = fit_isotonic(train)
    if knots:
        candidates.append(
            (
                "isotonic",
                {"knots": [[round(x, 6), round(y, 6)] for x, y in knots]},
                lambda p, k=knots: apply_isotonic(k, p),
            )
        )

    beta = fit_beta(train)
    if beta is not None:
        candidates.append(
            (
                "beta",
                {"a": round(beta[0], 6), "b": round(beta[1], 6), "c": round(beta[2], 6)},
                lambda p, b=beta: apply_beta(b, p),
            )
        )

    if not candidates:
        return CalibrationFit(
            method="identity",
            applied=False,
            reason="neither isotonic nor beta produced a usable fit",
            n_train=len(train),
            n_validation=len(validation),
            brier_identity=baseline,
        )

    scored = [
        (name, params, mapper, _brier(validation, mapper))
        for name, params, mapper in candidates
    ]
    name, params, _mapper, best_brier = min(scored, key=lambda row: row[3])
    improvement = baseline - best_brier

    threshold = min_brier_improvement(len(validation))
    if improvement < threshold:
        return CalibrationFit(
            method="identity",
            applied=False,
            reason=(
                f"best candidate ({name}) improved held-out Brier by "
                f"{improvement:.6f}, below the {threshold:.6f} required at "
                f"{len(validation)} validation observations; the uncalibrated "
                "estimate is kept"
            ),
            n_train=len(train),
            n_validation=len(validation),
            brier_identity=baseline,
            brier_fitted=best_brier,
            improvement=improvement,
            threshold=threshold,
        )

    return CalibrationFit(
        method=name,
        applied=True,
        reason=(
            f"{name} improved held-out Brier from {baseline:.6f} to "
            f"{best_brier:.6f} on {len(validation)} validation observations"
        ),
        parameters=params,
        n_train=len(train),
        n_validation=len(validation),
        brier_identity=baseline,
        brier_fitted=best_brier,
        improvement=improvement,
        threshold=threshold,
    )


def observations_from_snapshots(snapshots: Sequence[Any]) -> list[Observation]:
    """Turn settled recommendation snapshots into fitting observations.

    The probability and the outcome are both taken in the **recommended side's**
    frame, matching ``engine._outcome_value``: the model stated a probability that
    the side it picked would win, so that is the claim being calibrated.
    """
    from .engine import _outcome_value

    out: list[Observation] = []
    for index, snapshot in enumerate(sorted(snapshots, key=_snapshot_order)):
        probability = snapshot.fair_probability
        if probability is None:
            continue
        out.append(
            Observation(
                probability=float(probability),
                outcome=float(_outcome_value(snapshot)),
                sequence=index,
            )
        )
    return out


def _snapshot_order(snapshot: Any) -> tuple[Any, Any]:
    return (snapshot.snapshot_date, snapshot.rank or 0)


def fit_and_store(session: Any, *, model_name: str, version: str) -> CalibrationFit:
    """Fit on live settled snapshots and record the result against the model version.

    Demo rows are excluded by passing ``provenance=live`` rather than by filtering
    afterwards. The demo forecaster is deliberately miscalibrated - that is its
    whole purpose - so a map fitted on it would be a correction for a flaw that
    only exists in synthetic data, applied to real estimates.

    The result is stored whether or not it was adopted. A recorded refusal is the
    audit trail for why live estimates are still uncalibrated.
    """
    from pmvl_shared.db.models import ModelVersion
    from pmvl_shared.enums import DataProvenance
    from sqlalchemy import select

    from .engine import load_snapshots

    snapshots = load_snapshots(
        session, settled_only=True, provenance=DataProvenance.LIVE
    )
    fit = fit_calibration(observations_from_snapshots(snapshots))

    row = session.scalars(
        select(ModelVersion).where(
            ModelVersion.name == model_name, ModelVersion.version == version
        )
    ).first()
    if row is None:
        row = ModelVersion(name=model_name, version=version, description="")
        session.add(row)
    row.calibration = fit.as_dict()
    session.flush()

    log.info(
        "calibration fit for %s@%s: applied=%s (%s)",
        model_name, version, fit.applied, fit.reason,
    )
    return fit


def calibrator_from(fit: CalibrationFit | dict[str, Any] | None) -> Callable[[Decimal], Decimal]:
    """Rebuild the stored map as a callable, or the identity if none was adopted."""
    payload = fit.as_dict() if isinstance(fit, CalibrationFit) else (fit or {})
    if not payload.get("applied"):
        return lambda p: p

    method = payload.get("method")
    params = payload.get("parameters") or {}
    if method == "isotonic":
        knots = [(float(x), float(y)) for x, y in params.get("knots") or []]
        if not knots:
            return lambda p: p
        return lambda p: Decimal(str(apply_isotonic(knots, float(p))))
    if method == "beta":
        try:
            triple = (float(params["a"]), float(params["b"]), float(params["c"]))
        except (KeyError, TypeError, ValueError):
            return lambda p: p
        return lambda p: Decimal(str(apply_beta(triple, float(p))))
    return lambda p: p
