"""Three situations that produce an empty list, and must never be confused.

An empty opportunity list is the platform's most common output and its most
dangerous, because three completely different things render identically:

``ZERO_OPPORTUNITIES``
    The pipeline ran, the data is fresh, every gate was applied, and nothing
    cleared them. This is the expected result most days and is a *finding*.
``PIPELINE_DID_NOT_RUN``
    No scan happened. The list is empty because nothing looked, and saying
    "no opportunities today" would assert a conclusion nobody reached.
``PIPELINE_RAN_WITH_STALE_OR_INCOMPLETE_DATA``
    A scan happened but a provider failed or an input was past its hard freshness
    threshold. Some of the market was not examined, and the empty list is a
    statement about our coverage rather than about the market.

Collapsing these into "0 opportunities" is how a broken scanner looks like a quiet
market. Every health status here is machine-readable, and every one carries a
sentence a person can act on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from pmvl_shared.cadence import CADENCE_BY_JOB, DeploymentMode, SchedulerStatus
from pmvl_shared.freshness import DataType, assess

from .pipeline_status import job_status


class EmptyResultCause(StrEnum):
    ZERO_OPPORTUNITIES = "zero_opportunities"
    PIPELINE_DID_NOT_RUN = "pipeline_did_not_run"
    PIPELINE_RAN_WITH_STALE_OR_INCOMPLETE_DATA = "pipeline_ran_with_stale_or_incomplete_data"
    #: A frozen artefact is not a pipeline that failed; it is a deployment that
    #: never had one. Separated so a snapshot does not read as an outage.
    SERVING_FROZEN_SNAPSHOT = "serving_frozen_snapshot"


EXPLANATIONS: dict[EmptyResultCause, str] = {
    EmptyResultCause.ZERO_OPPORTUNITIES: (
        "The scan ran on fresh data and nothing cleared the gates. This is the "
        "normal result: both venues are actively arbitraged, and an opportunity is "
        "only listed when it survives every cost and every precondition."
    ),
    EmptyResultCause.PIPELINE_DID_NOT_RUN: (
        "No scan has run recently, so nothing looked. The empty list says nothing "
        "about the market."
    ),
    EmptyResultCause.PIPELINE_RAN_WITH_STALE_OR_INCOMPLETE_DATA: (
        "A scan ran, but some inputs were stale or a data source failed. Part of "
        "the market was not examined, so this is a statement about coverage rather "
        "than about the market."
    ),
    EmptyResultCause.SERVING_FROZEN_SNAPSHOT: (
        "This deployment serves a frozen research snapshot and runs no scanner. "
        "Results are those of the scan that produced the artefact."
    ),
}


class HealthLevel(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"
    #: The deployment is behaving correctly for what it is, but is not live.
    NOT_APPLICABLE = "not_applicable"


def classify_empty_result(
    session: Session,
    mode: DeploymentMode,
    *,
    job_name: str = "arbitrage",
    input_age_seconds: float | None = None,
    input_type: DataType = DataType.ARBITRAGE_SCAN,
) -> dict[str, Any]:
    """Why a result set is empty, in a form both a machine and a person can use."""
    if not mode.runs_scheduled_jobs:
        cause = EmptyResultCause.SERVING_FROZEN_SNAPSHOT
        level = HealthLevel.NOT_APPLICABLE
    else:
        cadence = CADENCE_BY_JOB.get(job_name)
        status = (
            job_status(session, cadence, mode)["scheduler_status"]
            if cadence
            else SchedulerStatus.UNKNOWN.value
        )
        freshness = assess(input_type, input_age_seconds)
        if status == SchedulerStatus.STALLED.value:
            cause = EmptyResultCause.PIPELINE_DID_NOT_RUN
            level = HealthLevel.FAILING
        elif not freshness.state.usable_for_recommendation:
            cause = EmptyResultCause.PIPELINE_RAN_WITH_STALE_OR_INCOMPLETE_DATA
            level = HealthLevel.DEGRADED
        else:
            cause = EmptyResultCause.ZERO_OPPORTUNITIES
            level = HealthLevel.OK

    return {
        "cause": cause.value,
        "level": level.value,
        "explanation": EXPLANATIONS[cause],
        # The one thing a caller most often gets wrong: treating an empty list as
        # a finding when it is an outage. Stated explicitly so it cannot be
        # inferred incorrectly from the absence of rows.
        "empty_result_is_a_finding": cause
        in (
            EmptyResultCause.ZERO_OPPORTUNITIES,
            EmptyResultCause.SERVING_FROZEN_SNAPSHOT,
        ),
    }
