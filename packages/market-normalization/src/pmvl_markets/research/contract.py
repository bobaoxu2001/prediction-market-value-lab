"""What a weather contract actually settles on.

A ticker suffix is not a specification. ``T90`` and ``B89.5`` look similar and
mean different things, and "90" appears in both a ``greater``-strike contract
that pays above 90 and a ``between`` contract covering 89-90. Guessing that they
are interchangeable is the single easiest way to produce a research dataset that
scores a model against the wrong question.

So the bucket is read from the venue's own structured fields - ``strike_type``,
``floor_strike``, ``cap_strike`` - and a contract whose bounds cannot be
established is refused rather than approximated.

Inclusivity is the part that silently corrupts results. Kalshi's high-temperature
contracts settle on an integer Fahrenheit reading, and the published bucket
"87 to 88" means 87 or 88, both inclusive - not the half-open interval a naive
reading produces. A half-open reading moves probability mass across every bucket
boundary in the ladder, in the same direction, for every contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


class BucketKind(str, Enum):
    """How the contract's payout region is bounded.

    All three are stored as INCLUSIVE integer bounds after conversion. The
    venue's threshold contracts are worded strictly - "greater than 96", "less
    than 81" - and are converted on the way in rather than carried as a
    strictness flag, so that no downstream consumer can forget to apply it.
    """

    #: floor <= T <= cap, both inclusive.
    BETWEEN = "between"
    #: T >= lower (already converted from a strict "greater than").
    AT_OR_ABOVE = "at_or_above"
    #: T <= upper (already converted from a strict "less than").
    AT_OR_BELOW = "at_or_below"


@dataclass(frozen=True)
class ContractSpec:
    """The settlement region of one contract, in the settlement's own units."""

    kind: BucketKind
    #: Inclusive lower bound in degrees F. None for AT_OR_BELOW.
    lower: Decimal | None
    #: Inclusive upper bound in degrees F. None for AT_OR_ABOVE.
    upper: Decimal | None

    def contains(self, temperature_f: Decimal | float | int) -> bool:
        """Does this observed settlement value pay YES?

        Both bounds inclusive, deliberately. See the module docstring.
        """
        value = Decimal(str(temperature_f))
        if self.lower is not None and value < self.lower:
            return False
        if self.upper is not None and value > self.upper:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "lower_inclusive": str(self.lower) if self.lower is not None else None,
            "upper_inclusive": str(self.upper) if self.upper is not None else None,
        }


#: Settlement is a whole-degree Fahrenheit reading from the NWS Climatological
#: Report, so the smallest gap between a strict and an inclusive bound is 1.
#: Named rather than inlined because it is an assumption about the settlement
#: source, not an arithmetic constant - a source reporting tenths would need a
#: different value and a different continuity correction downstream.
_INTEGER_STEP = Decimal(1)


class ContractParseError(ValueError):
    """The contract's settlement region could not be established."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_contract(
    *,
    strike_type: str | None,
    floor_strike: Any,
    cap_strike: Any,
) -> ContractSpec:
    """Read the payout region from the venue's structured strike fields.

    Refuses rather than guesses. A ``between`` contract missing a bound is not a
    one-sided contract - it is a contract this parser does not understand, and
    treating it as one-sided would silently extend its payout region to infinity.
    """
    kind_raw = (strike_type or "").strip().lower()
    lower, upper = _decimal(floor_strike), _decimal(cap_strike)

    if kind_raw == "between":
        if lower is None or upper is None:
            raise ContractParseError(
                f"between-contract missing a bound: floor={floor_strike!r} "
                f"cap={cap_strike!r}"
            )
        if lower > upper:
            raise ContractParseError(f"inverted bounds: {lower} > {upper}")
        return ContractSpec(BucketKind.BETWEEN, lower, upper)

    if kind_raw == "greater":
        if lower is None:
            raise ContractParseError("greater-contract has no floor_strike")
        # STRICT. The rule reads "is greater than 96", so 96 does NOT pay; the
        # first paying integer reading is 97.
        #
        # Treating floor_strike as inclusive is a one-degree error on every
        # threshold contract in the universe, always in the direction that
        # overstates YES. It is invisible in the ticker (T96), invisible in the
        # structured field (floor_strike=96), and visible only in the rule text.
        return ContractSpec(BucketKind.AT_OR_ABOVE, lower + _INTEGER_STEP, None)

    if kind_raw == "less":
        if upper is None:
            raise ContractParseError("less-contract has no cap_strike")
        # STRICT, symmetrically: "is less than 81" means 80 or below.
        return ContractSpec(BucketKind.AT_OR_BELOW, None, upper - _INTEGER_STEP)

    raise ContractParseError(f"unsupported strike_type {strike_type!r}")


def ladder_is_coherent(specs: list[ContractSpec]) -> tuple[bool, str]:
    """Do these contracts partition a range without overlap or gap?

    Used to validate a modelled probability set before it is compared to market
    prices. Two contracts that both pay at 88 make the modelled probabilities sum
    above one through no fault of the model.

    Returns a reason rather than a bare bool: "incoherent" is not actionable, and
    an overlap and a gap need different fixes.
    """
    bounded = [s for s in specs if s.kind is BucketKind.BETWEEN]
    if len(bounded) < 2:
        return True, "fewer than two bounded buckets; nothing to partition"
    ordered = sorted(bounded, key=lambda s: s.lower)  # type: ignore[arg-type,return-value]
    for left, right in zip(ordered, ordered[1:]):
        assert left.upper is not None and right.lower is not None
        if right.lower <= left.upper:
            return False, f"overlap: [{left.lower},{left.upper}] and [{right.lower},{right.upper}]"
        # Integer settlement: the next bucket must start at upper + 1.
        if right.lower - left.upper > 1:
            return False, f"gap between {left.upper} and {right.lower}"
    return True, "contiguous and non-overlapping"
