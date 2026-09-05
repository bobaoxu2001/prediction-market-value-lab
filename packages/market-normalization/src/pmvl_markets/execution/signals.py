"""Translate a research recommendation into an order intent, or refuse to.

The canonical research decision in this repository is the ``recommendations``
table (:class:`pmvl_shared.db.models.Recommendation`), written by the ranking
pipeline. This module is the *only* bridge from it to the execution layer, and it
is deliberately narrow: it changes representation and nothing else.

What it must never do
---------------------
Recompute a fair value. Invent an edge. Invent a ticker. Invent a size. Choose a
different market because the intended one is missing. Invert YES/NO. Rescue a
stale decision. Every one of those is a way to make the execution layer disagree
with the research record, and the research record is the thing under study.

So the adapter is total: every recommendation produces either an
:class:`~pmvl_markets.execution.models.OrderIntent` or an
:class:`IneligibleSignal` naming exactly why not. There is no third outcome and
no silent drop, because the denominator matters - "how many signals did the
robot want to trade" is unanswerable if the refusals are not recorded.

Eligibility is not reinterpreted
--------------------------------
A recommendation whose ``state`` is not ``STILL_ACTIONABLE`` is not actionable.
``SETTLED``, ``MARKET_CLOSED``, ``NO_LONGER_ACTIONABLE`` and ``EDGE_REDUCED`` are
research conclusions, and turning one of them into an order would be this module
overruling the model it is supposed to serve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pmvl_shared.enums import RecommendationState, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .models import ExecutionMode, OrderAction, OrderIntent, Outcome

log = get_logger(__name__)

__all__ = [
    "ResearchDecision",
    "IneligibleSignal",
    "IneligibilityReason",
    "SignalExecutionAdapter",
    "STRATEGY_NAME",
]

#: Named so a shadow plan can be traced back to the pipeline that produced it.
STRATEGY_NAME = "research-recommendations"


class IneligibilityReason(StrEnum):
    """Why a research decision did not become an order intent.

    Every value is a refusal to act, never a downgrade of the research verdict.
    """

    NOT_ACTIONABLE = "not_actionable"
    MISSING_TICKER = "missing_ticker"
    NOT_KALSHI = "not_kalshi"
    MARKET_CLOSED = "market_closed"
    MARKET_NOT_OPEN = "market_not_open"
    WRONG_EVENT_DATE = "wrong_event_date"
    MISSING_EXECUTABLE_PRICE = "missing_executable_price"
    PRICE_OUT_OF_RANGE = "price_out_of_range"
    MISSING_EDGE = "missing_edge"
    MISSING_SIZE = "missing_size"
    SIZE_POLICY_UNDEFINED = "size_policy_undefined"
    STALE_SIGNAL = "stale_signal"
    STALE_MARKET_DATA = "stale_market_data"
    ORIENTATION_MISSING = "orientation_missing"
    DEMO_PROVENANCE = "demo_provenance"


@dataclass(frozen=True, slots=True)
class ResearchDecision:
    """One row of the research record, in the shape the adapter consumes.

    A faithful projection of ``recommendations`` joined to its market. Nothing is
    computed here; every field is carried across.
    """

    recommendation_id: int
    batch_id: str
    ticker: str
    platform: str
    side: Side
    state: RecommendationState
    market_status: str

    entry_price: Decimal | None
    current_price: Decimal | None
    fair_probability: Decimal | None
    conservative_probability: Decimal | None
    net_ev_per_contract: Decimal | None
    conservative_net_ev: Decimal | None
    total_cost_per_contract: Decimal | None
    recommended_position_cap: Decimal | None

    model_version: str = ""
    provenance: str = ""
    horizon: str = ""
    created_at: datetime | None = None
    evidence_updated_at: datetime | None = None
    close_time: datetime | None = None
    expected_resolution_time: datetime | None = None

    @property
    def signal_id(self) -> str:
        return f"rec-{self.recommendation_id}"


@dataclass(frozen=True, slots=True)
class IneligibleSignal:
    """A decision that will not become an order, and exactly why."""

    signal_id: str
    ticker: str
    reason: IneligibilityReason
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "ticker": self.ticker,
            "reason": self.reason.value,
            "detail": self.detail,
        }


class SignalExecutionAdapter:
    """Pure translation. No I/O, no venue, no risk decision.

    Risk is deliberately somebody else's job: this class decides whether a
    decision is *expressible* as an order, and the risk gate decides whether that
    order is *permissible*. Merging them would let a translation bug silently
    widen a limit.
    """

    def __init__(
        self,
        *,
        mode: ExecutionMode = ExecutionMode.SHADOW,
        max_signal_age: timedelta = timedelta(hours=6),
        allow_demo_provenance: bool = False,
        position_size: int | None = None,
    ) -> None:
        self.mode = mode
        self.max_signal_age = max_signal_age
        #: Explicit, caller-supplied position size. ``None`` - the default -
        #: blocks every signal, because the research record does not yet publish
        #: a size and this module refuses to invent one. A shadow study may set
        #: it to a declared constant to exercise the rest of the path.
        self.position_size = position_size
        # Seed/fixture rows must not be mistaken for model output. Off by
        # default; the shadow replay turns it on explicitly and labels the run.
        self.allow_demo_provenance = allow_demo_provenance

    def to_intent(
        self, decision: ResearchDecision, *, now: datetime | None = None
    ) -> OrderIntent | IneligibleSignal:
        now = now or utcnow()
        ticker = (decision.ticker or "").strip()

        def refuse(reason: IneligibilityReason, detail: str = "") -> IneligibleSignal:
            return IneligibleSignal(decision.signal_id, ticker, reason, detail)

        # ---- eligibility: the research verdict is taken at face value --------
        if decision.state is not RecommendationState.STILL_ACTIONABLE:
            return refuse(
                IneligibilityReason.NOT_ACTIONABLE,
                f"recommendation state is {decision.state.value}",
            )

        if not self.allow_demo_provenance and decision.provenance == "demo":
            return refuse(
                IneligibilityReason.DEMO_PROVENANCE,
                "seed/fixture row, not model output",
            )

        # ---- market identity: exact or nothing ------------------------------
        if not ticker:
            return refuse(IneligibilityReason.MISSING_TICKER)
        if decision.platform.lower() != "kalshi":
            return refuse(
                IneligibilityReason.NOT_KALSHI,
                f"platform {decision.platform}",
            )
        if decision.market_status and decision.market_status.lower() != "open":
            return refuse(
                IneligibilityReason.MARKET_NOT_OPEN,
                f"market status {decision.market_status}",
            )
        if decision.close_time is not None and decision.close_time <= now:
            return refuse(
                IneligibilityReason.MARKET_CLOSED,
                f"closed at {decision.close_time.isoformat()}",
            )

        # ---- orientation: carried, never inferred ---------------------------
        if decision.side is None:
            return refuse(IneligibilityReason.ORIENTATION_MISSING)
        outcome = Outcome.from_side(decision.side)

        # ---- freshness ------------------------------------------------------
        signal_at = decision.evidence_updated_at or decision.created_at
        if signal_at is None:
            return refuse(
                IneligibilityReason.STALE_SIGNAL, "no decision timestamp"
            )
        age = now - signal_at
        if age > self.max_signal_age:
            return refuse(
                IneligibilityReason.STALE_SIGNAL,
                f"decision is {age.total_seconds() / 3600:.1f}h old",
            )

        # ---- price: the decision's own, never re-optimised ------------------
        # `entry_price` is the price the EV was computed at. Paying more than it
        # invalidates the edge that justified the trade, so it is both the limit
        # and the ceiling. This module does not search for a better price;
        # execution optimisation is not in scope and inventing one here would
        # mean the order no longer matches the research record.
        if decision.entry_price is None:
            return refuse(IneligibilityReason.MISSING_EXECUTABLE_PRICE)
        limit_cents = int((decision.entry_price * 100).to_integral_value())
        if not 1 <= limit_cents <= 99:
            return refuse(
                IneligibilityReason.PRICE_OUT_OF_RANGE,
                f"entry price {decision.entry_price} -> {limit_cents}c",
            )

        # ---- edge: measured by research, not by this module -----------------
        edge = decision.conservative_net_ev
        if edge is None:
            return refuse(
                IneligibilityReason.MISSING_EDGE, "no conservative net EV"
            )

        # ---- size: the research record does not currently define one --------
        #
        # `recommended_position_cap` is produced by `max_profitable_size` - the
        # largest size whose *marginal* contract still has positive EV, bounded
        # at 5000. That is a book-depth capacity ceiling, not a recommended
        # position, and reading it as the order quantity asked for 213-1829
        # contracts on real rows: roughly 250x anything the model sized.
        #
        # It failed closed on the risk caps, which is why it was never dangerous,
        # but it made the shadow measurement a lie - the denominator would have
        # read "risk rejected our trades" when the truth was "the adapter asked
        # for the whole book". Inventing a smaller number here would be worse:
        # the adapter's one job is not to invent a size.
        #
        # So this blocks, explicitly, until the research layer publishes a
        # position-size policy. That is a research decision, not an execution one.
        if decision.recommended_position_cap is None:
            return refuse(IneligibilityReason.MISSING_SIZE)
        capacity = int(decision.recommended_position_cap)
        if capacity <= 0:
            return refuse(
                IneligibilityReason.MISSING_SIZE,
                f"capacity ceiling is {decision.recommended_position_cap}",
            )
        if self.position_size is None:
            return refuse(
                IneligibilityReason.SIZE_POLICY_UNDEFINED,
                "research publishes a capacity ceiling "
                f"({capacity}) but no position size; execution will not choose one",
            )
        quantity = min(self.position_size, capacity)

        return OrderIntent(
            intent_id=decision.signal_id,
            ticker=ticker,
            outcome=outcome,
            action=OrderAction.BUY,
            limit_price_cents=limit_cents,
            quantity=quantity,
            mode=self.mode,
            strategy=STRATEGY_NAME,
            strategy_version=decision.model_version or "unversioned",
            decision_epoch=decision.batch_id or decision.signal_id,
            source_signal_id=decision.signal_id,
            model_probability=decision.fair_probability,
            conservative_probability=decision.conservative_probability,
            expected_edge_after_fees=edge,
            max_acceptable_price_cents=limit_cents,
            decision_timestamp=decision.created_at,
            signal_timestamp=signal_at,
            market_data_timestamp=signal_at,
            reason=(
                f"{STRATEGY_NAME} rank batch {decision.batch_id} "
                f"horizon {decision.horizon}"
            ),
        )
