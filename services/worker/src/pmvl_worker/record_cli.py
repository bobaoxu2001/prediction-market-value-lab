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
import time
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
    AppendOnlyLog,
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


@app.command("polymarket-forward")
def polymarket_forward(
    markets: int = typer.Option(40, help="Binary markets to record (two tokens each)."),
    interval: float = typer.Option(15.0, help="Seconds between cycles."),
    duration: float = typer.Option(1800.0, help="Total seconds to record."),
    tape_batch: int = typer.Option(5, help="Condition ids per tape request."),
    out: Path = typer.Option(..., help="Output directory."),
) -> None:
    """Record Polymarket books and prints to disk, with tape coverage measured.

    Written after an in-memory capture reported zero maker fills on 538 quotes and
    could not be re-examined to say whether that was a real zero or a mapping bug.
    Everything goes to the same append-only, hash-chained logs the Kalshi recorder
    writes, so the replay tooling runs on it unchanged.

    The tape is polled per market, several condition ids to a request, and coverage is
    **measured**: a page that comes back full whose oldest print is newer than a
    market's cursor means prints between the two were never seen. That batch is split
    and re-polled market by market, and a single market that still shows the gap is
    counted as lost. Missing prints under-count fills, which pulls a maker result
    toward zero - the silent bias this project has already found once - so the count
    is written into the stats rather than left to inference.
    """
    import httpx

    from pmvl_markets.recording import AppendOnlyLog
    from pmvl_markets.recording.polymarket_capture import (
        books_to_snapshot,
        trades_to_records,
    )

    PAGE = 500

    async def _run() -> dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        books_log = AppendOnlyLog(out / "snapshots.jsonl")
        trades_log = AppendOnlyLog(out / "trades.jsonl")
        stats = {"cycles": 0, "snapshots": 0, "book_failures": 0, "trades": 0,
                 "tape_requests": 0, "tape_pages_full": 0, "tape_splits": 0,
                 "markets_with_lost_prints": 0, "skews_ms": []}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                "https://gamma-api.polymarket.com/events",
                params={"closed": "false", "limit": 200,
                        "order": "volume24hr", "ascending": "false"},
            )
            chosen: list[dict[str, Any]] = []
            for event in resp.json():
                for m in event.get("markets") or []:
                    try:
                        ids = json.loads(m.get("clobTokenIds") or "[]")
                    except ValueError:
                        continue
                    if len(ids) == 2 and m.get("conditionId") and float(
                        m.get("volume24hr") or 0
                    ) > 0:
                        chosen.append({"condition": m["conditionId"], "yes": ids[0],
                                       "no": ids[1], "question": m.get("question", "")})
                if len(chosen) >= markets:
                    break
            chosen = chosen[:markets]
            #: The mapping replay needs and nothing in a quote can supply: which token
            #: closes which. Written before the first cycle so it cannot drift.
            (out / "polymarket_markets.json").write_text(json.dumps(
                {"strategy_version": STRATEGY_VERSION, "markets": chosen}, indent=2) + "\n")
            tokens = [t for m in chosen for t in (m["yes"], m["no"])]
            conditions = [m["condition"] for m in chosen]
            cursor: dict[str, int] = {}
            seen: set[str] = set()

            async def pull(ids: list[str]) -> list[dict[str, Any]] | None:
                stats["tape_requests"] += 1
                r = await client.get(
                    "https://data-api.polymarket.com/trades",
                    params={"market": ",".join(ids), "limit": PAGE, "takerOnly": "true"},
                )
                rows = r.json() if r.status_code == 200 else None
                return rows if isinstance(rows, list) else None

            def gap(rows: list[dict[str, Any]], ids: list[str]) -> bool:
                if len(rows) < PAGE:
                    return False
                oldest = min(int(x["timestamp"]) for x in rows)
                return any(oldest > cursor[i] for i in ids if i in cursor)

            def keep(rows: list[dict[str, Any]]) -> None:
                for rec in trades_to_records(rows):
                    if rec.trade_id in seen:
                        continue
                    seen.add(rec.trade_id)
                    trades_log.append(rec.as_dict())
                    stats["trades"] += 1
                for x in rows:
                    cid = x.get("conditionId")
                    if cid:
                        cursor[cid] = max(cursor.get(cid, 0), int(x["timestamp"]))

            deadline = time.time() + duration
            seq = 0
            while time.time() < deadline:
                started = time.time()
                sent = datetime.now(timezone.utc)
                r = await client.post(
                    "https://clob.polymarket.com/books",
                    json=[{"token_id": t} for t in tokens],
                )
                got = datetime.now(timezone.utc)
                if r.status_code == 200:
                    snap = books_to_snapshot(r.json(), event_ticker="POLYMARKET",
                                             sent_at=sent, received_at=got,
                                             sequence=seq, requested=tokens)
                    books_log.append(snap.as_dict())
                    stats["snapshots"] += 1
                    stats["skews_ms"].append(float(snap.skew_bound_ms))
                    seq += 1
                else:
                    stats["book_failures"] += 1

                for i in range(0, len(conditions), tape_batch):
                    batch = conditions[i : i + tape_batch]
                    rows = await pull(batch)
                    if rows is None:
                        continue
                    if len(rows) >= PAGE:
                        stats["tape_pages_full"] += 1
                    if gap(rows, batch) and len(batch) > 1:
                        stats["tape_splits"] += 1
                        for cid in batch:
                            single = await pull([cid])
                            if single is None:
                                continue
                            if gap(single, [cid]):
                                stats["markets_with_lost_prints"] += 1
                            keep(single)
                    else:
                        if gap(rows, batch):
                            stats["markets_with_lost_prints"] += 1
                        keep(rows)

                stats["cycles"] += 1
                await asyncio.sleep(max(0.0, interval - (time.time() - started)))
        skews = sorted(stats.pop("skews_ms"))
        stats["skew_bound_ms_p50"] = skews[len(skews) // 2] if skews else None
        stats["skew_bound_ms_max"] = skews[-1] if skews else None
        return stats

    body = asyncio.run(_run())
    console.print_json(data=body)
    (out / "recording_summary.json").write_text(
        json.dumps({"strategy_version": STRATEGY_VERSION, "venue": "polymarket", **body},
                   indent=2) + "\n")
    for name in ("snapshots.jsonl", "trades.jsonl"):
        path = out / name
        if path.exists():
            ok, message = verify_log(path)
            console.print(f"{'[green]' if ok else '[red]'}{name}: {message}")


@app.command("polymarket-drain")
def polymarket_drain(
    directory: Path = typer.Argument(..., help="A polymarket-forward recording."),
    seconds: float = typer.Option(360.0, help="How long to keep polling the tape."),
    interval: float = typer.Option(20.0, help="Seconds between tape polls."),
    tape_batch: int = typer.Option(5, help="Condition ids per request."),
) -> None:
    """Keep polling the tape after the books stop, so batched prints arrive.

    Polymarket's trade feed is not real time. Its newest timestamp sits frozen and
    ages between indexing batches - measured at three minutes and more platform-wide -
    so the prints from a recording's final minutes exist but have not been published
    when the recorder stops. Without a drain those quotes look unfilled.

    Appends to the recording's existing tape log and continues its hash chain, and
    reads the prints already there first so nothing is written twice.
    """
    import httpx

    from pmvl_markets.recording import AppendOnlyLog, read_log
    from pmvl_markets.recording.polymarket_capture import trades_to_records

    mapping = json.loads((directory / "polymarket_markets.json").read_text())
    conditions = [m["condition"] for m in mapping["markets"]]
    log_path = directory / "trades.jsonl"
    seen = {e.body["trade_id"] for e in read_log(log_path)} if log_path.exists() else set()
    tape = AppendOnlyLog(log_path)

    async def _run() -> dict[str, Any]:
        added = 0
        high_water = 0
        async with httpx.AsyncClient(timeout=60) as client:
            deadline = time.time() + seconds
            while time.time() < deadline:
                started = time.time()
                for i in range(0, len(conditions), tape_batch):
                    r = await client.get(
                        "https://data-api.polymarket.com/trades",
                        params={"market": ",".join(conditions[i : i + tape_batch]),
                                "limit": 500, "takerOnly": "true"},
                    )
                    rows = r.json() if r.status_code == 200 else []
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        high_water = max(high_water, int(row.get("timestamp") or 0))
                    for rec in trades_to_records(rows):
                        if rec.trade_id not in seen:
                            seen.add(rec.trade_id)
                            tape.append(rec.as_dict())
                            added += 1
                await asyncio.sleep(max(0.0, interval - (time.time() - started)))
        return {"prints_added": added, "tape_high_water": high_water}

    body = asyncio.run(_run())
    hw = datetime.fromtimestamp(body["tape_high_water"], tz=timezone.utc) if body[
        "tape_high_water"] else None
    console.print(f"drained {body['prints_added']} prints; tape now indexed through "
                  f"{hw.isoformat() if hw else 'unknown'}")
    ok, message = verify_log(log_path)
    console.print(f"{'[green]' if ok else '[red]'}trades.jsonl: {message}")


@app.command("burst")
def burst(
    seconds: float = typer.Option(60.0, help="How long to sweep for."),
    tickers: int = typer.Option(100, help="Markets per sweep (one request)."),
    universe: Path = typer.Option(
        Path("artifacts/forward/universe-study-frozen.json"),
        help="Frozen census whose series supply the candidates.",
    ),
    ledger: Path = typer.Option(
        Path("artifacts/forward/edge_watch_burst.csv"), help="Rolling ledger."
    ),
) -> None:
    """Sweep one batch of books as fast as the limiter allows, to catch a brief gap.

    The twenty-session study samples every ten seconds, so a dislocation lasting a
    fraction of a second has only a small chance of landing in any given window.
    That is the largest hole in "no route cleared a cent in 787,000 observations",
    and this is the cheap way to probe it: the batch endpoint answers in about half a
    second, so one request per round trip gives roughly fourteen to sixteen times the
    temporal resolution without needing the authenticated websocket.

    It measures the two Kalshi structures at that rate - the complete set inside one
    market, which the matching engine should never let cross, and proven implication
    pairs across rungs, which nothing prevents from crossing.
    """
    import statistics
    import time as _time
    from collections import defaultdict

    from pmvl_markets.arbitrage.implication import (
        ProofStatus,
        StrikeContract,
        prove_implication,
    )
    from pmvl_shared.enums import Side

    def _strike(market: Any):
        comparator = (getattr(market, "strike_type", None) or "").lower()
        if comparator in {"greater", "greater_or_equal"}:
            value = getattr(market, "floor_strike", None)
        elif comparator in {"less", "less_or_equal"}:
            value = getattr(market, "cap_strike", None)
        else:
            return None
        return (comparator, value) if value is not None else None

    async def _run() -> dict[str, Any]:
        provider = KalshiProvider()
        try:
            series = json.loads(universe.read_text()).get("series") or []
            by_event: dict[str, list[Any]] = defaultdict(list)
            for ticker in series:
                data = await provider._client.get_json(
                    "/markets",
                    params={"status": "open", "series_ticker": ticker, "limit": 1000},
                    use_cache=False,
                    allow_404=True,
                )
                for row in (data or {}).get("markets") or []:
                    market = provider._normalize_market(row)
                    if market is not None and _strike(market):
                        by_event[market.platform_event_id].append(market)

            #: Whole ladders, widest first, so every rung of a pair is in one request.
            chosen: list[Any] = []
            for _event, members in sorted(
                by_event.items(), key=lambda kv: -len(kv[1])
            ):
                if not 2 <= len(members) <= MAX_BATCH_TICKERS:
                    continue
                if len(chosen) + len(members) > tickers:
                    continue
                chosen.extend(members)
            lookup = {m.platform_market_id: m for m in chosen}
            keys = list(lookup)
            if not keys:
                return {"error": "no ladder in the frozen universe is currently open"}

            sweeps = 0
            rtts: list[float] = []
            set_sums: list[Decimal] = []
            pair_edges: list[Decimal] = []
            set_crossed = 0
            pair_clearing = 0
            deadline = _time.time() + seconds
            while _time.time() < deadline:
                started = _time.time()
                capture = await provider.get_orderbooks_synchronized(keys)
                rtts.append(_time.time() - started)
                if not capture.ok:
                    continue
                sweeps += 1
                per_event: dict[str, list[StrikeContract]] = defaultdict(list)
                for key, book in capture.books.items():
                    market = lookup.get(key)
                    if market is None:
                        continue
                    yes_ask, no_ask = book.best_ask(Side.YES), book.best_ask(Side.NO)
                    if yes_ask is not None and no_ask is not None:
                        total = yes_ask + no_ask
                        set_sums.append(total)
                        if total < ONE:
                            set_crossed += 1
                    strike = _strike(market)
                    if strike:
                        per_event[market.platform_event_id].append(
                            StrikeContract(
                                market=market, book=book,
                                comparator=strike[0], threshold=strike[1],
                            )
                        )
                for rungs in per_event.values():
                    rungs.sort(key=lambda c: c.threshold)
                    for i, weaker in enumerate(rungs):
                        for stronger in rungs[i + 1 :]:
                            if prove_implication(stronger, weaker).status is not ProofStatus.PROVEN:
                                continue
                            yes_ask = weaker.book.best_ask(Side.YES)
                            no_ask = stronger.book.best_ask(Side.NO)
                            if yes_ask is None or no_ask is None:
                                continue
                            edge = ONE - (yes_ask + no_ask)
                            pair_edges.append(edge)
                            if edge > 0:
                                pair_clearing += 1
            return {
                "sweeps": sweeps, "markets": len(keys),
                "rtt_p50_ms": statistics.median(rtts) * 1000 if rtts else 0,
                "hz": sweeps / seconds if seconds else 0,
                "set_observations": len(set_sums),
                "set_min": min(set_sums) if set_sums else None,
                "set_crossed": set_crossed,
                "pair_observations": len(pair_edges),
                "pair_best": max(pair_edges) if pair_edges else None,
                "pair_clearing": pair_clearing,
            }
        finally:
            await provider.aclose()

    body = asyncio.run(_run())
    if body.get("error"):
        console.print(f"[red]{body['error']}")
        raise typer.Exit(1)

    table = Table(title="burst sweep - the resolution the 10s study cannot reach")
    for column in ("sweeps", "Hz", "vs 10s study", "rtt p50",
                   "set obs", "set min", "set crossed",
                   "pair obs", "pair best", "pair clearing"):
        table.add_column(column, justify="right")
    table.add_row(
        str(body["sweeps"]), f"{body['hz']:.2f}", f"{body['hz'] * 10:.0f}x",
        f"{body['rtt_p50_ms']:.0f}ms",
        str(body["set_observations"]), str(body["set_min"]), str(body["set_crossed"]),
        str(body["pair_observations"]), str(body["pair_best"]), str(body["pair_clearing"]),
    )
    console.print(table)
    if body["set_crossed"] or body["pair_clearing"]:
        console.print(
            "[bold red]something cleared at burst resolution that the 10s cadence "
            "would have missed. Verify before believing it."
        )
    else:
        console.print(
            f"[green]nothing cleared at {body['hz'] * 10:.0f}x the study's resolution"
        )

    ledger.parent.mkdir(parents=True, exist_ok=True)
    new = not ledger.exists()
    with ledger.open("a", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(
                ["scanned_at", "seconds", "sweeps", "hz", "markets", "set_observations",
                 "set_min", "set_crossed", "pair_observations", "pair_best",
                 "pair_clearing"]
            )
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), seconds, body["sweeps"],
            round(body["hz"], 3), body["markets"], body["set_observations"],
            body["set_min"], body["set_crossed"], body["pair_observations"],
            body["pair_best"], body["pair_clearing"],
        ])
    console.print(f"appended to {ledger}")


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


@app.command("settlements")
def settlements(
    directories: list[Path] = typer.Argument(..., help="Day directories whose markets to look up."),
    out: Path = typer.Option(
        Path("artifacts/forward/settlements.jsonl"), help="Append-only settlement log."
    ),
) -> None:
    """Record how every market in these recordings settled, for offline replay.

    Holding a maker fill to settlement needs the outcome, and replay never touches the
    network - so the outcome is recorded like everything else. One row per market per
    run, as the venue reports it now: an open market is written as open, and a later
    run writes it again once it settles. Nothing is rewritten; replay reads the latest
    row. Read-only, one batched lookup per hundred markets.
    """
    tickers: set[str] = set()
    for directory in directories:
        for session_dir in sorted(directory.glob("session-*")):
            snapshots = session_dir / "snapshots.jsonl"
            if not snapshots.exists():
                continue
            for entry in read_log(snapshots):
                if entry.body.get("kind") == "snapshot":
                    tickers.update(entry.body["books"])
    ordered = sorted(tickers)
    if not ordered:
        console.print("[red]no recorded markets under those directories")
        raise typer.Exit(1)

    async def _run() -> list[dict[str, Any]]:
        provider = KalshiProvider()
        rows: list[dict[str, Any]] = []
        try:
            for i in range(0, len(ordered), MAX_BATCH_TICKERS):
                chunk = ordered[i : i + MAX_BATCH_TICKERS]
                data = await provider._client.get_json(
                    "/markets", params={"tickers": ",".join(chunk), "limit": len(chunk)},
                    use_cache=False,
                )
                seen = set()
                fetched_at = datetime.now(timezone.utc).isoformat()
                for m in (data or {}).get("markets") or []:
                    seen.add(m["ticker"])
                    rows.append({
                        "ticker": m["ticker"], "status": m.get("status"), "result": m.get("result"),
                        "settlement_value_dollars": m.get("settlement_value_dollars"),
                        "settlement_ts": m.get("settlement_ts"), "fetched_at": fetched_at,
                    })
                #: a market the venue no longer returns is written down as missing,
                #: not skipped - an absent row would read as "never looked"
                rows.extend({"ticker": t, "status": "not_returned", "fetched_at": fetched_at}
                            for t in chunk if t not in seen)
        finally:
            await provider.aclose()
        return rows

    rows = asyncio.run(_run())
    log = AppendOnlyLog(out)
    for row in rows:
        log.append(row)
    final = sum(1 for r in rows if r.get("status") in ("finalized", "settled")
                and r.get("settlement_value_dollars") not in (None, ""))
    console.print(f"{len(ordered)} markets: {final} settled, {len(rows) - final} not yet; appended to {out}")


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
