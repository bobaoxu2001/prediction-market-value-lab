"""How old each kind of data may be before it stops being usable.

There was one threshold, ``max_quote_age_seconds``, applied to everything. That is
wrong in both directions at once. A top-of-book quote five minutes old is already
suspect, because the whole premise of the platform is that the ask is executable. A
market's *title and rules* five minutes old are perfectly current - they change
maybe once in a contract's life. Using one number means either rejecting good rule
data or accepting stale prices, and the codebase chose the second.

Each data type therefore carries its own policy, and each policy answers three
separate questions:

- **soft stale**: show a warning, keep using it
- **hard stale**: stop using it for anything that gates a recommendation
- **blocks eligibility**: whether hard-stale makes a recommendation impossible

The last is not implied by the second. A stale *rule text* should block a
cross-platform arbitrage claim, because the claim rests on the rules matching. A
stale *24h volume* should not block anything; it is context, not a precondition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FreshnessState(StrEnum):
    """Reported per input, so a reader can see which one is the problem."""

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    #: Never observed at all. Distinct from STALE: absent data and old data call
    #: for different responses, and merging them hides which one happened.
    UNAVAILABLE = "unavailable"

    @property
    def usable_for_recommendation(self) -> bool:
        return self in (FreshnessState.FRESH, FreshnessState.AGING)


class DataType(StrEnum):
    MARKET_METADATA = "market_metadata"
    TOP_OF_BOOK = "top_of_book"
    FULL_ORDERBOOK = "full_orderbook"
    EXTERNAL_MODEL_INPUT = "external_model_input"
    RULE_TEXT = "rule_text"
    SETTLEMENT_STATE = "settlement_state"
    MODEL_PREDICTION = "model_prediction"
    ARBITRAGE_SCAN = "arbitrage_scan"
    RECOMMENDATION_SNAPSHOT = "recommendation_snapshot"


@dataclass(frozen=True)
class FreshnessPolicy:
    data_type: DataType
    target_cadence_seconds: int
    soft_stale_seconds: int
    hard_stale_seconds: int
    #: Whether hard-stale data makes a recommendation impossible, as opposed to
    #: merely worth warning about.
    blocks_eligibility: bool
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.target_cadence_seconds <= self.soft_stale_seconds <= self.hard_stale_seconds:
            raise ValueError(
                f"{self.data_type}: thresholds must satisfy "
                "target <= soft <= hard"
            )

    def state(self, age_seconds: float | None) -> FreshnessState:
        if age_seconds is None:
            return FreshnessState.UNAVAILABLE
        if age_seconds <= self.soft_stale_seconds:
            return FreshnessState.FRESH
        if age_seconds <= self.hard_stale_seconds:
            return FreshnessState.AGING
        return FreshnessState.STALE


POLICIES: dict[DataType, FreshnessPolicy] = {
    DataType.TOP_OF_BOOK: FreshnessPolicy(
        data_type=DataType.TOP_OF_BOOK,
        target_cadence_seconds=180,
        soft_stale_seconds=600,
        hard_stale_seconds=1800,
        blocks_eligibility=True,
        rationale=(
            "The executable ask is the platform's central claim. A half-hour-old "
            "quote is not a price anyone can trade against."
        ),
    ),
    DataType.FULL_ORDERBOOK: FreshnessPolicy(
        data_type=DataType.FULL_ORDERBOOK,
        target_cadence_seconds=180,
        soft_stale_seconds=900,
        hard_stale_seconds=3600,
        blocks_eligibility=True,
        rationale=(
            "Depth decays more slowly than the top of book, but sizing a position "
            "against an hour-old ladder is guesswork."
        ),
    ),
    DataType.MARKET_METADATA: FreshnessPolicy(
        data_type=DataType.MARKET_METADATA,
        target_cadence_seconds=600,
        soft_stale_seconds=7200,
        hard_stale_seconds=86400,
        blocks_eligibility=False,
        rationale="Titles and categories change rarely; staleness here is cosmetic.",
    ),
    DataType.RULE_TEXT: FreshnessPolicy(
        data_type=DataType.RULE_TEXT,
        target_cadence_seconds=86400,
        soft_stale_seconds=7 * 86400,
        hard_stale_seconds=30 * 86400,
        blocks_eligibility=True,
        rationale=(
            "Rules change almost never, so the thresholds are generous - but a "
            "cross-platform equivalence claim rests entirely on them, so stale "
            "rules must block it rather than merely warn."
        ),
    ),
    DataType.EXTERNAL_MODEL_INPUT: FreshnessPolicy(
        data_type=DataType.EXTERNAL_MODEL_INPUT,
        target_cadence_seconds=3600,
        soft_stale_seconds=7200,
        hard_stale_seconds=21600,
        blocks_eligibility=True,
        rationale=(
            "The independent estimate is built from these. Stale inputs mean the "
            "only figure that can demonstrate an edge is no longer supported."
        ),
    ),
    DataType.SETTLEMENT_STATE: FreshnessPolicy(
        data_type=DataType.SETTLEMENT_STATE,
        target_cadence_seconds=1800,
        soft_stale_seconds=7200,
        hard_stale_seconds=86400,
        blocks_eligibility=False,
        rationale=(
            "Affects the track record, not live eligibility. A settled market is "
            "excluded by its status, not by the age of the settlement record."
        ),
    ),
    DataType.MODEL_PREDICTION: FreshnessPolicy(
        data_type=DataType.MODEL_PREDICTION,
        target_cadence_seconds=7200,
        soft_stale_seconds=14400,
        hard_stale_seconds=43200,
        blocks_eligibility=True,
        rationale="A recommendation cannot rest on a half-day-old estimate.",
    ),
    DataType.ARBITRAGE_SCAN: FreshnessPolicy(
        data_type=DataType.ARBITRAGE_SCAN,
        target_cadence_seconds=60,
        soft_stale_seconds=900,
        hard_stale_seconds=3600,
        blocks_eligibility=True,
        rationale=(
            "Arbitrage windows close in seconds. An hour-old scan is a historical "
            "record, not an opportunity."
        ),
    ),
    DataType.RECOMMENDATION_SNAPSHOT: FreshnessPolicy(
        data_type=DataType.RECOMMENDATION_SNAPSHOT,
        target_cadence_seconds=86400,
        soft_stale_seconds=2 * 86400,
        hard_stale_seconds=7 * 86400,
        blocks_eligibility=False,
        rationale="The immutable daily record; age is expected and is not a fault.",
    ),
}


@dataclass
class FreshnessAssessment:
    """One input's freshness, and what it implies."""

    data_type: DataType
    state: FreshnessState
    age_seconds: float | None
    blocks_eligibility: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "data_type": self.data_type.value,
            "state": self.state.value,
            "age_seconds": self.age_seconds,
            "blocks_eligibility": self.blocks_eligibility,
        }


def assess(data_type: DataType, age_seconds: float | None) -> FreshnessAssessment:
    policy = POLICIES[data_type]
    state = policy.state(age_seconds)
    return FreshnessAssessment(
        data_type=data_type,
        state=state,
        age_seconds=age_seconds,
        # UNAVAILABLE blocks wherever STALE would. Absent data cannot satisfy a
        # precondition any better than expired data can, and treating "never
        # fetched" as acceptable is how a missing feed becomes an invisible one.
        blocks_eligibility=(
            policy.blocks_eligibility and not state.usable_for_recommendation
        ),
    )


def blocking_inputs(assessments: list[FreshnessAssessment]) -> list[str]:
    """Names of the inputs that make a recommendation impossible right now."""
    return [a.data_type.value for a in assessments if a.blocks_eligibility]


def eligible(assessments: list[FreshnessAssessment]) -> bool:
    """Fails closed: any blocking input that is stale or absent denies eligibility."""
    return not blocking_inputs(assessments)
