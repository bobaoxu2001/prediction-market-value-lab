"""Reconstructing a market record as it stood at a past instant.

The retrodiction harness replays models against markets that have already settled.
Those markets are read out of the database *now*, which means the row it starts from
is the post-resolution row: ``status`` is ``settled``, ``result`` says which way it
went, and every quote field holds the terminal price.

Handing that record to a model would not be a subtle leak. ``result`` is the answer.
A model that reads no prices at all would still score a perfect Brier if anything
downstream of it consulted that field, and the resulting evaluation would be
worthless in a way that looks exactly like success.

So the record is rewound before any model sees it: outcome-revealing and
state-revealing fields are cleared, and only the fields that were already true at
``as_of`` survive. The list of what survives is deliberately short and explicit -
a whitelist, so a field added to :class:`NormalizedMarket` later is dropped by
default rather than silently forwarded into a historical evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pmvl_shared.enums import MarketStatus
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import ensure_utc

#: Fields carried into the rewound record.
#:
#: Every one of these is either immutable for the life of the contract (the
#: question text, the strike, the settlement rule, the token ids) or is a
#: scheduling fact fixed when the market opened (the times). None of them can
#: encode the outcome.
#:
#: ``fee_rate``/``maker_fee_rate``/``tick_size`` are the weakest members of the
#: list: a venue can revise a fee schedule, so these are *today's* values applied
#: to a past instant. They are kept because no probability model reads them - they
#: matter to the cost engine, which the harness does not run - and because
#: dropping them would leave the record failing its own validation.
_PRESERVED_FIELDS: frozenset[str] = frozenset(
    {
        "platform",
        "platform_market_id",
        "platform_event_id",
        "series_ticker",
        "title",
        "subtitle",
        "normalized_title",
        "description",
        "category",
        "outcomes",
        "yes_token_id",
        "no_token_id",
        "condition_id",
        "open_time",
        "close_time",
        "event_occurrence_time",
        "expected_resolution_time",
        "source_timezone",
        "settlement_source",
        "settlement_rules_raw",
        "settlement_rules_normalized",
        "resolution_hash",
        "market_type",
        "strike_type",
        "floor_strike",
        "cap_strike",
        "tick_size",
        "price_level_structure",
        "min_order_size",
        "fee_rate",
        "maker_fee_rate",
        "fee_type",
        "negative_risk",
        "provenance",
    }
)

#: Cleared because they state the outcome, directly or by implication.
#:
#: Named separately from "everything not preserved" purely so the reason is on the
#: record. ``actual_settlement_time`` is here because a market with a settlement
#: time has, self-evidently, settled.
_OUTCOME_REVEALING_FIELDS: frozenset[str] = frozenset(
    {"result", "actual_settlement_time"}
)

#: Set to a reconstructed value rather than dropped, and so reported separately
#: from the cleared fields. The distinction matters: a cleared field is one the
#: harness is admitting it does not know, while a reconstructed one is a claim
#: the harness is making about the past, and a reader should be able to see which
#: is which without reading this file.
_RECONSTRUCTED_FIELDS: frozenset[str] = frozenset({"status", "accepting_orders"})


class RewindError(ValueError):
    """Raised when a market cannot be honestly rewound to the requested instant."""


@dataclass(frozen=True)
class RewoundMarket:
    """A market record restricted to what was knowable at ``as_of``."""

    market: NormalizedMarket
    as_of: datetime
    #: Fields dropped because their value at ``as_of`` is not known. Recorded so a
    #: report can show the defence ran and on what.
    cleared_fields: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "cleared_fields": list(self.cleared_fields),
            "reconstructed_fields": sorted(_RECONSTRUCTED_FIELDS),
            "preserved_field_count": len(_PRESERVED_FIELDS),
        }


def rewind_market(market: NormalizedMarket, *, as_of: datetime) -> RewoundMarket:
    """Strip a settled market back to the fields that were true at ``as_of``.

    Raises :class:`RewindError` when the instant is not actually in the market's
    trading life - before it opened, or at/after it closed. Both are refusals rather
    than clamps: an evaluation instant outside the window is a sampling bug, and
    silently moving it would hide the bug and produce a number anyway.
    """
    # ensure_utc on every side of every comparison. SQLite drops tzinfo on
    # round-trip, so a market loaded from the database has naive datetimes while
    # `as_of` is always aware, and comparing them raises rather than returning a
    # wrong answer. That is the good failure mode, and it is still a failure: it
    # took out every evaluation on the first live run.
    as_of = ensure_utc(as_of)
    open_time = ensure_utc(market.open_time)
    close = ensure_utc(market.close_time or market.expected_resolution_time)

    if open_time is not None and as_of < open_time:
        raise RewindError(
            f"as_of {as_of.isoformat()} precedes the market's open at "
            f"{open_time.isoformat()}"
        )
    if close is not None and as_of >= close:
        raise RewindError(
            f"as_of {as_of.isoformat()} is at or after the market's close at "
            f"{close.isoformat()}; there is no forecast to make"
        )

    payload = market.model_dump()
    cleared: list[str] = []
    rewound: dict[str, object] = {}

    for name, value in payload.items():
        if name in _PRESERVED_FIELDS:
            rewound[name] = value
            continue
        # Everything else is dropped back to the field's own default, which is the
        # honest representation of "not observed at this instant" for the quote
        # fields and "not yet known" for the outcome fields.
        if value is not None and name not in _RECONSTRUCTED_FIELDS:
            cleared.append(name)

    # The one field that is not simply dropped. At `as_of` the market was, by the
    # checks above, inside its trading window - so `open` is the reconstruction,
    # and leaving it `unknown` would make the record look degraded rather than
    # historical.
    rewound["status"] = MarketStatus.OPEN
    rewound["accepting_orders"] = True

    return RewoundMarket(
        market=NormalizedMarket(**rewound),
        as_of=as_of,
        cleared_fields=tuple(sorted(cleared)),
    )


def assert_no_outcome_leak(market: NormalizedMarket) -> None:
    """Fail loudly if a rewound record still carries its own answer.

    Called by the harness on every rewound market. It is redundant with
    :func:`rewind_market` by construction, and that is the point: this is the
    assertion that survives someone adding a field to the preserved list without
    thinking about what it reveals.
    """
    for name in _OUTCOME_REVEALING_FIELDS:
        value = getattr(market, name, None)
        if value:
            raise RewindError(
                f"rewound market still carries outcome-revealing field {name!r}="
                f"{value!r}; refusing to run a historical evaluation on it"
            )
    if market.status is MarketStatus.SETTLED:
        raise RewindError("rewound market still has status=settled")
