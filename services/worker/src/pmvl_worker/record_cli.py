"""``pmvl record`` - forward capture, and nothing that interprets it.

Deliberately separated from every scan command. The recorder must not know what a
profitable structure looks like, because a recorder that knows would eventually be
tuned - by me, with a good reason, on a Tuesday - to record the events where the
answer comes out well. It captures what it was pointed at, on a fixed cadence, and
writes the refusals to the same file as the captures.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from pmvl_markets.providers.kalshi import MAX_BATCH_TICKERS, KalshiProvider
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
