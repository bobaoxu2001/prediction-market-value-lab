"""Economic feasibility preflight, on real captured order books only.

    Is this market large enough that a successful alpha would be worth having?

That question can be answered today, with data already on disk, before a single
point-in-time observation has been collected - and it should be, because it
determines whether the months of collection that follow are worth spending.

What this is not
----------------
It is not an alpha estimate and cannot become one. ``alpha`` is a hypothetical
the caller supplies; every number below is conditional on it. Nothing here reads
a settlement, a forecast or a model probability. A large capacity number means
the venue could absorb size, not that there is anything to trade.

Refusals
--------
The script refuses demo and fixture provenance outright rather than filtering
it. A capacity figure computed partly from seeded rows is not a weaker result,
it is a different result wearing this one's name.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "packages/shared/src"),
    str(ROOT / "packages/market-normalization/src"),
]

from pmvl_shared.enums import Platform  # noqa: E402
from pmvl_shared.money import ZERO, D, quantize_usd  # noqa: E402
from pmvl_shared.timeutil import parse_ts  # noqa: E402

from pmvl_markets.alpha.capacity import (  # noqa: E402
    DEFAULT_ALPHAS,
    DEFAULT_SIZES,
    BookState,
    CapacityObservation,
    Economics,
    levels_from_rows,
    liquidity_bucket,
    max_executable_size,
    minimum_viable_alpha,
    price_bucket,
    price_order,
    segment_capacity,
    spread_bucket,
    summarise,
    ttl_bucket,
)


class SyntheticDataError(RuntimeError):
    """Non-live provenance reached a capacity calculation."""


#: Kalshi weather series ticker -> the city the contract settles on.
#:
#: The mapping is for *reporting only*. It is deliberately not used to infer a
#: settlement station for research: the station is named in the settlement rule
#: text and belongs in a field of its own, not in a lookup keyed on a ticker
#: prefix that the venue may reuse.
SERIES_CITY = {
    "KXHIGHNY": "New York (Central Park)",
    "KXHIGHLAX": "Los Angeles (LAX)",
    "KXHIGHCHI": "Chicago (Midway)",
    "KXHIGHMIA": "Miami",
    "KXHIGHAUS": "Austin (Camp Mabry)",
    "KXHIGHDEN": "Denver",
    "KXHIGHPHIL": "Philadelphia",
}


def load_observations(
    con: sqlite3.Connection, *, category: str | None, require_two_sided: bool
) -> list[CapacityObservation]:
    """Read every captured book and price it. Live provenance only."""
    where = ["o.provenance = 'live'"]
    params: list[Any] = []
    if category:
        where.append("m.category = ?")
        params.append(category)

    rows = con.execute(
        f"""
        SELECT o.id, o.observed_at, m.platform, m.platform_market_id,
               COALESCE(m.series_ticker, ''), m.expected_resolution_time,
               m.fee_rate, m.fee_type, m.provenance
          FROM orderbook_snapshots o
          JOIN markets m ON m.id = o.market_id
         WHERE {' AND '.join(where)}
         ORDER BY o.id
        """,
        params,
    ).fetchall()

    observations: list[CapacityObservation] = []
    for (
        snapshot_id, observed_at, platform, market_id, series,
        resolution, fee_rate, fee_type, market_provenance,
    ) in rows:
        if market_provenance != "live":
            raise SyntheticDataError(
                f"market {market_id} has provenance {market_provenance!r}; "
                "capacity may not be computed from non-live rows"
            )

        for side in ("yes", "no"):
            asks = levels_from_rows(
                con.execute(
                    "SELECT price, size FROM orderbook_levels "
                    "WHERE snapshot_id=? AND side=? AND is_ask=1 ORDER BY level_index",
                    (snapshot_id, side),
                ).fetchall()
            )
            bids = levels_from_rows(
                con.execute(
                    "SELECT price, size FROM orderbook_levels "
                    "WHERE snapshot_id=? AND side=? AND is_ask=0 ORDER BY level_index",
                    (snapshot_id, side),
                ).fetchall()
            )
            book = BookState(asks=asks, bids=bids)
            if require_two_sided and not book.two_sided:
                continue
            if book.mid is None:
                continue

            captured = parse_ts(observed_at)
            settles = parse_ts(resolution)
            hours = (
                (settles - captured).total_seconds() / 3600.0
                if captured and settles
                else None
            )

            obs = CapacityObservation(
                snapshot_id=snapshot_id,
                market_key=f"{platform}:{market_id}",
                series=SERIES_CITY.get(series, series or "unknown"),
                side=side,
                observed_at=observed_at,
                price_bucket=price_bucket(book.best_ask),
                spread_bucket=spread_bucket(book.spread),
                ttl_bucket=ttl_bucket(hours),
                hour_utc=captured.hour if captured else -1,
                liquidity_bucket=liquidity_bucket(book.ask_depth_usd),
                best_ask=book.best_ask,
                spread=book.spread,
                ask_depth_contracts=book.ask_depth_contracts,
                ask_depth_usd=book.ask_depth_usd,
                bid_depth_contracts=book.bid_depth_contracts,
            )

            venue = Platform(platform)
            rate = D(str(fee_rate))
            for alpha in DEFAULT_ALPHAS:
                for size in DEFAULT_SIZES:
                    for economics, store in (
                        (Economics.HOLD_TO_SETTLEMENT, obs.points),
                        (Economics.ROUND_TRIP, obs.round_trip_points),
                    ):
                        point = price_order(
                            book, size=size, alpha=alpha, platform=venue,
                            fee_rate=rate, fee_type=fee_type or "",
                            economics=economics,
                        )
                        if point is not None:
                            store[(str(alpha), str(size))] = point

                obs.max_positive[str(alpha)] = max_executable_size(
                    book, alpha=alpha, platform=venue, fee_rate=rate,
                    fee_type=fee_type or "", min_net_edge=ZERO,
                )
                obs.max_one_cent[str(alpha)] = max_executable_size(
                    book, alpha=alpha, platform=venue, fee_rate=rate,
                    fee_type=fee_type or "", min_net_edge=Decimal("0.01"),
                )
            observations.append(obs)
    return observations


def cost_floor_report(
    con: sqlite3.Connection, observations: list[CapacityObservation]
) -> dict[str, Any]:
    """What an edge must exceed before any size is worth trading."""
    floors_hold: list[Decimal] = []
    floors_round: list[Decimal] = []
    for obs in observations:
        hold = obs.points.get((str(DEFAULT_ALPHAS[0]), "100"))
        rt = obs.round_trip_points.get((str(DEFAULT_ALPHAS[0]), "100"))
        if hold is not None:
            floors_hold.append(hold.total_cost)
        if rt is not None:
            floors_round.append(rt.total_cost)
    return {
        "hold_to_settlement_at_100": summarise(floors_hold),
        "round_trip_at_100": summarise(floors_round),
    }


def build_report(
    observations: list[CapacityObservation], con: sqlite3.Connection
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "disclaimer": (
            "CAPACITY ONLY. Every figure is conditional on a HYPOTHETICAL gross "
            "alpha supplied as an input. This is not evidence of alpha and may "
            "not be cited as such."
        ),
        "observations": len(observations),
        "distinct_snapshots": len({o.snapshot_id for o in observations}),
        "distinct_markets": len({o.market_key for o in observations}),
        "cost_floor": cost_floor_report(con, observations),
        "by_alpha": {},
        "segments": {},
        "size_ladder": {},
    }

    for alpha in DEFAULT_ALPHAS:
        key = str(alpha)
        contracts = [o.max_positive.get(key, (ZERO, ZERO))[0] for o in observations]
        dollars = [o.max_positive.get(key, (ZERO, ZERO))[1] for o in observations]
        c1 = [o.max_one_cent.get(key, (ZERO, ZERO))[0] for o in observations]
        d1 = [o.max_one_cent.get(key, (ZERO, ZERO))[1] for o in observations]
        tradeable = sum(1 for v in contracts if v > 0)

        pnl, deployed = [], []
        for obs in observations:
            size, dollar = obs.max_positive.get(key, (ZERO, ZERO))
            if size <= 0:
                continue
            point = obs.points.get((key, "100")) or next(
                (p for (a, _), p in obs.points.items() if a == key), None
            )
            if point is None:
                continue
            pnl.append(quantize_usd(point.net_edge * size))
            deployed.append(dollar)

        report["by_alpha"][f"{int(alpha * 100)}c"] = {
            "tradeable_books": tradeable,
            "tradeable_share": f"{tradeable / max(1, len(observations)):.1%}",
            "max_contracts_positive_edge": summarise(contracts),
            "max_dollars_positive_edge": summarise(dollars),
            "max_contracts_1c_edge": summarise(c1),
            "max_dollars_1c_edge": summarise(d1),
            # Conditional on the book being tradeable at all. Pooling the
            # untradeable books as zeros would mix "no capacity" into a
            # distribution that is meant to describe capacity when it exists.
            "pnl_per_opportunity_given_tradeable": summarise(pnl),
            "capital_per_opportunity_given_tradeable": summarise(deployed),
        }

    for key in (
        "series", "price_bucket", "spread_bucket", "ttl_bucket", "liquidity_bucket"
    ):
        report["segments"][key] = {
            f"{int(alpha * 100)}c": segment_capacity(
                observations, alpha=alpha, key=key
            )
            for alpha in (Decimal("0.02"), Decimal("0.03"), Decimal("0.05"))
        }

    # Round-trip: the convergence strategy's economics. Reported separately
    # because it crosses the spread twice and sells into the bid, which is the
    # thin side of a prediction-market book. Collapsing the two would let a
    # hold-to-settlement capacity stand in for a convergence one.
    round_trip: dict[str, Any] = {}
    for alpha in DEFAULT_ALPHAS:
        edges = [
            o.round_trip_points[(str(alpha), "100")].net_edge
            for o in observations
            if (str(alpha), "100") in o.round_trip_points
        ]
        positive = sum(1 for e in edges if e > 0)
        round_trip[f"{int(alpha * 100)}c"] = {
            "n": len(edges),
            "positive_share": f"{positive / max(1, len(edges)):.1%}",
            "net_edge_at_100": summarise(edges),
        }
    report["round_trip_at_100"] = round_trip

    ladder: dict[str, Any] = {}
    for alpha in DEFAULT_ALPHAS:
        rows = {}
        for size in DEFAULT_SIZES:
            edges = [
                o.points[(str(alpha), str(size))].net_edge
                for o in observations
                if (str(alpha), str(size)) in o.points
            ]
            positive = sum(1 for e in edges if e > 0)
            rows[str(int(size))] = {
                "n": len(edges),
                "positive_share": f"{positive / max(1, len(edges)):.1%}",
                "net_edge": summarise(edges),
            }
        ladder[f"{int(alpha * 100)}c"] = rows
    report["size_ladder"] = ladder
    return report


def scenarios(
    observations: list[CapacityObservation],
    *,
    cities: int,
    contracts_per_firing: Decimal,
) -> dict[str, Any]:
    """Transparent hypothetical-firing scenarios. NOT alpha estimates.

    The opportunity count is built from the universe, not from the sample:
    ``cities x firing_rate`` station-days fire per calendar day, and each firing
    station-day yields ``contracts_per_firing`` tradeable contracts - a revision
    moves the whole board but only the buckets near the threshold are worth
    trading.

    An earlier version divided a sample total by the number of capture days,
    which silently scaled the answer by how long the collector happened to have
    been running. That is a property of the collector, not of the market.
    """
    out: dict[str, Any] = {
        "disclaimer": (
            "SCENARIO ONLY. Firing rates and alpha levels are assumptions, not "
            "measurements. No alpha has been demonstrated."
        ),
        "cities_in_universe": cities,
        "tradeable_contracts_per_firing_station_day": str(contracts_per_firing),
        "cases": [],
    }
    for alpha in (Decimal("0.02"), Decimal("0.03"), Decimal("0.05")):
        key = str(alpha)
        sizes = [o.max_positive.get(key, (ZERO, ZERO))[0] for o in observations]
        tradeable = [s for s in sizes if s > 0]
        if not tradeable:
            continue
        ordered = sorted(tradeable)
        median_size = ordered[len(ordered) // 2]

        edges = [
            o.points[(key, "100")].net_edge
            for o in observations
            if (key, "100") in o.points and o.points[(key, "100")].net_edge > 0
        ]
        median_edge = sorted(edges)[len(edges) // 2] if edges else ZERO
        deployed = [
            o.max_positive.get(key, (ZERO, ZERO))[1] for o in observations
        ]
        median_deployed = sorted([d for d in deployed if d > 0])
        median_capital = (
            median_deployed[len(median_deployed) // 2] if median_deployed else ZERO
        )

        for rate in (Decimal("0.05"), Decimal("0.10"), Decimal("0.20")):
            opportunities = D(str(cities)) * rate * contracts_per_firing
            per_opp = quantize_usd(median_edge * median_size)
            out["cases"].append(
                {
                    "hypothetical_alpha": f"{int(alpha * 100)}c",
                    "firing_rate": f"{rate:.0%}",
                    "opportunities_per_day": str(quantize_usd(opportunities)),
                    "contracts_per_opportunity": str(median_size),
                    "net_dollars_per_opportunity": str(per_opp),
                    "net_dollars_per_day": str(quantize_usd(per_opp * opportunities)),
                    "capital_per_opportunity": str(median_capital),
                    "peak_capital_if_concurrent": str(
                        quantize_usd(median_capital * opportunities)
                    ),
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "data" / "pmvl.db"))
    parser.add_argument("--category", default="weather")
    parser.add_argument("--all-categories", action="store_true")
    parser.add_argument("--json", default=None)
    parser.add_argument(
        "--require-two-sided",
        action="store_true",
        default=True,
        help="only price books with both a bid and an ask (default: on)",
    )
    args = parser.parse_args(argv)

    path = Path(args.database)
    if not path.exists():
        print(f"missing {path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        category = None if args.all_categories else args.category
        observations = load_observations(
            con, category=category, require_two_sided=args.require_two_sided
        )
        if not observations:
            print("INSUFFICIENT DATA: no live two-sided books matched", file=sys.stderr)
            return 2

        cities = len({o.series for o in observations if o.series != "unknown"})
        report = build_report(observations, con)
        report["scenarios"] = scenarios(
            observations,
            cities=max(1, cities),
            contracts_per_firing=Decimal("1.5"),
        )
    finally:
        con.close()

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    _print(report)
    return 0


def _print(report: dict[str, Any]) -> None:
    print("=" * 78)
    print("CAPACITY PREFLIGHT - hypothetical alpha, real books, taker only")
    print("=" * 78)
    print(report["disclaimer"])
    print()
    print(
        f"observations={report['observations']}  "
        f"snapshots={report['distinct_snapshots']}  "
        f"markets={report['distinct_markets']}"
    )
    print()
    print("--- COST FLOOR (what an edge must exceed at 100 contracts) ---")
    for name, stats in report["cost_floor"].items():
        print(f"  {name:28s} n={stats['n']:>5s}  p25={stats['p25']}  "
              f"median={stats['median']}  p75={stats['p75']}  p90={stats['p90']}")
    print()
    print("--- MAX EXECUTABLE SIZE, positive net edge (hold to settlement) ---")
    print(f"  {'alpha':>6s} {'tradeable':>10s} {'contracts p25/med/p75/p90':>38s}"
          f" {'dollars med':>14s}")
    for alpha, block in report["by_alpha"].items():
        c = block["max_contracts_positive_edge"]
        d = block["max_dollars_positive_edge"]
        print(f"  {alpha:>6s} {block['tradeable_share']:>10s} "
              f"{c['p25']:>9s}/{c['median']:>9s}/{c['p75']:>9s}/{c['p90']:>9s} "
              f"{d['median']:>14s}")
    print()
    print("--- PnL PER OPPORTUNITY at max size (books that are tradeable) ---")
    for alpha, block in report["by_alpha"].items():
        p = block["pnl_per_opportunity_given_tradeable"]
        k = block["capital_per_opportunity_given_tradeable"]
        print(f"  {alpha:>6s}  pnl med={p['median']:>10s} p90={p['p90']:>10s}   "
              f"capital med={k['median']:>12s}")
    print()
    print("--- NET EDGE BY ORDER SIZE (share of books with positive net edge) ---")
    sizes = [str(int(s)) for s in DEFAULT_SIZES]
    print(f"  {'alpha':>6s} " + " ".join(f"{s:>8s}" for s in sizes))
    for alpha, rows in report["size_ladder"].items():
        print(f"  {alpha:>6s} " + " ".join(
            f"{rows[s]['positive_share']:>8s}" for s in sizes
        ))
    print()
    print("--- ROUND TRIP at 100 contracts (convergence: crosses twice) ---")
    print(f"  {'alpha':>6s} {'n':>5s} {'positive':>9s} {'net edge median':>16s}")
    for alpha, block in report["round_trip_at_100"].items():
        print(f"  {alpha:>6s} {block['n']:>5d} {block['positive_share']:>9s} "
              f"{block['net_edge_at_100']['median']:>16s}")
    print()
    print("--- CAPACITY BY CITY (hypothetical 3c alpha) ---")
    for name, block in report["segments"]["series"]["3c"].items():
        c = block["contracts"]
        print(f"  {name:26s} n={block['observations']:>4d} "
              f"tradeable={block['tradeable_share']:>6s} "
              f"contracts med={c['median']:>9s} p90={c['p90']:>9s}")
    print()
    print("--- SCENARIOS (assumptions, not measurements) ---")
    print(report["scenarios"]["disclaimer"])
    print(f"  {'alpha':>6s} {'fire':>6s} {'opps/day':>9s} {'ctr/opp':>10s} "
          f"{'$/opp':>10s} {'$/day':>10s} {'capital':>12s}")
    for case in report["scenarios"]["cases"]:
        print(f"  {case['hypothetical_alpha']:>6s} {case['firing_rate']:>6s} "
              f"{case['opportunities_per_day']:>9s} "
              f"{case['contracts_per_opportunity']:>10s} "
              f"{case['net_dollars_per_opportunity']:>10s} "
              f"{case['net_dollars_per_day']:>10s} "
              f"{case['peak_capital_if_concurrent']:>12s}")


if __name__ == "__main__":
    raise SystemExit(main())
