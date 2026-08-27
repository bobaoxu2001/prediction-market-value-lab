"""The Phase 4C1 evaluation protocol, frozen before any result is seen.

Everything here is precommitted. It is written down now, while the cohort is too
small to evaluate, precisely so that no choice in it can be influenced by an
outcome - a decision offset picked after seeing which one looks best is not a
research design, it is a search reported as one.

The protocol carries a content hash. Changing any frozen value changes the hash,
so a result can be checked against the protocol it claims to follow rather than
against whatever the file happens to say later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "4c1-protocol-v1"


@dataclass(frozen=True)
class EvaluationProtocol:
    """Precommitted specification for the first Model-vs-Market evaluation."""

    version: str = PROTOCOL_VERSION

    #: Hours before market close at which the observation is taken. ONE value,
    #: not a set to search over.
    decision_offset_hours: float = 6.0

    model_version: str = "weather-normal-provisional-v1"
    #: Frozen as a documented prior. Fitting it on the evaluation cohort and
    #: then evaluating on the same rows is not calibration, it is scoring a
    #: model against its own training set.
    sigma_source: str = "PROVISIONAL_PRIOR"
    sigma_fitted: bool = False

    #: Primary benchmark for FORECASTING quality. The midpoint is not tradable
    #: and is the right comparison for "does the model know more than the
    #: consensus".
    primary_market_probability: str = "market_mid"
    #: Separate benchmark for ACTIONABILITY. Kept apart because a model can beat
    #: the consensus and still lose to the spread, and reporting one as the
    #: other is how a forecasting result becomes a trading claim.
    secondary_market_probability: str = "executable_yes_ask_and_no_ask"
    fee_treatment: str = "kalshi_taker_fee_applied_to_executable_only"
    slippage_treatment: str = "none_modelled_top_of_book_size_capped"

    primary_metric: str = "mean_paired_station_day_delta_brier"
    #: delta = model - market. Negative means the model is better.
    primary_metric_sign: str = "negative_favours_model"
    secondary_metric: str = "mean_paired_station_day_delta_log_loss"

    #: One station's daily maximum resolves every contract in its ladder, so the
    #: ladder is one realisation and not N of them.
    independent_unit: str = "station_day"
    bootstrap_unit: str = "station_day"
    within_station_day_aggregation: str = "unweighted_mean_of_contract_scores"

    #: Below this, the evaluation does not run.
    minimum_independent_events: int = 30

    notes: tuple[str, ...] = field(
        default_factory=lambda: (
            "Calibration curves at N~30 are descriptive only; bins are not "
            "evidence of calibration quality.",
            "Settlement labels come from the venue terminal result. The archived "
            "CLI maximum is independent audit evidence and is never the label.",
        )
    )

    def as_dict(self) -> dict[str, Any]:
        out = {
            k: (list(v) if isinstance(v, tuple) else v)
            for k, v in self.__dict__.items()
        }
        return out

    def content_hash(self) -> str:
        """Stable hash over the frozen values.

        Any change to a frozen choice changes this, which is what makes the
        freeze checkable rather than merely stated.
        """
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


#: The single frozen instance. Import this rather than constructing one.
FROZEN_PROTOCOL = EvaluationProtocol()


def render(protocol: EvaluationProtocol = FROZEN_PROTOCOL) -> str:
    d = protocol.as_dict()
    lines = ["", "PHASE 4C1 EVALUATION PROTOCOL (FROZEN)", "=" * 60]
    for key in (
        "version", "decision_offset_hours", "model_version", "sigma_source",
        "sigma_fitted", "primary_market_probability",
        "secondary_market_probability", "fee_treatment", "slippage_treatment",
        "primary_metric", "primary_metric_sign", "secondary_metric",
        "independent_unit", "bootstrap_unit", "within_station_day_aggregation",
        "minimum_independent_events",
    ):
        lines.append(f"  {key:<34} {d[key]}")
    lines.append(f"  {'content_hash':<34} {protocol.content_hash()}")
    for note in d["notes"]:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
