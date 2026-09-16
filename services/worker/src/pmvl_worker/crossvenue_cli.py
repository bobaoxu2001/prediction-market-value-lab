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
from pmvl_shared.timeutil import utcnow

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
