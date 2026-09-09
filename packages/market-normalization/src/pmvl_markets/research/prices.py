"""Market price semantics: what is observable, and what is actually executable.

The midpoint is not a price anyone can trade. It is the average of two prices,
at least one of which nobody is offering, and on a 1-cent-tick contract with a
3-cent spread it flatters every entry by 1.5 cents. Weather contracts trade at
spreads wide enough that midpoint-versus-executable is the difference between an
apparent edge and no edge at all, so the two are separate fields here and the
executable one is the only one permitted in a return claim.

Kalshi quotes YES and NO separately, both in cents, and the two sides are not
redundant: ``no_ask`` is a real resting offer, not ``100 - yes_bid``. Deriving
one from the other assumes a two-sided book that is always perfectly consistent,
which is exactly the assumption that produces free money in a backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pmvl_shared.enums import Platform

from ..pricing.fees import KALSHI_TAKER_RATE, fee_per_contract
from .exclusions import Exclusion

#: Order size at which the venue fee is charged against an edge.
#:
#: One contract, deliberately. Kalshi ceils the taker fee to the whole cent on
#: the *whole order*, so the per-contract fee is at its maximum at size 1 and
#: falls as size grows. Charging the size-1 fee is therefore the conservative
#: reading, and on these series it is also the realistic one: median depth at
#: the touch is a few dollars.
DEFAULT_FEE_CONTRACTS = Decimal(1)


@dataclass(frozen=True)
class MarketState:
    """One archived book, expressed as probabilities with provenance kept."""

    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None

    @property
    def spread(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def mid(self) -> Decimal | None:
        """Descriptive only. Never an entry price in a return calculation."""
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def executable_yes_buy(self) -> Decimal | None:
        """What buying YES costs: the offer, not the midpoint."""
        return self.yes_ask

    @property
    def executable_yes_sell(self) -> Decimal | None:
        """What selling YES realises: the bid."""
        return self.yes_bid

    @property
    def executable_no_buy(self) -> Decimal | None:
        """The NO offer as quoted.

        Deliberately not ``1 - yes_bid``. That identity holds only when both
        sides are simultaneously present and consistent, and when they are not,
        the synthesised price is one nobody was offering.
        """
        return self.no_ask

    def validate(self) -> Exclusion | None:
        """Reject books that cannot be reasoned about, with a reason."""
        for value in (self.yes_bid, self.yes_ask, self.no_bid, self.no_ask):
            if value is not None and not (Decimal(0) <= value <= Decimal(1)):
                return Exclusion.BOOK_INVALID
        if self.yes_bid is not None and self.yes_ask is not None:
            if self.yes_bid > self.yes_ask:
                # Crossed book: either a capture artefact or a venue anomaly.
                # Either way it is not a state to score a model against.
                return Exclusion.BOOK_INVALID
        if self.yes_ask is None and self.no_ask is None:
            # Nothing buyable on either side; there is no executable price to
            # compare a model to, only a quote to look at.
            return Exclusion.NO_EXECUTABLE_PRICE
        return None

    def as_dict(self) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return str(v) if v is not None else None

        return {
            "yes_bid": s(self.yes_bid),
            "yes_ask": s(self.yes_ask),
            "no_bid": s(self.no_bid),
            "no_ask": s(self.no_ask),
            "mid": s(self.mid),
            "spread": s(self.spread),
            "executable_yes_buy": s(self.executable_yes_buy),
            "executable_yes_sell": s(self.executable_yes_sell),
            "executable_no_buy": s(self.executable_no_buy),
        }


@dataclass(frozen=True)
class Edge:
    """Model minus market, kept in separate forms that must not be collapsed.

    They are different numbers answering different questions, and collapsing
    them is how a descriptive gap becomes a claimed profit.

    ``raw_mid`` is a research diagnostic against a price nobody offers.

    The ``executable_*`` fields are gross of fees: they answer "how far is the
    model from the offer", which is a statement about mispricing, not about
    money. They were previously documented as the fields that could support a
    position claim; they cannot. On these series the taker fee at the money is
    1.75 probability points against a median quoted spread of 1.00, so a gross
    edge smaller than the fee is a loss that reads as a profit.

    Only ``executable_*_net`` may support a statement about a position. That is
    also what the frozen protocol declares - see ``protocol.py``,
    ``fee_treatment = "kalshi_taker_fee_applied_to_executable_only"`` - and the
    fee is applied to the executable legs only, never to ``raw_mid``.
    """

    raw_mid: Decimal | None
    executable_yes: Decimal | None
    executable_no: Decimal | None
    #: Executable edge after the venue's taker fee. The only tradeable number.
    executable_yes_net: Decimal | None = None
    executable_no_net: Decimal | None = None
    #: The fee actually charged, per contract, at the size it was charged for.
    #: Reported so a thin edge can be seen to die to fees rather than vanishing.
    fee_per_contract_yes: Decimal | None = None
    fee_per_contract_no: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return str(v) if v is not None else None

        return {
            "raw_mid_edge": s(self.raw_mid),
            "executable_yes_edge": s(self.executable_yes),
            "executable_no_edge": s(self.executable_no),
            "executable_yes_edge_net": s(self.executable_yes_net),
            "executable_no_edge_net": s(self.executable_no_net),
            "fee_per_contract_yes": s(self.fee_per_contract_yes),
            "fee_per_contract_no": s(self.fee_per_contract_no),
        }


def compute_edge(
    model_probability: Decimal,
    state: MarketState,
    *,
    contracts: Decimal = DEFAULT_FEE_CONTRACTS,
    fee_rate: Decimal = KALSHI_TAKER_RATE,
    fee_type: str = "",
) -> Edge:
    """Edges in probability units throughout, gross and net of the venue fee.

    Everything is a probability in [0,1]. Kalshi quotes cents and it is
    tempting to mix units; a single cents-versus-probability slip is a factor of
    100 and produces an edge that looks spectacular rather than wrong.

    The fee is a probability too: a fee of 2 cents on a contract that pays 1
    dollar is 0.02 in these units, which is why it can be subtracted from an
    edge directly. ``fee_rate`` should already carry the series
    ``fee_multiplier``; the default is Kalshi's published rate at multiplier 1.

    Applied to the executable legs only, per ``protocol.py``'s frozen
    ``fee_treatment``. ``raw_mid`` is left gross because it is a diagnostic
    against an unofferable price - charging a fee against a trade nobody could
    have made would imply the midpoint was executable, which is the exact
    confusion this module exists to prevent.
    """
    mid = state.mid
    yes_ask = state.executable_yes_buy
    no_ask = state.executable_no_buy

    def fee_at(price: Decimal | None) -> Decimal | None:
        """Per-contract fee at the price actually paid, or None if unquoted."""
        if price is None:
            return None
        return fee_per_contract(
            Platform.KALSHI, contracts, price, rate=fee_rate, fee_type=fee_type
        )

    def net(gross: Decimal | None, fee: Decimal | None) -> Decimal | None:
        # An unknown fee must not read as a free trade.
        if gross is None or fee is None:
            return None
        return gross - fee

    # Buying YES: pay the YES offer, receive 1 if it settles YES.
    gross_yes = None if yes_ask is None else model_probability - yes_ask
    # Buying NO: pay the NO offer, receive 1 if it settles NO, which has
    # modelled probability (1 - p).
    gross_no = None if no_ask is None else (Decimal(1) - model_probability) - no_ask
    fee_yes, fee_no = fee_at(yes_ask), fee_at(no_ask)

    return Edge(
        raw_mid=None if mid is None else model_probability - mid,
        executable_yes=gross_yes,
        executable_no=gross_no,
        executable_yes_net=net(gross_yes, fee_yes),
        executable_no_net=net(gross_no, fee_no),
        fee_per_contract_yes=fee_yes,
        fee_per_contract_no=fee_no,
    )
