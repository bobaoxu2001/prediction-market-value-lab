"""``pmvl record`` - forward capture, and nothing that interprets it.

Deliberately separated from every scan command. The recorder must not know what a
profitable structure looks like, because a recorder that knows would eventually be
tuned - by me, with a good reason, on a Tuesday - to record the events where the
answer comes out well. It captures what it was pointed at, on a fixed cadence, and
writes the refusals to the same file as the captures.
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from decimal import Decimal

from pmvl_markets.providers.kalshi import MAX_BATCH_TICKERS, KalshiProvider
from pmvl_markets.providers.polymarket import PolymarketProvider
from pmvl_shared.money import ONE
from pmvl_markets.recording import (
    STRATEGY_VERSION,
    RecorderConfig,
    SynchronizedRecorder,
    read_log,
    verify_log,
)

app = typer.Typer(add_completion=False, help="Synchronized forward recording.")
console = Console()


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _discover(
    provider: KalshiProvider,
    count: int,
    series: list[str],
    *,
    max_pages: int = 400,
    rank_by: str = "volume",
) -> tuple[list[str], dict[str, Any]]:
    """Events that can be captured in one request, ranked by two-sided legs.

    Ranking by *two-sided* legs rather than by volume or open interest is the whole
    point: a leg quoted on one side only cannot be part of any structure that has to
    be both bought and sold, so recording it would inflate coverage without adding a
    single testable observation.

    Without ``series`` this walks the entire open-market universe rather than a few
    pages. It has to: the exchange's auto-generated combination markets are numerous
    enough that the first twelve thousand rows contain no two-sided event at all, so
    a page-limited scan would not be a sample of the market, it would be a sample of
    whatever the cursor happens to return first.

    Returns the chosen events *and* the census behind the choice, so that the
    selection can be re-derived later instead of taken on trust.
    """
    rows: list[dict[str, Any]] = []
    pages = 0
    exhausted = True
    if series:
        for ticker in series:
            cursor = None
            while pages < max_pages:
                params: dict[str, Any] = {
                    "status": "open", "series_ticker": ticker, "limit": 1000,
                }
                if cursor:
                    params["cursor"] = cursor
                data = await provider._client.get_json(
                    "/markets", params=params, use_cache=False, allow_404=True
                )
                pages += 1
                if not data:
                    break
                rows.extend(data.get("markets") or [])
                cursor = data.get("cursor")
                if not cursor:
                    break
    else:
        cursor = None
        while pages < max_pages:
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = await provider._client.get_json("/markets", params=params, use_cache=False)
            pages += 1
            if not data:
                break
            rows.extend(data.get("markets") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        else:
            #: The page budget ran out with the cursor still live. The scan is a
            #: prefix of the universe, not the universe, and the census has to say
            #: so - a ranking over an unstated subset is not a ranking.
            exhausted = False

    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_event[str(row.get("event_ticker"))].append(row)

    qualified: list[dict[str, Any]] = []
    too_wide = 0
    quoted_but_untraded = 0
    for event, markets in by_event.items():
        if not event:
            continue
        if len(markets) > MAX_BATCH_TICKERS:
            too_wide += 1
            continue
        two_sided = sum(
            1
            for m in markets
            if _f(m.get("yes_bid_dollars")) > 0 and _f(m.get("no_bid_dollars")) > 0
        )
        if two_sided < 2:
            continue
        volume_24h = sum(_f(m.get("volume_24h_fp")) for m in markets)
        if volume_24h <= 0:
            #: Quoted on both sides by somebody, traded by nobody. These dominate the
            #: two-sided universe, and selecting on quote count alone lands entirely
            #: on them - which would measure a maker fill rate of zero for a reason
            #: that has nothing to do with whether making markets pays.
            quoted_but_untraded += 1
            continue
        qualified.append(
            {
                "event": event,
                "two_sided_legs": two_sided,
                "legs": len(markets),
                "volume_24h": volume_24h,
            }
        )

    if rank_by == "volume":
        qualified.sort(key=lambda r: (-r["volume_24h"], -r["two_sided_legs"], r["event"]))
    else:
        qualified.sort(key=lambda r: (-r["two_sided_legs"], -r["volume_24h"], r["event"]))

    census = {
        "pages_fetched": pages,
        "page_budget": max_pages,
        "pagination_exhausted": exhausted,
        "markets_scanned": len(rows),
        "events_seen": len(by_event),
        "events_too_wide_for_one_request": too_wide,
        "events_two_sided_but_never_traded": quoted_but_untraded,
        "events_qualified": len(qualified),
        "qualification": "at least 2 two-sided legs, non-zero 24h volume, fits one request",
        "ranking": rank_by,
        "selected": qualified[:count],
        "runners_up": qualified[count : count + 10],
        #: Every event that passed, not just the ones taken. A study that repeats the
        #: selection over many sessions has to be able to freeze the universe it
        #: selects from; a census that recorded only the winners would let each
        #: session's universe drift with the market and make "the same experiment,
        #: twenty times" untrue in a way nobody could check afterwards.
        "qualified_universe": qualified,
    }
    return [row["event"] for row in qualified[:count]], census


@app.command("forward")
def forward(
    event: list[str] = typer.Option([], "--event", help="Event ticker to record. Repeatable."),
    series: list[str] = typer.Option([], "--series", help="Restrict discovery to these series."),
    discover: int = typer.Option(0, help="Auto-select this many capturable events."),
    rank: str = typer.Option(
        "volume", help="Rank discovered events by 'volume' or 'two-sided'."
    ),
    interval: float = typer.Option(5.0, help="Seconds between capture cycles."),
    duration: float = typer.Option(300.0, help="Total seconds to record."),
    depth: int = typer.Option(25, help="Book levels per leg."),
    trades: bool = typer.Option(True, help="Also record prints for two-sided legs."),
    watch: int = typer.Option(24, help="Max legs whose prints are pulled per cycle."),
    events_from: Path = typer.Option(
        None,
        "--events-from",
        help="Reuse the events a previous run's selection.json chose, instead of rescanning.",
    ),
    out: Path = typer.Option(Path("artifacts/forward"), help="Output directory."),
) -> None:
    """Record every leg of each event inside a single request, on a fixed cadence.

    ``--events-from`` replays an earlier selection rather than re-running discovery.
    Re-running discovery would pick a *different* set of events - the universe moves -
    so two runs meant to extend the same sample have to be pointed at the same census
    explicitly rather than trusting that the ranking is stable.
    """

    async def _run() -> dict[str, Any]:
        provider = KalshiProvider()
        try:
            events = list(event)
            census: dict[str, Any] = {}
            if events_from is not None:
                prior = json.loads(events_from.read_text())
                chosen = [row["event"] for row in prior.get("selected", [])]
                events.extend(e for e in chosen if e not in events)
                console.print(
                    f"reusing the selection recorded in {events_from} "
                    f"({len(chosen)} events)"
                )
            if discover:
                found, census = await _discover(
                    provider, discover, list(series), rank_by=rank
                )
                events.extend(e for e in found if e not in events)
                out.mkdir(parents=True, exist_ok=True)
                (out / "selection.json").write_text(json.dumps(census, indent=2) + "\n")
                console.print(
                    f"scanned {census['markets_scanned']:,} open markets across "
                    f"{census['events_seen']:,} events; "
                    f"{census['events_qualified']:,} qualified "
                    f"({census['events_two_sided_but_never_traded']:,} were quoted "
                    f"on both sides but never traded)"
                )
            if not events:
                raise typer.BadParameter(
                    "no events: pass --event, or --discover N (optionally with --series)"
                )

            config = RecorderConfig(
                event_tickers=tuple(events),
                out_dir=out,
                interval_seconds=interval,
                duration_seconds=duration,
                depth=depth,
                record_trades=trades,
                max_trade_watch=watch,
            )
            recorder = SynchronizedRecorder(provider, config)
            legs = await recorder.prepare()

            table = Table(title=f"recording {len(events)} events (v{STRATEGY_VERSION})")
            table.add_column("event")
            table.add_column("legs", justify="right")
            table.add_column("capturable")
            for ev in events:
                n = len(legs.get(ev, []))
                table.add_row(ev, str(n), "yes" if 0 < n <= MAX_BATCH_TICKERS else "NO")
            console.print(table)

            stats = await recorder.run()
            return stats.as_dict()
        finally:
            await provider.aclose()

    summary = asyncio.run(_run())
    console.print_json(data=summary)

    for name in ("snapshots.jsonl", "trades.jsonl"):
        path = out / name
        if path.exists():
            ok, message = verify_log(path)
            console.print(f"{'[green]' if ok else '[red]'}{name}: {message}")

    (out / "recording_summary.json").write_text(
        json.dumps({"strategy_version": STRATEGY_VERSION, **summary}, indent=2) + "\n"
    )
    console.print(f"\nwrote {out}/recording_summary.json")


@app.command("census")
def census_cmd(
    rank: str = typer.Option("volume", help="Rank by 'volume' or 'two-sided'."),
    top: int = typer.Option(8, help="How many events the ranking would take."),
    series: list[str] = typer.Option([], "--series", help="Restrict the scan to these series."),
    out: Path = typer.Option(..., help="Where to freeze the universe."),
) -> None:
    """Scan the market once and write the qualifying universe, recording nothing.

    Split out from `forward` so that a multi-session study can fix its universe
    before the first session runs. Re-discovering per session would let the pool of
    candidate events drift with the market, and twenty sessions drawn from twenty
    different universes are not twenty repetitions of one experiment.
    """

    async def _run() -> dict[str, Any]:
        provider = KalshiProvider()
        try:
            _, body = await _discover(provider, top, list(series), rank_by=rank)
            return body
        finally:
            await provider.aclose()

    body = asyncio.run(_run())
    body["frozen_at"] = datetime.now(timezone.utc).isoformat()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n")
    console.print(
        f"scanned {body['markets_scanned']:,} open markets across "
        f"{body['events_seen']:,} events\n"
        f"{body['events_qualified']:,} qualified "
        f"({body['events_two_sided_but_never_traded']:,} quoted but never traded, "
        f"{body['events_too_wide_for_one_request']:,} too wide for one request)\n"
        f"pagination exhausted: {body['pagination_exhausted']}\n"
        f"wrote {out}"
    )


def _series_of(event: str) -> str:
    """The series a Kalshi event ticker belongs to: everything before the first dash."""
    return event.split("-", 1)[0]


@app.command("sessions")
def sessions(
    universe: Path = typer.Option(..., help="Frozen census whose series define the universe."),
    count: int = typer.Option(20, help="Sessions to record."),
    duration: float = typer.Option(1800.0, help="Seconds of capture per session."),
    interval: float = typer.Option(10.0, help="Seconds between capture cycles."),
    top: int = typer.Option(8, help="Events per session."),
    watch: int = typer.Option(100, help="Max legs whose prints are pulled per cycle."),
    depth: int = typer.Option(25, help="Book levels per leg."),
    out: Path = typer.Option(..., help="Study directory."),
) -> None:
    """Record N non-overlapping sessions against a frozen universe of series.

    The universe is fixed by ``--universe`` and the per-session selection is
    mechanical: the top ``--top`` currently-open events inside those series by 24h
    volume. Events roll over daily, so freezing the *series* rather than the events is
    what makes twenty sessions repetitions of one experiment rather than twenty
    different ones.

    A session that captures nothing is still a session. It is recorded with whatever
    it got and the study moves on; re-rolling a bad session is how a study quietly
    becomes a search.
    """
    frozen = json.loads(universe.read_text())
    series = sorted({_series_of(row["event"]) for row in frozen.get("qualified_universe", [])})
    if not series:
        raise typer.BadParameter(
            f"{universe} carries no qualified universe; re-run `pmvl record census`"
        )

    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "study.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {
            "strategy_version": STRATEGY_VERSION,
            "universe": str(universe),
            "universe_frozen_at": frozen.get("frozen_at"),
            "series": series,
            "planned_sessions": count,
            "session_seconds": duration,
            "interval_seconds": interval,
            "events_per_session": top,
            "sessions": [],
        }
    )
    done = len(manifest["sessions"])
    console.print(
        f"[bold]{len(series)}[/bold] series in the frozen universe; "
        f"{done} of {count} sessions already recorded"
    )

    async def _one(index: int) -> dict[str, Any]:
        provider = KalshiProvider()
        started = datetime.now(timezone.utc)
        session_dir = out / f"session-{index:02d}"
        try:
            events, census = await _discover(provider, top, series, rank_by="volume")
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "selection.json").write_text(json.dumps(census, indent=2) + "\n")
            if not events:
                return {
                    "session": index, "started_at": started.isoformat(),
                    "date": started.date().isoformat(), "events": [],
                    "note": "no event in the frozen universe was open and two-sided",
                    "stats": {},
                }
            config = RecorderConfig(
                event_tickers=tuple(events),
                out_dir=session_dir,
                interval_seconds=interval,
                duration_seconds=duration,
                depth=depth,
                record_trades=True,
                max_trade_watch=watch,
            )
            recorder = SynchronizedRecorder(provider, config)
            await recorder.prepare()
            stats = await recorder.run()
            return {
                "session": index,
                "started_at": started.isoformat(),
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "date": started.date().isoformat(),
                "events": events,
                "stats": stats.as_dict(),
            }
        finally:
            await provider.aclose()

    for index in range(done + 1, count + 1):
        console.print(f"\n[bold]session {index}/{count}[/bold] starting {datetime.now(timezone.utc).isoformat()}")
        record = asyncio.run(_one(index))
        manifest["sessions"].append(record)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        stats = record.get("stats") or {}
        console.print(
            f"  events={len(record['events'])} snapshots={stats.get('snapshots', 0)} "
            f"trades={stats.get('trades', 0)} rejections={stats.get('rejections', 0)} "
            f"skew_max_ms={stats.get('skew_bound_ms_max')}"
        )

    dates = sorted({r["date"] for r in manifest["sessions"]})
    console.print(
        f"\n[bold]{len(manifest['sessions'])}[/bold] sessions across "
        f"[bold]{len(dates)}[/bold] calendar date(s): {dates}"
    )
    console.print(f"wrote {manifest_path}")


@app.command("polymarket-sets")
def polymarket_sets(
    markets: int = typer.Option(2000, help="How many active markets to scan."),
    chunk: int = typer.Option(200, help="Markets per batch request (400 books)."),
    ledger: Path = typer.Option(
        Path("artifacts/forward/edge_watch_polymarket.csv"),
        help="Rolling ledger, appended to.",
    ),
) -> None:
    """Scan Polymarket complete sets: YES ask + NO ask against the dollar they pay.

    Worth watching separately from Kalshi's version, which cannot dislocate at all.
    On Kalshi the asks are derived from the bids in one matching engine, so a set
    under a dollar is a crossed book and the engine removes it on arrival - a zero
    there is close to a tautology. On Polymarket, YES and NO are separate ERC-1155
    tokens with separate order books and nothing cross-checks them, so a set under a
    dollar is *possible*. That makes a zero here a fact about who is arbitraging,
    not about the plumbing.

    Each batch is one request, so the two sides of a set are always read together.
    """
    import httpx

    async def _run() -> dict[str, Any]:
        provider = PolymarketProvider()
        try:
            mkts = [
                m
                for m in await provider.list_active_markets(limit=markets)
                if m.yes_token_id and m.no_token_id
            ]
            rows: list[tuple[Decimal, Decimal, str]] = []
            books_seen = 0
            async with httpx.AsyncClient(
                base_url="https://clob.polymarket.com", timeout=60
            ) as client:
                for i in range(0, len(mkts), chunk):
                    batch = mkts[i : i + chunk]
                    toks = [t for m in batch for t in (m.yes_token_id, m.no_token_id)]
                    resp = await client.post(
                        "/books", json=[{"token_id": t} for t in toks]
                    )
                    if resp.status_code != 200:
                        continue
                    books = {e["asset_id"]: e for e in resp.json()}
                    books_seen += len(books)
                    for m in batch:
                        ya = books.get(m.yes_token_id, {}).get("asks") or []
                        na = books.get(m.no_token_id, {}).get("asks") or []
                        if not ya or not na:
                            continue
                        y = min((Decimal(l["price"]), Decimal(l["size"])) for l in ya)
                        n = min((Decimal(l["price"]), Decimal(l["size"])) for l in na)
                        rows.append(
                            (ONE - (y[0] + n[0]), min(y[1], n[1]), m.title or "")
                        )
            return {"rows": rows, "books": books_seen, "markets": len(mkts)}
        finally:
            await provider.aclose()

    body = asyncio.run(_run())
    rows = body["rows"]
    if not rows:
        console.print("[red]no complete set could be priced")
        raise typer.Exit(1)

    edges = sorted(r[0] for r in rows)

    def at(q: float) -> Decimal:
        return edges[min(len(edges) - 1, int(q * (len(edges) - 1)))]

    clearing = [r for r in rows if r[0] > 0]
    best = max(rows, key=lambda r: r[0])

    table = Table(title="polymarket complete sets (YES ask + NO ask vs $1)")
    for column in ("markets", "sets priced", "p50 edge", "p95 edge", "best",
                   "clearing $1", "best depth"):
        table.add_column(column, justify="right")
    table.add_row(
        str(body["markets"]), str(len(rows)), str(at(0.50)), str(at(0.95)),
        str(best[0]), str(len(clearing)), str(best[1]),
    )
    console.print(table)
    if clearing:
        console.print(
            f"[bold red]{len(clearing)} complete set(s) priced under a dollar. "
            "Two independent books can do this; verify before believing it."
        )
        for edge, size, title in sorted(clearing, reverse=True)[:5]:
            console.print(f"   edge {edge} on {size} sets - {title[:60]}")
    else:
        console.print(
            "[green]no complete set priced under a dollar - the same answer so far"
        )

    ledger.parent.mkdir(parents=True, exist_ok=True)
    new = not ledger.exists()
    with ledger.open("a", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(
                ["scanned_at", "markets", "sets_priced", "p50_edge", "p95_edge",
                 "best_edge", "best_depth_sets", "clearing_one_dollar"]
            )
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), body["markets"], len(rows),
            at(0.50), at(0.95), best[0], best[1], len(clearing),
        ])
    console.print(f"appended to {ledger}")


@app.command("verify")
def verify(log: Path = typer.Argument(..., help="Log to check end to end.")) -> None:
    """Confirm no observation has been removed or edited since it was written."""
    ok, message = verify_log(log)
    console.print(f"{'[green]intact[/green]' if ok else '[red]BROKEN[/red]'}: {message}")
    raise typer.Exit(0 if ok else 1)


@app.command("coverage")
def coverage(
    directory: Path = typer.Argument(Path("artifacts/forward"), help="Recording directory.")
) -> None:
    """What the recording actually contains, including what it failed to capture."""
    snapshots = directory / "snapshots.jsonl"
    if not snapshots.exists():
        console.print(f"[red]no recording at {snapshots}")
        raise typer.Exit(1)

    per_event: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"snapshots": 0, "rejections": 0, "two_sided": 0, "skews": []}
    )
    versions: set[str] = set()
    for entry in read_log(snapshots):
        body = entry.body
        versions.add(str(body.get("strategy_version")))
        row = per_event[body["event_ticker"]]
        if body["kind"] == "snapshot":
            row["snapshots"] += 1
            row["skews"].append(float(body["skew_bound_ms"]))
            row["two_sided"] += sum(
                1
                for b in body["books"].values()
                if b.get("yes_asks") and b.get("no_asks")
            )
        else:
            row["rejections"] += 1

    table = Table(title=f"coverage - strategy versions {sorted(versions)}")
    for col, just in [("event", "left"), ("snapshots", "right"), ("rejected", "right"),
                      ("two-sided legs", "right"), ("skew p50 ms", "right"),
                      ("skew max ms", "right")]:
        table.add_column(col, justify=just)
    for event, row in sorted(per_event.items()):
        skews = sorted(row["skews"])
        table.add_row(
            event,
            str(row["snapshots"]),
            str(row["rejections"]),
            str(row["two_sided"]),
            f"{skews[len(skews) // 2]:.0f}" if skews else "-",
            f"{skews[-1]:.0f}" if skews else "-",
        )
    console.print(table)

    if len(versions) > 1:
        console.print(
            "[red]this recording spans more than one strategy version; "
            "its cycles must not be pooled"
        )
