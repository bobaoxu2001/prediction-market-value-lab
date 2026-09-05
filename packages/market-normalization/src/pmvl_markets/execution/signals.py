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
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

from pmvl_shared.enums import DataProvenance, RecommendationState, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .models import ExecutionMode, OrderAction, OrderIntent, Outcome
from .sizing import SizingRefused

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
    #: When the book this decision was priced against was observed.
    quote_observed_at: datetime | None = None

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
        sizing_policy: Any = None,
    ) -> None:
        self.mode = mode
        self.max_signal_age = max_signal_age
        #: The component allowed to answer "how many contracts?". ``None`` - the
        #: default - blocks every signal: the research record publishes a
        #: capacity ceiling and a Kelly fraction, neither of which is a size, and
        #: an adapter that picks a number is an adapter that invented a strategy.
        self.sizing_policy = sizing_policy
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

        # Allowlist LIVE; refuse everything else.
        #
        # This was a denylist of the single literal "demo", which let through
        # `fixture` (a replayed recorded response - exactly the thing that must
        # not become an order), any casing, and any value nobody enumerated.
        # Every other classifier in this subsystem fails closed; this one failed
        # open, on the one gate standing between recorded data and a venue.
        provenance = (decision.provenance or "").strip().lower()
        if provenance != DataProvenance.LIVE.value:
            if provenance == DataProvenance.DEMO.value and self.allow_demo_provenance:
                pass  # explicitly admitted by a labelled study
            else:
                return refuse(
                    IneligibilityReason.DEMO_PROVENANCE,
                    f"provenance {provenance or '<empty>'!r} is not live model "
                    "output",
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
        # Round DOWN, always. Default Decimal rounding is half-even, which took
        # 0.5966 to 60c - a third of a cent *above* the price the edge was
        # measured at, on conservative edges of 1-5c. On the 12 live actionable
        # rows, 4 rounded up, one of them by 14.5% of its own edge. Paying more
        # than the research entry price is the one direction that silently
        # invalidates the trade, so the tick is always taken in our favour.
        limit_cents = int(
            (decision.entry_price * 100).to_integral_value(rounding=ROUND_DOWN)
        )
        if not 1 <= limit_cents <= 99:
            return refuse(
                IneligibilityReason.PRICE_OUT_OF_RANGE,
                f"entry price {decision.entry_price} -> {limit_cents}c",
            )

        # The unrounded research entry price, as a ceiling the limit is checked
        # against rather than derived from.
        max_entry_cents = int(
            (decision.entry_price * 100).to_integral_value(rounding=ROUND_DOWN)
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
        if self.sizing_policy is None:
            return refuse(
                IneligibilityReason.SIZE_POLICY_UNDEFINED,
                f"research publishes a capacity ceiling ({capacity}) but no "
                "position size, and no sizing policy is configured",
            )
        try:
            sizing = self.sizing_policy.decide(
                mode=self.mode, ticker=ticker, capacity_ceiling=capacity
            )
        except SizingRefused as exc:
            return refuse(IneligibilityReason.SIZE_POLICY_UNDEFINED, str(exc)[:200])
        quantity = sizing.requested_quantity

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
            # The ceiling is the research price, not the rounded limit. Setting
            # both to the same number made the risk gate's PRICE_ABOVE_MAX arm
            # compare a value against itself, so it could never fire.
            max_acceptable_price_cents=max_entry_cents,
            decision_timestamp=decision.created_at,
            signal_timestamp=signal_at,
            # The quote's own observation time where research recorded one.
            # Reusing the signal timestamp made MARKET_DATA_STALE a duplicate of
            # SIGNAL_STALE, so a decision could be fresh while the book it was
            # priced against was not - the same inert-limit shape as the daily
            # loss and spend breakers.
            market_data_timestamp=decision.quote_observed_at or signal_at,
            reason=(
                f"{STRATEGY_NAME} rank batch {decision.batch_id} "
                f"horizon {decision.horizon}"
            ),
            capacity_ceiling=capacity,
            sizing=sizing,
        )
