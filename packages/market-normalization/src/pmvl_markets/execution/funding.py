"""Per-venue cash, and reservations against it, so two candidates cannot spend it twice.

A cross-venue pair needs money sitting on *both* venues at the same time, and the
money on one cannot pay for the other. Tracking a single pooled balance would let two
candidates that each fit the pool be approved together and then fail at submission,
after the first legs are already live - which is the expensive way to discover a
funding conflict.

So the balance is per venue, and approving a candidate *reserves* against that venue
rather than merely comparing to it. A reservation is held from the moment a candidate
is approved until its legs fill or are abandoned, and the reserved amount is invisible
to every other candidate in between.

Reserving both legs is all-or-nothing. A pair that can fund its Kalshi leg but not its
Polymarket leg must take neither: a half-funded pair is a one-legged position waiting
to happen, which is the exact outcome the pair structure exists to avoid.

**An unknown balance refuses.** ``available_usd=None`` means the venue was never read,
and :meth:`FundingLedger.reserve_all` declines rather than assuming solvency. That is
the default for any venue this process has not authenticated against, which is why the
future live path is blocked by absence of information rather than by a flag someone
has to remember to set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.enums import Platform
from pmvl_shared.money import ZERO, quantize_usd
from pmvl_shared.timeutil import utcnow

__all__ = [
    "BalanceSource",
    "ReservationState",
    "VenueFunds",
    "Reservation",
    "ReservationOutcome",
    "FundingLedger",
]


class BalanceSource(StrEnum):
    """Where a declared balance came from. Recorded so an audit can weigh it."""

    #: Read from the venue's authenticated account endpoint.
    VENUE_API = "venue_api"
    #: Typed in by an operator, e.g. for a shadow run with a notional bankroll.
    OPERATOR_DECLARED = "operator_declared"
    #: Never read. Blocks reservation.
    UNKNOWN = "unknown"


class ReservationState(StrEnum):
    HELD = "held"
    COMMITTED = "committed"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class VenueFunds:
    venue: Platform
    available_usd: Decimal | None
    source: BalanceSource
    as_of: datetime

    @property
    def is_known(self) -> bool:
        return self.available_usd is not None and self.source is not BalanceSource.UNKNOWN


@dataclass
class Reservation:
    ref: str
    venue: Platform
    amount_usd: Decimal
    state: ReservationState = ReservationState.HELD
    created_at: datetime = field(default_factory=utcnow)
    settled_at: datetime | None = None
    #: What was actually spent once the leg resolved. ``None`` while held.
    committed_usd: Decimal | None = None
    #: Set when the commit exceeded the hold. A buy should not be able to do this,
    #: so a non-zero value here is a modelling defect worth surfacing, not absorbing.
    overrun_usd: Decimal = ZERO

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "venue": self.venue.value,
            "amount_usd": str(quantize_usd(self.amount_usd)),
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "committed_usd": (
                str(quantize_usd(self.committed_usd))
                if self.committed_usd is not None
                else None
            ),
            "overrun_usd": str(quantize_usd(self.overrun_usd)),
        }


@dataclass(frozen=True, slots=True)
class ReservationOutcome:
    approved: bool
    refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.approved


class FundingLedger:
    """Available cash per venue, minus everything currently held against it."""

    def __init__(self) -> None:
        self._funds: dict[Platform, VenueFunds] = {}
        self._reservations: dict[str, Reservation] = {}
        #: Commits a later balance read superseded. Kept, not deleted: the run's
        #: journal still has to be able to show where the money went.
        self._retired: dict[str, Reservation] = {}

    # ----------------------------------------------------------------- balance
    def declare(
        self,
        venue: Platform,
        available_usd: Decimal | None,
        *,
        source: BalanceSource,
        as_of: datetime | None = None,
    ) -> None:
        """Record what a venue says is spendable.

        Re-declaring replaces the figure. What happens to existing reservations
        depends on which of two different things they represent, and conflating them
        costs capacity on every refresh:

        **Held** reservations are money promised to a candidate that has not yet
        filled. The venue does not know about them, so they are *not* reflected in a
        fresh balance read and must keep being subtracted. They survive.

        **Committed** reservations are money already spent. A fresh read from the
        venue itself has that spend baked into the number it returns, so continuing
        to subtract it would deduct the same dollars twice - and the error compounds,
        because every subsequent refresh retires nothing and the ledger's idea of
        spendable capacity drifts steadily below the truth. So a ``VENUE_API``
        declaration retires the commits it supersedes.

        An ``OPERATOR_DECLARED`` figure is a notional bankroll, not a reading of the
        account, so it retires nothing: the operator is saying how much to risk, not
        reporting what is left.
        """
        if available_usd is not None and available_usd < ZERO:
            raise ValueError(f"{venue.value}: available balance cannot be negative")
        stamp = as_of or utcnow()
        if source is BalanceSource.VENUE_API:
            self._retire_commits_reflected_in(venue, stamp)
        self._funds[venue] = VenueFunds(
            venue=venue,
            available_usd=available_usd,
            source=source,
            as_of=stamp,
        )

    def _retire_commits_reflected_in(self, venue: Platform, as_of: datetime) -> None:
        """Drop committed reservations the venue's own figure already accounts for.

        Only those settled at or before the read. A commit that landed *after* the
        balance was sampled is not in that number yet and must keep being subtracted.
        """
        for ref, reservation in list(self._reservations.items()):
            if reservation.venue != venue:
                continue
            if reservation.state is not ReservationState.COMMITTED:
                continue
            if reservation.settled_at is not None and reservation.settled_at <= as_of:
                self._retired[ref] = self._reservations.pop(ref)

    def funds(self, venue: Platform) -> VenueFunds:
        return self._funds.get(
            venue,
            VenueFunds(
                venue=venue,
                available_usd=None,
                source=BalanceSource.UNKNOWN,
                as_of=utcnow(),
            ),
        )

    def held_usd(self, venue: Platform) -> Decimal:
        return sum(
            (
                r.amount_usd
                for r in self._reservations.values()
                if r.venue == venue and r.state is ReservationState.HELD
            ),
            ZERO,
        )

    def committed_usd(self, venue: Platform) -> Decimal:
        return sum(
            (
                (r.committed_usd or ZERO)
                for r in self._reservations.values()
                if r.venue == venue and r.state is ReservationState.COMMITTED
            ),
            ZERO,
        )

    def spendable_usd(self, venue: Platform) -> Decimal | None:
        """What a new candidate could still reserve. ``None`` when unknown."""
        funds = self.funds(venue)
        if not funds.is_known:
            return None
        assert funds.available_usd is not None
        return funds.available_usd - self.held_usd(venue) - self.committed_usd(venue)

    # ------------------------------------------------------------ reservations
    def reserve_all(
        self, requests: list[tuple[str, Platform, Decimal]]
    ) -> ReservationOutcome:
        """Take every reservation or none of them.

        ``requests`` is ``(ref, venue, amount)``. Amounts for the same venue add up
        within one call, so a pair with two legs on one venue is checked against the
        combined figure rather than twice against the same balance.
        """
        if not requests:
            return ReservationOutcome(approved=False, reasons=("no legs to reserve",))

        reasons: list[str] = []
        for ref, _venue, amount in requests:
            if ref in self._reservations:
                reasons.append(f"{ref}: a reservation with this ref already exists")
            if amount <= ZERO:
                reasons.append(f"{ref}: reservation amount {amount} is not positive")

        needed: dict[Platform, Decimal] = {}
        for _ref, venue, amount in requests:
            needed[venue] = needed.get(venue, ZERO) + amount

        for venue, amount in needed.items():
            funds = self.funds(venue)
            if not funds.is_known:
                reasons.append(
                    f"{venue.value}: balance is {funds.source.value}; refusing to "
                    "reserve against a figure that was never read"
                )
                continue
            spendable = self.spendable_usd(venue)
            assert spendable is not None
            if spendable < amount:
                reasons.append(
                    f"{venue.value}: needs ${quantize_usd(amount)} but only "
                    f"${quantize_usd(spendable)} is unreserved"
                )

        if reasons:
            return ReservationOutcome(approved=False, reasons=tuple(reasons))

        now = utcnow()
        refs: list[str] = []
        for ref, venue, amount in requests:
            self._reservations[ref] = Reservation(
                ref=ref, venue=venue, amount_usd=amount, created_at=now
            )
            refs.append(ref)
        return ReservationOutcome(approved=True, refs=tuple(refs))

    def commit(self, ref: str, actual_usd: Decimal) -> Reservation:
        """Convert a hold into money actually spent.

        A commit larger than the hold is recorded as an overrun rather than silently
        widened. It should be impossible for a limit buy, so seeing one means the
        cost model and the fill disagree, and that is worth failing a reconciliation
        over.
        """
        reservation = self._require(ref)
        if reservation.state is not ReservationState.HELD:
            raise ValueError(
                f"{ref}: cannot commit a reservation that is {reservation.state.value}"
            )
        if actual_usd < ZERO:
            raise ValueError(f"{ref}: committed amount cannot be negative")
        reservation.state = ReservationState.COMMITTED
        reservation.committed_usd = actual_usd
        reservation.overrun_usd = max(actual_usd - reservation.amount_usd, ZERO)
        reservation.settled_at = utcnow()
        return reservation

    def release(self, ref: str) -> Reservation:
        """Give a hold back, because the candidate was abandoned or the leg did not fill."""
        reservation = self._require(ref)
        if reservation.state is ReservationState.COMMITTED:
            raise ValueError(f"{ref}: committed money cannot be released")
        reservation.state = ReservationState.RELEASED
        reservation.committed_usd = ZERO
        reservation.settled_at = utcnow()
        return reservation

    def reservation(self, ref: str) -> Reservation | None:
        return self._reservations.get(ref) or self._retired.get(ref)

    def _require(self, ref: str) -> Reservation:
        reservation = self._reservations.get(ref)
        if reservation is None:
            raise KeyError(f"no reservation named {ref}")
        return reservation

    # ------------------------------------------------------------------ report
    def snapshot(self) -> dict[str, object]:
        """Everything an auditor needs to check the arithmetic by hand."""
        venues = sorted(
            {*self._funds, *(r.venue for r in self._reservations.values())},
            key=lambda v: v.value,
        )
        return {
            "venues": {
                venue.value: {
                    "declared_available_usd": (
                        str(quantize_usd(self.funds(venue).available_usd))
                        if self.funds(venue).available_usd is not None
                        else None
                    ),
                    "balance_source": self.funds(venue).source.value,
                    "held_usd": str(quantize_usd(self.held_usd(venue))),
                    "committed_usd": str(quantize_usd(self.committed_usd(venue))),
                    "spendable_usd": (
                        str(quantize_usd(self.spendable_usd(venue)))
                        if self.spendable_usd(venue) is not None
                        else None
                    ),
                }
                for venue in venues
            },
            "reservations": [r.as_dict() for r in self._reservations.values()],
            "retired_reservations": [r.as_dict() for r in self._retired.values()],
        }
