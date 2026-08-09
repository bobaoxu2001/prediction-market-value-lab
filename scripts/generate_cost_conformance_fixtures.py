#!/usr/bin/env python
"""Emit conformance fixtures for the TypeScript cost port.

The extension carries a second implementation of the fee maths, in TypeScript,
because it must work against the venue's live book rather than our published
snapshot. Two implementations are two numbers that can disagree, and here they
would disagree in front of somebody about to place an order.

This script is the bridge. It calls the **real** functions in
``pmvl_markets.pricing`` - never a restatement of them - over a case list chosen
to sit on the boundaries where the two could plausibly diverge, and writes the
answers to a JSON file the TypeScript suite replays.

Regenerate with::

    python scripts/generate_cost_conformance_fixtures.py

and commit the result. A diff in the fixture file is a change in what the product
charges, and should be read as carefully as a change to the code that produced it.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from typing import Any

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import D
from pmvl_shared.schemas import BookLevel, OrderBook

from pmvl_markets.pricing.fees import (
    fee_per_contract,
    fee_rounding_cost,
    taker_fee,
)
from pmvl_markets.pricing.orderbook import walk_book

OUT = pathlib.Path("apps/extension/fixtures/cost-conformance.json")

#: Sizes chosen for where they break things, not for being round.
#:
#: 1 is where Kalshi's whole-order cent ceiling is at its most brutal. 3 and 7 are
#: non-round sizes whose division leaves a repeating decimal, which is where a
#: fixed-precision port drifts from Python's 28-digit context if it is going to.
#: 0.5 exercises fractional contracts, which Kalshi's `_fp` fields permit.
SIZES = ["0.5", "1", "2", "3", "5", "7", "10", "25", "100", "137", "1000"]

#: Prices spanning the quadratic fee curve, including both extremes where the fee
#: collapses toward zero and the 50c peak where it is largest.
PRICES = [
    "0.0010", "0.0100", "0.0200", "0.0300", "0.0500",
    "0.1000", "0.2500", "0.4900", "0.5000", "0.5100",
    "0.7500", "0.9000", "0.9700", "0.9900", "0.9990",
]

#: Kalshi's published taker rate, a maker-fee series, and a per-series multiplier.
KALSHI_RATES = ["0.07", "0.0175", "0.035"]
#: Polymarket rates are read per market; these bracket the observed range.
POLY_RATES = ["0.07", "0.02", "0.10"]


def emit_fee_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for venue, platform, rates in (
        ("kalshi", Platform.KALSHI, KALSHI_RATES),
        ("polymarket", Platform.POLYMARKET, POLY_RATES),
    ):
        for rate in rates:
            for size in SIZES:
                for price in PRICES:
                    contracts, p, r = D(size), D(price), D(rate)
                    cases.append(
                        {
                            "venue": venue,
                            "contracts": size,
                            "price": price,
                            "rate": rate,
                            "fee_type": "",
                            "total_fee": str(
                                taker_fee(platform, contracts, p, rate=r, fee_type="")
                            ),
                            "fee_per_contract": str(
                                fee_per_contract(
                                    platform, contracts, p, rate=r, fee_type=""
                                )
                            ),
                            "fee_rounding": str(
                                fee_rounding_cost(
                                    platform, contracts, p, rate=r, fee_type=""
                                )
                            ),
                        }
                    )

    # Flat-fee Kalshi series take a different branch entirely.
    for size in SIZES:
        for price in ("0.0100", "0.5000", "0.9900"):
            contracts, p, r = D(size), D(price), D("0.01")
            cases.append(
                {
                    "venue": "kalshi",
                    "contracts": size,
                    "price": price,
                    "rate": "0.01",
                    "fee_type": "flat",
                    "total_fee": str(
                        taker_fee(Platform.KALSHI, contracts, p, rate=r, fee_type="flat")
                    ),
                    "fee_per_contract": str(
                        fee_per_contract(
                            Platform.KALSHI, contracts, p, rate=r, fee_type="flat"
                        )
                    ),
                    "fee_rounding": str(
                        fee_rounding_cost(
                            Platform.KALSHI, contracts, p, rate=r, fee_type="flat"
                        )
                    ),
                }
            )
    return cases


#: Ladders that exercise a single deep level, a thin multi-level walk, and a book
#: that runs out before the requested size.
BOOKS: dict[str, list[tuple[str, str]]] = {
    "single_deep": [("0.0100", "100000")],
    "three_levels": [("0.0100", "10"), ("0.0200", "25"), ("0.0400", "500")],
    "thin": [("0.3300", "3"), ("0.3400", "4")],
    "fine_tick": [("0.1230", "17"), ("0.1240", "33"), ("0.1250", "1000")],
}


def emit_walk_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for name, levels in BOOKS.items():
        book_levels = [
            BookLevel(price=D(price), size=D(size)) for price, size in levels
        ]
        for size in SIZES:
            quote = walk_book(book_levels, D(size))
            cases.append(
                {
                    "book": name,
                    "levels": [{"price": p, "size": s} for p, s in levels],
                    "size": size,
                    "result": None
                    if quote is None
                    else {
                        "filled_size": str(quote.filled_size),
                        "average_price": str(quote.average_price),
                        "levels_consumed": quote.levels_consumed,
                        "fully_filled": quote.fully_filled,
                    },
                }
            )
    return cases


def main() -> None:
    payload = {
        "_comment": (
            "Generated by scripts/generate_cost_conformance_fixtures.py from the "
            "real pmvl_markets.pricing functions. Do not hand-edit: regenerate."
        ),
        "fee_cases": emit_fee_cases(),
        "walk_cases": emit_walk_cases(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1) + "\n")
    print(
        f"wrote {OUT} "
        f"({len(payload['fee_cases'])} fee cases, {len(payload['walk_cases'])} walks)"
    )


if __name__ == "__main__":
    main()
