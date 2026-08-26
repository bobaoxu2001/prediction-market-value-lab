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

from .exclusions import Exclusion


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
    """Model minus market, kept in three separate forms.

    They are different numbers answering different questions, and collapsing
    them is how a descriptive gap becomes a claimed profit. ``raw_mid`` is a
    research diagnostic; only the executable variants can support any statement
    about a position.
    """

    raw_mid: Decimal | None
    executable_yes: Decimal | None
    executable_no: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return str(v) if v is not None else None

        return {
            "raw_mid_edge": s(self.raw_mid),
            "executable_yes_edge": s(self.executable_yes),
            "executable_no_edge": s(self.executable_no),
        }


def compute_edge(model_probability: Decimal, state: MarketState) -> Edge:
    """Edges in probability units throughout.

    Everything is a probability in [0,1]. Kalshi quotes cents and it is
    tempting to mix units; a single cents-versus-probability slip is a factor of
    100 and produces an edge that looks spectacular rather than wrong.
    """
    mid = state.mid
    yes_ask = state.executable_yes_buy
    no_ask = state.executable_no_buy
    return Edge(
        raw_mid=None if mid is None else model_probability - mid,
        # Buying YES: pay the YES offer, receive 1 if it settles YES.
        executable_yes=None if yes_ask is None else model_probability - yes_ask,
        # Buying NO: pay the NO offer, receive 1 if it settles NO, which has
        # modelled probability (1 - p).
        executable_no=None if no_ask is None else (Decimal(1) - model_probability) - no_ask,
    )
