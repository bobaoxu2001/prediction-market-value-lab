"""``pmvl pair`` - cross-venue shadow scanning, attestation drafting and replay.

Every command here is read-only against the venues. ``scan --live`` fetches public
market data and order books over unauthenticated endpoints; nothing in this module
can authenticate, place an order or move funds, and the executor it drives refuses
any client that is not a declared simulation.

The output that matters is the coverage funnel. A run that finds nothing prints a
funnel showing *where* the pairs went, which is the difference between "this market
is efficient" and "this matcher only compared 117 pairs".
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from pmvl_markets.execution.assessment import AccessStatus
from pmvl_markets.execution.funding import BalanceSource, FundingLedger
from pmvl_markets.execution.shadow_pairs import MarketBook, run_shadow_pairs
from pmvl_markets.matching.cancellation import AttestationStore, draft_attestation
from pmvl_shared.enums import MarketStatus, Platform
from pmvl_shared.timeutil import iso, utcnow

app = typer.Typer(
    add_completion=False,
    help=(
        "Cross-venue pair scanning in shadow mode. Read-only against both venues: "
        "no order is ever submitted, and no command here can authenticate."
    ),
)
console = Console()

DEFAULT_ATTESTATIONS = Path("data/rule_attestations.json")


def _attestations(path: Path | None) -> AttestationStore:
    return AttestationStore(path or DEFAULT_ATTESTATIONS)


def _funding(bankroll: Decimal | None) -> FundingLedger:
    """Declare a notional bankroll, or leave both venues unread so nothing trades.

    Leaving it unread is the default. A shadow run with no declared balance still
    produces the full funnel; it simply stops at the funding stage, which is the
    honest representation of an account nobody has checked.
    """
    ledger = FundingLedger()
    if bankroll is not None:
        for venue in (Platform.KALSHI, Platform.POLYMARKET):
            ledger.declare(venue, bankroll, source=BalanceSource.OPERATOR_DECLARED)
    return ledger


def _load_from_db(max_age_hours: int, limit: int | None) -> list[MarketBook]:
    from sqlalchemy import select

    from pmvl_markets.arbitrage.pipeline import (
        _market_from_row,
        latest_orderbook,
        orderbook_from_snapshot,
    )
    from pmvl_shared.db import session_scope
    from pmvl_shared.db.models import Market, OrderbookSnapshot

    cutoff = utcnow() - timedelta(hours=max_age_hours)
    out: list[MarketBook] = []
    with session_scope() as db:
        ids = db.scalars(
            select(OrderbookSnapshot.market_id)
            .where(OrderbookSnapshot.observed_at > cutoff)
            .distinct()
        ).all()
        rows = db.scalars(
            select(Market).where(
                Market.id.in_(ids), Market.status == MarketStatus.OPEN.value
            )
        ).all()
        for row in rows:
            market = _market_from_row(row)
            snapshot = latest_orderbook(db, row.id)
            if snapshot is None:
                continue
            book = orderbook_from_snapshot(db, snapshot, row)
            if book.is_empty:
                continue
            out.append(MarketBook(market=market, book=book))
            if limit and len(out) >= limit:
                break
    return out


async def _load_live(limit: int) -> list[MarketBook]:
    """Fetch fresh markets and books from both venues over public endpoints."""
    from pmvl_markets.providers.kalshi import KalshiProvider
    from pmvl_markets.providers.polymarket import PolymarketProvider

    out: list[MarketBook] = []
    kalshi = KalshiProvider()
    poly = PolymarketProvider()
    try:
        markets = await kalshi.list_active_markets(limit=limit)
        books = await kalshi.get_orderbooks(markets)
        out.extend(
            MarketBook(market=m, book=books[m.platform_market_id])
            for m in markets
            if m.platform_market_id in books
        )
        pmarkets = await poly.list_active_markets(limit=limit)
        pbooks = await poly.get_orderbooks(pmarkets)
        out.extend(
            MarketBook(market=m, book=pbooks[m.platform_market_id])
            for m in pmarkets
            if m.platform_market_id in pbooks
        )
    finally:
        await kalshi.aclose()
        await poly.aclose()
    return out


@app.command()
def scan(
    live: bool = typer.Option(
        False, "--live", help="Fetch fresh public books instead of reading the local DB"
    ),
    limit: int = typer.Option(300, help="Markets to load per venue"),
    max_age_hours: int = typer.Option(24, help="DB mode: how old a stored book may be"),
    max_quote_age_seconds: int = typer.Option(
        120, help="How old a book may be and still be priced"
    ),
    bankroll: float = typer.Option(
        None, help="Notional per-venue balance. Omitted means unread, which blocks."
    ),
    attestations: Path = typer.Option(None, help="Reviewed rule readings"),
    out: Path = typer.Option(None, help="Write the full run as JSON here"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Scan both venues for complementary pairs and shadow-execute what qualifies."""
    universe = (
        asyncio.run(_load_live(limit))
        if live
        else _load_from_db(max_age_hours, limit * 2)
    )
    run = run_shadow_pairs(
        universe,
        attestations=_attestations(attestations),
        funding=_funding(Decimal(str(bankroll)) if bankroll is not None else None),
        max_quote_age_seconds=max_quote_age_seconds,
        label=f"cross-venue shadow pass ({'live' if live else 'local DB'})",
        # Trading permission is not something this command can establish, so it is
        # left unverified and the pairs stop before submission.
        access={},
    )
    payload = run.as_dict()
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, default=str))
        console.print(f"[green]wrote {out}[/green]")
    if as_json:
        console.print_json(json.dumps(payload, default=str))
    else:
        console.print(run.summary())


@app.command("attest-draft")
def attest_draft(
    market_id: str = typer.Argument(..., help="Platform market id"),
    venue: str = typer.Option(..., help="kalshi or polymarket"),
    out: Path = typer.Option(None, help="Write the draft here instead of stdout"),
) -> None:
    """Emit a reviewer's worksheet for one market's cancellation terms.

    The draft is not an attestation: ``reviewed_by`` is blank, and loading it without
    filling that in raises. Reading the published rules and signing the draft is the
    step that lets a pair ever reach a guaranteed verdict.
    """
    from sqlalchemy import select

    from pmvl_markets.arbitrage.pipeline import _market_from_row
    from pmvl_shared.db import session_scope
    from pmvl_shared.db.models import Market

    platform = Platform(venue)
    with session_scope() as db:
        row = db.scalar(
            select(Market).where(
                Market.platform == platform.value,
                Market.platform_market_id == market_id,
            )
        )
        if row is None:
            raise typer.BadParameter(f"no {venue} market {market_id} in the database")
        market = _market_from_row(row)

    draft = draft_attestation(
        venue=platform,
        market_ref=market.platform_market_id,
        resolution_hash=market.resolution_hash,
        rules_text=market.settlement_rules_raw,
    )
    blob = json.dumps(draft, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(blob)
        console.print(f"[green]wrote {out}[/green] - fill in reviewed_by to sign it")
    else:
        console.print_json(blob)


@app.command()
def replay(
    path: Path = typer.Argument(..., help="A run exported by `pair scan --out`"),
    pair: str = typer.Option(None, help="Show only this pair id"),
) -> None:
    """Re-read an exported run: the funnel, then each pair's transitions and legs."""
    payload: dict[str, Any] = json.loads(path.read_text())
    console.print(f"[bold]{payload.get('mode')} run[/bold] "
                  f"{payload.get('started_at')} -> {payload.get('finished_at')}")
    console.print(f"book provenance: {payload.get('book_provenance')}")
    for stage in payload.get("funnel", {}).get("stages", []):
        console.print(
            f"  {stage['stage']:22} {stage['survived']:>7} / {stage['entered']:<7} "
            f"{stage['establishes']}"
        )
        for reason, count in (stage.get("dropped") or {}).items():
            console.print(f"      [dim]dropped {count:>6}  {reason}[/dim]")

    for journal in payload.get("journals", []):
        if pair and journal["pair_id"] != pair:
            continue
        console.print(f"\n[bold]{journal['pair_id']}[/bold] -> {journal['state']} "
                      f"(arbitrage_completed={journal['arbitrage_completed']})")
        for t in journal["transitions"]:
            console.print(f"    {t['at']}  {t['from']:16} -> {t['to']:16} {t['reason'][:90]}")
        for leg in journal["legs"]:
            console.print(
                f"    leg {leg['label']}: requested {leg['requested_quantity']} @ "
                f"{leg['requested_price']}, filled {leg['filled_quantity']} @ "
                f"{leg['average_price']}, fees {leg['fees_usd']}, "
                f"spent ${leg['net_cash_out_usd']}"
            )
            for note in leg["notes"]:
                console.print(f"        [dim]{note}[/dim]")
        console.print(f"    assessment: {journal['assessment']['blocking_reasons'] or 'all axes pass'}")


# ---------------------------------------------------------------- implication
def _strike_of(market) -> tuple[str, Decimal] | None:
    """The comparator and the strike it applies to, or None when this is not a rung.

    Kalshi puts an upward comparator's strike in ``floor_strike`` and a downward one's
    in ``cap_strike``. Reading the wrong one silently produces a threshold that is
    ``None``, which would then compare equal across a whole ladder and assert
    implications that do not exist.
    """
    comparator = (market.strike_type or "").lower()
    if comparator in {"greater", "greater_or_equal"}:
        strike = market.floor_strike
    elif comparator in {"less", "less_or_equal"}:
        strike = market.cap_strike
    else:
        return None
    if strike is None:
        return None
    return comparator, strike


def _load_strike_families(max_age_hours: int, min_members: int = 2):
    """Group booked, open markets into same-event threshold ladders."""
    from collections import defaultdict

    from sqlalchemy import select

    from pmvl_markets.arbitrage.implication import StrikeContract
    from pmvl_markets.arbitrage.pipeline import (
        _market_from_row,
        latest_orderbook,
        orderbook_from_snapshot,
    )
    from pmvl_shared.db import session_scope
    from pmvl_shared.db.models import Market, OrderbookSnapshot
    from pmvl_shared.enums import DataProvenance

    cutoff = utcnow() - timedelta(hours=max_age_hours)
    families: dict[str, list] = defaultdict(list)
    skipped = {"not_a_rung": 0, "no_book": 0}
    with session_scope() as db:
        booked = db.scalars(
            select(OrderbookSnapshot.market_id)
            .where(OrderbookSnapshot.observed_at > cutoff)
            .distinct()
        ).all()
        rows = db.scalars(
            select(Market).where(
                Market.id.in_(booked),
                Market.status == MarketStatus.OPEN.value,
                Market.provenance != DataProvenance.DEMO.value,
            )
        ).all()
        for row in rows:
            market = _market_from_row(row)
            rung = _strike_of(market)
            if rung is None:
                skipped["not_a_rung"] += 1
                continue
            snapshot = latest_orderbook(db, row.id)
            if snapshot is None:
                skipped["no_book"] += 1
                continue
            book = orderbook_from_snapshot(db, snapshot, row)
            if book.is_empty:
                skipped["no_book"] += 1
                continue
            comparator, strike = rung
            key = f"{market.platform.value}:{market.platform_event_id or market.series_ticker}"
            families[key].append(
                StrikeContract(
                    market=market, book=book, comparator=comparator,
                    threshold=strike, market_id=row.id,
                )
            )
    return (
        {k: v for k, v in families.items() if len(v) >= min_members},
        skipped,
    )


async def _live_strike_families(max_events: int, max_rungs: int):
    """Fetch fresh books for whole ladders, so every rung is quoted at once.

    Whole ladders rather than individual markets: a structure needs both rungs, and
    two books fetched minutes apart are not a pair - the second leg's price is a
    different market state from the first's. Fetching an event together keeps the
    cross-leg time skew to the width of one batch, which is then recorded.
    """
    from collections import defaultdict

    from sqlalchemy import func, select

    from pmvl_markets.arbitrage.implication import StrikeContract
    from pmvl_markets.arbitrage.pipeline import _market_from_row
    from pmvl_markets.providers.kalshi import KalshiProvider
    from pmvl_shared.db import session_scope
    from pmvl_shared.db.models import Market
    from pmvl_shared.enums import DataProvenance

    from pmvl_shared.db.models import OrderbookSnapshot

    with session_scope() as db:
        # Seed from ladders that are known to trade, then quote them fresh.
        #
        # Ranking by rung count instead picks the largest ladders, which on Kalshi are
        # the far-dated crypto boards carrying two hundred strikes and no resting
        # size: 240 rungs were requested that way and every one came back empty. Prior
        # book coverage is the cheap proxy for "someone quotes this", and it is only a
        # seed - every price used below is fetched fresh.
        recently_booked = (
            select(OrderbookSnapshot.market_id)
            .where(OrderbookSnapshot.observed_at > utcnow() - timedelta(days=7))
            .distinct()
            .scalar_subquery()
        )
        events = db.execute(
            select(Market.platform_event_id, func.count(Market.id).label("n"))
            .where(
                Market.platform == Platform.KALSHI.value,
                Market.status == MarketStatus.OPEN.value,
                Market.provenance != DataProvenance.DEMO.value,
                Market.platform_event_id.is_not(None),
                Market.id.in_(recently_booked),
                Market.strike_type.in_(
                    ["greater", "greater_or_equal", "less", "less_or_equal"]
                ),
            )
            .group_by(Market.platform_event_id)
            .having(func.count(Market.id) >= 2)
            .order_by(func.count(Market.id).desc())
            .limit(max_events)
        ).all()
        event_ids = [e[0] for e in events]
        rows = db.scalars(
            select(Market).where(
                Market.platform_event_id.in_(event_ids),
                Market.status == MarketStatus.OPEN.value,
                Market.platform == Platform.KALSHI.value,
            )
        ).all()
        markets = [_market_from_row(r) for r in rows]

    by_event: dict[str, list] = defaultdict(list)
    for m in markets:
        if _strike_of(m) is not None:
            by_event[m.platform_event_id or ""].append(m)
    trimmed: list = []
    for _evt, ms in by_event.items():
        trimmed.extend(sorted(ms, key=lambda m: m.platform_market_id)[:max_rungs])

    provider = KalshiProvider()
    try:
        books = await provider.get_orderbooks(trimmed)
    finally:
        await provider.aclose()

    families: dict[str, list] = defaultdict(list)
    for m in trimmed:
        book = books.get(m.platform_market_id)
        if book is None or book.is_empty:
            continue
        comparator, strike = _strike_of(m)
        families[f"kalshi:{m.platform_event_id}"].append(
            StrikeContract(market=m, book=book, comparator=comparator, threshold=strike)
        )
    return {k: v for k, v in families.items() if len(v) >= 2}, {
        "requested": len(trimmed), "booked": sum(len(v) for v in families.values())
    }


@app.command("implication-scan")
def implication_scan(
    live: bool = typer.Option(
        False, "--live", help="Fetch fresh public books instead of reading stored ones"
    ),
    live_events: int = typer.Option(6, help="How many ladders to quote when --live"),
    live_rungs: int = typer.Option(40, help="Max rungs per ladder when --live"),
    max_age_hours: int = typer.Option(24, help="How old a stored book may be"),
    max_quote_age_seconds: int = typer.Option(
        300, help="How old a book may be and still be priced"
    ),
    min_net_floor: float = typer.Option(
        0.01, help="Net floor a structure must clear, in dollars"
    ),
    max_sets: int = typer.Option(10000, help="Size ceiling per structure"),
    min_notional: float = typer.Option(0.0, help="Minimum matched notional"),
    out: Path = typer.Option(None, help="Write certificates and inputs as JSON"),
    show: int = typer.Option(5, help="How many top structures to print"),
) -> None:
    """Find same-venue threshold structures whose payoff floor exceeds their cost.

    Buys YES on the weaker claim and NO on the stronger one. Because the stronger
    claim implies the weaker, the state that would leave the pair unpaid does not
    exist, so the floor is $1 per set. The decision is made on that floor against real
    ask depth and fees - never on a price anomaly.
    """
    from pmvl_markets.arbitrage.implication import ProofStatus, prove_implication, scan_implication_family

    if live:
        families, skipped = asyncio.run(_live_strike_families(live_events, live_rungs))
        console.print(
            f"[bold]{len(families)}[/bold] ladders quoted live "
            f"({skipped['booked']} of {skipped['requested']} rungs returned a book)"
        )
    else:
        families, skipped = _load_strike_families(max_age_hours)
        console.print(
            f"[bold]{len(families)}[/bold] booked threshold families "
            f"({sum(len(v) for v in families.values())} rungs); "
            f"skipped {skipped['not_a_rung']} non-rung, {skipped['no_book']} unbooked"
        )

    certificates = []
    proof_counts: dict[str, int] = {}
    for _key, rungs in sorted(families.items()):
        ordered = sorted(rungs, key=lambda c: c.threshold)
        for i, weaker in enumerate(ordered):
            for stronger in ordered[i + 1 :]:
                p = prove_implication(stronger, weaker)
                if not p.holds:
                    p = prove_implication(weaker, stronger)
                proof_counts[p.status.value] = proof_counts.get(p.status.value, 0) + 1
        certificates.extend(
            scan_implication_family(
                rungs,
                max_sets=Decimal(str(max_sets)),
                min_net_floor_usd=Decimal(str(min_net_floor)),
                max_quote_age_seconds=max_quote_age_seconds,
                min_notional_usd=Decimal(str(min_notional)),
            )
        )

    accepted = [c for c in certificates if c.accepted]
    console.print(
        f"pairs examined: {sum(proof_counts.values())}  "
        + "  ".join(f"{k}={v}" for k, v in sorted(proof_counts.items()))
    )
    console.print(
        f"[bold]certified structures:[/bold] {len(certificates)}   "
        f"[bold green]accepted: {len(accepted)}[/bold green]"
    )

    # Rank by how close the structure came, not by net floor: every rejected pair has
    # a net floor of zero, so sorting on it shows an arbitrary slice of the rejects
    # and hides which ones nearly cleared.
    def closeness(cert):
        if cert.accepted:
            return (0, -cert.net_floor_usd)
        if cert.best_combined_ask is None:
            return (2, Decimal("0"))
        return (1, cert.best_combined_ask)

    ranked = sorted(certificates, key=closeness)[:show]
    priced = [c.best_combined_ask for c in certificates if c.best_combined_ask is not None]
    if priced:
        console.print(
            f"cheapest combined ask across all structures: [bold]${min(priced)}[/bold] "
            f"(a structure clears when this is under $1.00 net of fees)"
        )
    for cert in ranked:
        mark = "[green]ACCEPT[/green]" if cert.accepted else "[yellow]reject[/yellow]"
        console.print(
            f"\n  {mark} {cert.event_key}  net_floor=${cert.net_floor_usd:.4f} "
            f"qty={cert.quantity} cost=${cert.total_cost_usd:.2f} "
            f"status={cert.structural_status}"
        )
        for leg in cert.legs:
            console.print(
                f"      {leg['role']:28} {leg['contract_key']:26} "
                f"{leg['side']:3} {leg['predicate']:26} vwap={leg['vwap']}"
            )
        if not cert.accepted:
            console.print(f"      [dim]{cert.rejection_reason}[/dim]")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "generated_at": iso(utcnow()),
                    "parameters": {
                        "max_age_hours": max_age_hours,
                        "max_quote_age_seconds": max_quote_age_seconds,
                        "min_net_floor_usd": min_net_floor,
                        "max_sets": max_sets,
                        "min_notional_usd": min_notional,
                    },
                    "families": len(families),
                    "rungs": sum(len(v) for v in families.values()),
                    "proof_counts": proof_counts,
                    "accepted": len(accepted),
                    # The inputs travel with the verdicts. A certificate that cannot
                    # be recomputed is a claim, not a result: the books it was priced
                    # against are gone minutes later, so without them nobody can
                    # check the arithmetic or tell a fixed bug from a moved market.
                    "inputs": {
                        key: [
                            {
                                "comparator": c.comparator,
                                "threshold": str(c.threshold),
                                "market": c.market.model_dump(mode="json"),
                                "book": c.book.model_dump(mode="json"),
                            }
                            for c in rungs
                        ]
                        for key, rungs in families.items()
                    },
                    "certificates": [c.as_dict() for c in certificates],
                },
                indent=2,
                default=str,
            )
        )
        console.print(f"\n[green]wrote {out}[/green]")


@app.command("implication-replay")
def implication_replay(
    path: Path = typer.Argument(..., help="A run exported by `implication-scan --out`"),
    show: int = typer.Option(3, help="How many structures to print"),
) -> None:
    """Recompute an exported run from its saved inputs and diff against the verdicts.

    This is the check that a certificate means something. It reprices every structure
    from the books stored in the file - no network, no database - and compares the
    result to what was recorded. A mismatch means the scanner is not a function of its
    inputs, which would make every past verdict unreproducible.
    """
    from decimal import Decimal as Dec

    from pmvl_markets.arbitrage.implication import StrikeContract, scan_implication_family
    from pmvl_shared.schemas import NormalizedMarket, OrderBook

    payload = json.loads(path.read_text())
    params = payload.get("parameters", {})
    inputs = payload.get("inputs")
    if not inputs:
        raise typer.BadParameter(
            "this export predates input capture and cannot be replayed; re-run "
            "implication-scan --out to produce a replayable file"
        )

    recomputed = []
    for _key, rungs in sorted(inputs.items()):
        contracts = [
            StrikeContract(
                market=NormalizedMarket.model_validate(r["market"]),
                book=OrderBook.model_validate(r["book"]),
                comparator=r["comparator"],
                threshold=Dec(r["threshold"]),
            )
            for r in rungs
        ]
        recomputed.extend(
            scan_implication_family(
                contracts,
                max_sets=Dec(str(params.get("max_sets", 10000))),
                min_net_floor_usd=Dec(str(params.get("min_net_floor_usd", 0.01))),
                max_quote_age_seconds=int(params.get("max_quote_age_seconds", 300)),
                min_notional_usd=Dec(str(params.get("min_notional_usd", 0))),
            )
        )

    original = payload.get("certificates", [])
    console.print(
        f"replayed [bold]{len(recomputed)}[/bold] structures from {path.name} "
        f"(file recorded {len(original)})"
    )

    def fingerprint(cert: dict) -> tuple:
        return (
            cert["event_key"],
            tuple(leg["contract_key"] for leg in cert["legs"]),
            cert["quantity"],
            cert["payoff_floor_usd"],
            cert["gross_cost_usd"],
            cert["net_floor_usd"],
            cert["structural_status"],
            cert["proof"]["status"],
        )

    # Book age is wall-clock, so an acceptance that hinged on freshness legitimately
    # differs on replay. Everything else is a pure function of the saved inputs and
    # must not move.
    before = sorted(fingerprint(c) for c in original)
    after = sorted(fingerprint(c.as_dict()) for c in recomputed)
    if before == after:
        console.print("[green]identical[/green]: every payoff floor, size and cost reproduced exactly")
    else:
        drift = [(b, a) for b, a in zip(before, after) if b != a]
        console.print(f"[red]{len(drift)} structures did not reproduce[/red]")
        for b, a in drift[:5]:
            console.print(f"  recorded: {b}")
            console.print(f"  replayed: {a}")
        raise typer.Exit(1)

    for cert in sorted(recomputed, key=lambda c: -c.net_floor_usd)[:show]:
        console.print(
            f"\n  {cert.event_key}  net_floor=${cert.net_floor_usd:.4f} "
            f"qty={cert.quantity} status={cert.structural_status} "
            f"accepted={cert.accepted}"
        )
        for leg in cert.legs:
            console.print(
                f"      {leg['role']:28} {leg['contract_key']:28} "
                f"{leg['side']:3} {leg['predicate']}"
            )


# --------------------------------------------------------------------- baskets
def _load_baskets(max_age_hours: int, live: bool, live_events: int):
    """Group booked, open markets into same-event outcome baskets.

    Unlike the implication route this keeps ``between`` rungs - they are the interior
    cells of the partition and the whole structure depends on having all of them.
    """
    from collections import defaultdict

    from sqlalchemy import func, select

    from pmvl_markets.arbitrage.basket import Bucket, interval_for
    from pmvl_markets.arbitrage.pipeline import (
        _market_from_row,
        latest_orderbook,
        orderbook_from_snapshot,
    )
    from pmvl_shared.db import session_scope
    from pmvl_shared.db.models import Market, OrderbookSnapshot
    from pmvl_shared.enums import DataProvenance

    cutoff = utcnow() - timedelta(hours=max_age_hours)
    with session_scope() as db:
        recently_booked = (
            select(OrderbookSnapshot.market_id)
            .where(OrderbookSnapshot.observed_at > cutoff)
            .distinct()
            .scalar_subquery()
        )
        events = db.execute(
            select(Market.platform_event_id, func.count(Market.id))
            .where(
                Market.status == MarketStatus.OPEN.value,
                Market.provenance != DataProvenance.DEMO.value,
                Market.platform_event_id.is_not(None),
                Market.id.in_(recently_booked),
            )
            .group_by(Market.platform_event_id)
            .having(func.count(Market.id) >= 2)
            .order_by(func.count(Market.id).desc())
            .limit(live_events if live else 10_000)
        ).all()
        event_ids = [e[0] for e in events]
        rows = db.scalars(
            select(Market).where(
                Market.platform_event_id.in_(event_ids),
                Market.status == MarketStatus.OPEN.value,
                Market.provenance != DataProvenance.DEMO.value,
            )
        ).all()
        markets = [_market_from_row(r) for r in rows]
        stored: dict[str, Any] = {}
        if not live:
            for row in rows:
                snapshot = latest_orderbook(db, row.id)
                if snapshot is not None:
                    stored[row.platform_market_id] = orderbook_from_snapshot(db, snapshot, row)

    rungs = [m for m in markets if interval_for(m) is not None]
    if live:
        from pmvl_markets.providers.kalshi import KalshiProvider

        kalshi = [m for m in rungs if m.platform is Platform.KALSHI]
        provider = KalshiProvider()
        try:
            stored = asyncio.run(provider.get_orderbooks(kalshi))
        finally:
            asyncio.run(provider.aclose())

    baskets: dict[str, list] = defaultdict(list)
    for market in rungs:
        book = stored.get(market.platform_market_id)
        if book is None or book.is_empty:
            continue
        baskets[f"{market.platform.value}:{market.platform_event_id}"].append(
            Bucket(market=market, book=book, interval=interval_for(market))
        )
    return {k: v for k, v in baskets.items() if len(v) >= 2}, len(rungs)


@app.command("basket-scan")
def basket_scan(
    live: bool = typer.Option(False, "--live", help="Fetch fresh public books"),
    live_events: int = typer.Option(40, help="How many events to quote when --live"),
    max_age_hours: int = typer.Option(24, help="How old a stored book may be"),
    max_quote_age_seconds: int = typer.Option(300, help="Freshness limit for pricing"),
    min_net_floor: float = typer.Option(0.01, help="Net floor a basket must clear"),
    max_sets: int = typer.Option(10000, help="Size ceiling per basket"),
    out: Path = typer.Option(None, help="Write certificates and inputs as JSON"),
    show: int = typer.Option(5, help="How many baskets to print"),
) -> None:
    """Find same-venue outcome baskets whose payoff floor exceeds their cost.

    Prices both directions. Buying every YES pays $1 because exactly one rung wins;
    buying every NO pays $(N-1) because every rung but the winner does. Both floors
    need the rungs proven disjoint and covering, which is checked on an inferred
    observation grid rather than assumed from a shared event ticker.
    """
    from pmvl_markets.arbitrage.basket import scan_basket

    baskets, rung_total = _load_baskets(max_age_hours, live, live_events)
    console.print(
        f"[bold]{len(baskets)}[/bold] booked baskets "
        f"({sum(len(v) for v in baskets.values())} of {rung_total} rungs have a book)"
    )

    certificates = []
    for _key, buckets in sorted(baskets.items()):
        certificates.extend(
            scan_basket(
                buckets,
                max_sets=Decimal(str(max_sets)),
                min_net_floor_usd=Decimal(str(min_net_floor)),
                max_quote_age_seconds=max_quote_age_seconds,
            )
        )

    proven = [c for c in certificates if c.proof.proven]
    accepted = [c for c in certificates if c.accepted]
    console.print(
        f"certificates: {len(certificates)}  partition proven: {len(proven)}  "
        f"[bold green]accepted: {len(accepted)}[/bold green]"
    )

    for cert in sorted(certificates, key=lambda c: -c.net_floor_usd)[:show]:
        mark = "[green]ACCEPT[/green]" if cert.accepted else "[yellow]reject[/yellow]"
        console.print(
            f"\n  {mark} {cert.event_key} [{cert.direction}] rungs={cert.rung_count} "
            f"floor/set=${cert.payoff_per_set} net_floor=${cert.net_floor_usd:.4f} "
            f"qty={cert.quantity}"
        )
        console.print(
            f"      partition: {'proven' if cert.proof.proven else 'NOT proven'} "
            f"grid={cert.proof.grid_step}  combined_ask=${cert.combined_top_of_book}"
        )
        if not cert.accepted:
            console.print(f"      [dim]{cert.rejection_reason[:160]}[/dim]")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated_at": iso(utcnow()),
            "live": live,
            "parameters": {
                "max_age_hours": max_age_hours,
                "max_quote_age_seconds": max_quote_age_seconds,
                "min_net_floor_usd": min_net_floor,
                "max_sets": max_sets,
            },
            "baskets": len(baskets),
            "accepted": len(accepted),
            "inputs": {
                key: [
                    {
                        "market": b.market.model_dump(mode="json"),
                        "book": b.book.model_dump(mode="json"),
                    }
                    for b in buckets
                ]
                for key, buckets in baskets.items()
            },
            "certificates": [c.as_dict() for c in certificates],
        }, indent=2, default=str))
        console.print(f"\n[green]wrote {out}[/green]")
