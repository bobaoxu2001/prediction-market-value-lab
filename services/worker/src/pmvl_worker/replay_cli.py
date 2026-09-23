"""``pmvl replay`` - analysis, run only against recorded files.

Every command here refuses to touch the network. A measurement that needs a live
fetch cannot be re-run against the same data next week, and an experiment whose
inputs move is not an experiment. The recording's hash chain is verified on load, so
a run against an edited log fails instead of quietly reporting a better number.
"""

from __future__ import annotations

import csv
import json
from typing import Any
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pmvl_markets.arbitrage.dislocation import (
    THRESHOLDS_CENTS,
    harvest,
    harvest_implication_pairs,
)
from pmvl_markets.arbitrage.maker_experiment import (
    MARKOUT_HORIZONS,
    FillEstimator,
    QuotePolicy,
    evaluate_quote,
    generate_quotes,
    run_experiment,
)
from pmvl_markets.arbitrage.maker_study import (
    SessionResult,
    compare_days,
    pool_sessions,
)
from pmvl_markets.recording import load_recording
from pmvl_shared.enums import Side

app = typer.Typer(add_completion=False, help="Offline replay of a forward recording.")
console = Console()


def _load(directory: Path):
    recording = load_recording(directory)
    if not recording.snapshots:
        console.print(f"[red]no snapshots in {directory}")
        raise typer.Exit(1)
    version = recording.single_version
    if version is None:
        console.print(
            f"[red]recording spans strategy versions {sorted(recording.strategy_versions)}; "
            "these cycles answer different questions and must not be pooled"
        )
        raise typer.Exit(1)
    console.print_json(data=recording.as_dict())
    return recording, version


def _write_quote_ledger(path: Path, report) -> None:
    """Every quote the policy placed, filled or not.

    The unfilled ones are most of the file and all of the denominator. A ledger of
    only the fills would make the fill rate unrecoverable from the artifact, which is
    the one number a reader most needs to check.
    """
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["market", "event", "placed_at", "price", "size", "queue_ahead",
             "spread", "at_level_volume", "through_volume",
             *[f"filled_{e.value}" for e in FillEstimator],
             *[f"markout_{h}s" for h in MARKOUT_HORIZONS],
             "hedge_cost", "liquidation_pnl"]
        )
        for outcome in report.outcomes:
            quote = outcome.quote
            writer.writerow(
                [quote.market, quote.event_ticker, quote.placed_at.isoformat(),
                 quote.price, quote.size, quote.queue_ahead, quote.spread_at_placement,
                 outcome.relevant_volume_at_level, outcome.relevant_volume_through,
                 *[outcome.filled_size[e] for e in FillEstimator],
                 *[outcome.markout[h] for h in MARKOUT_HORIZONS],
                 outcome.hedge_cost, outcome.liquidation_pnl]
            )


@app.command("maker")
def maker(
    directory: Path = typer.Argument(Path("artifacts/forward"), help="Recording directory."),
    side: str = typer.Option("yes", help="Side to quote."),
    improve_ticks: int = typer.Option(0, help="0 joins the bid; 1 steps in front of it."),
    size: int = typer.Option(50, help="Contracts per quote."),
    lifetime: int = typer.Option(60, help="Seconds a quote rests before cancellation."),
    min_spread: float = typer.Option(0.02, help="Minimum spread to quote into."),
    latency_ms: int = typer.Option(
        300, help="Assumed delay between seeing the book and the order resting on it."
    ),
    out: Path = typer.Option(None, help="Where to write the report. Defaults inside the recording."),
) -> None:
    """Measure fill probability, completion, markout and unconditional expectancy."""
    recording, version = _load(directory)
    policy = QuotePolicy(
        side=Side(side),
        improve_ticks=improve_ticks,
        size=Decimal(size),
        lifetime_seconds=lifetime,
        min_spread=Decimal(str(min_spread)),
        placement_latency_ms=latency_ms,
        name=f"{'improve' if improve_ticks else 'join'}-{side}",
    )
    report = run_experiment(
        recording.snapshots, recording.trades, policy, strategy_version=version
    )
    body = report.as_dict()

    table = Table(title=f"maker experiment - {policy.name} (v{version})")
    for column in ("estimator", "fills", "fill rate", "completion",
                   "markout 60s | fill", "hedge cost | fill", "hedged pair PnL",
                   "E[PnL] / quote 60s", "95% CI", "admissible"):
        table.add_column(column, justify="right" if column != "estimator" else "left")
    for estimator in FillEstimator:
        row = body["estimators"][estimator.value]
        ci = row["expectancy_per_quote_ci95"]
        table.add_row(
            estimator.value,
            str(row["fills"]),
            row["fill_rate"],
            row["completion_rate"],
            str(row["mean_markout_given_fill"]["60"]),
            str(row["mean_hedge_cost_given_fill"]),
            str(row["hedged_pair_pnl_per_contract"]),
            row["expectancy_per_quote_usd"]["60"],
            f"[{ci[0]}, {ci[1]}]" if ci else "-",
            "yes" if row["admissible"] else "NO",
        )
    console.print(table)
    console.print(
        f"quotes placed: {body['quotes_placed']} across {body['markets']} markets, "
        f"{body['event_days']} event-days across {body['distinct_dates']} date(s)"
    )

    target = out or (directory / f"maker_experiment_{policy.name}.json")
    target.write_text(json.dumps(body, indent=2) + "\n")

    ledger = directory / f"maker_quote_ledger_{policy.name}.csv"
    _write_quote_ledger(ledger, report)

    console.print(f"wrote {target}\nwrote {ledger} ({len(report.outcomes)} quotes, winners and losers)")


@app.command("maker-sweep")
def maker_sweep(
    directory: Path = typer.Argument(Path("artifacts/forward"), help="Recording directory."),
    size: int = typer.Option(50, help="Contracts per quote."),
    lifetime: int = typer.Option(60, help="Seconds a quote rests before cancellation."),
    latency_ms: int = typer.Option(300, help="Delay before the order rests on the book."),
    out: Path = typer.Option(None, help="Where to write the combined report."),
) -> None:
    """Run every declared policy and report all of them, not the best of them.

    Four policies are fixed here in the source: join or improve, on each side. They
    are run together and reported together on purpose. Running four and quoting the
    one that came out ahead is the same mistake as running four hundred - the best of
    N looks good at N=4 too, and the only defence is that the other three are printed
    next to it.
    """
    recording, version = _load(directory)
    policies = [
        QuotePolicy(
            side=Side(side_name), improve_ticks=ticks, size=Decimal(size),
            lifetime_seconds=lifetime, placement_latency_ms=latency_ms,
            name=f"{'improve' if ticks else 'join'}-{side_name}",
        )
        for side_name in ("yes", "no")
        for ticks in (0, 1)
    ]

    table = Table(title=f"maker policy sweep - {len(policies)} policies (v{version})")
    for column in ("policy", "quotes", "fills", "fill rate", "markout 60s | fill",
                   "hedged pair PnL", "E[PnL] / quote 60s", "95% CI"):
        table.add_column(column, justify="right" if column != "policy" else "left")

    reports = {}
    for policy in policies:
        report = run_experiment(
            recording.snapshots, recording.trades, policy, strategy_version=version
        )
        body = report.as_dict()
        reports[policy.name] = body
        _write_quote_ledger(directory / f"maker_quote_ledger_{policy.name}.csv", report)
        row = body["estimators"][FillEstimator.QUEUE_MODEL.value]
        ci = row["expectancy_per_quote_ci95"]
        table.add_row(
            policy.name,
            str(body["quotes_placed"]),
            str(row["fills"]),
            row["fill_rate"],
            str(row["mean_markout_given_fill"]["60"]),
            str(row["hedged_pair_pnl_per_contract"]),
            row["expectancy_per_quote_usd"]["60"],
            f"[{ci[0]}, {ci[1]}]" if ci else "-",
        )
    console.print(table)
    console.print(
        "[yellow]all four policies are reported because four were run; the best of "
        "four is not a finding"
    )

    target = out or (directory / "maker_policy_sweep.json")
    target.write_text(
        json.dumps(
            {
                "policies_run": len(policies),
                "multiplicity_note": (
                    "every policy declared in the source was run and every one is "
                    "reported. Selecting the strongest of these after the fact would "
                    "need a multiplicity correction that none of these intervals carry"
                ),
                "estimator_used_in_table": FillEstimator.QUEUE_MODEL.value,
                "reports": reports,
            },
            indent=2,
        )
        + "\n"
    )
    console.print(f"wrote {target}")


def _run_study(directory: Path, policies, *, size, lifetime, latency_ms):
    """Pool one study directory. Shared by the study and replication commands."""
    manifest_path = directory / "study.json"
    if not manifest_path.exists():
        console.print(f"[red]no study manifest at {manifest_path}")
        raise typer.Exit(1)
    manifest = json.loads(manifest_path.read_text())

    results: list[SessionResult] = []
    versions: set[str] = set()
    for record in manifest["sessions"]:
        session_dir = directory / f"session-{record['session']:02d}"
        if not (session_dir / "snapshots.jsonl").exists():
            continue
        recording = load_recording(session_dir)
        version = recording.single_version
        if version is None:
            console.print(
                f"[red]{directory} session {record['session']} spans strategy versions "
                f"{sorted(recording.strategy_versions)}; refusing to pool it"
            )
            raise typer.Exit(1)
        versions.add(version)
        for policy in policies:
            results.append(
                SessionResult(
                    session=record["session"], date=record["date"],
                    started_at=record["started_at"],
                    report=run_experiment(
                        recording.snapshots, recording.trades, policy,
                        strategy_version=version,
                    ),
                )
            )
    if len(versions) > 1:
        console.print(f"[red]{directory} spans recorder versions {sorted(versions)}")
        raise typer.Exit(1)
    if not results:
        console.print(f"[red]no session in {directory} carries a recording")
        raise typer.Exit(1)
    return pool_sessions(results, planned_sessions=manifest["planned_sessions"]), versions.pop()


@app.command("polymarket-maker")
def polymarket_maker(
    directory: Path = typer.Argument(..., help="Directory from `pmvl record polymarket-forward`."),
    size: int = typer.Option(50, help="Contracts per quote."),
    lifetime: int = typer.Option(60, help="Seconds a quote rests before cancellation."),
    latency_ms: int = typer.Option(300, help="Delay before the order rests on the book."),
    min_spread: float = typer.Option(0.002, help="Minimum spread to quote into (2 ticks)."),
    out: Path = typer.Option(None, help="Where to write the report."),
) -> None:
    """The maker experiment on Polymarket: zero fees, 0.001 ticks, complement hedge.

    Each token is its own single-sided market, so every quote buys a token and the
    closing leg is the *complement* token - both together settle for exactly $1. Two
    declared policies, join and improve, and both are reported: every token in the
    recording is quoted, so between them they cover buying YES and buying NO.

    Fees are zero because the venue charges none on 999 of 1,000 markets. That is a
    measured fact about the venue, not an assumption made to flatter the result, and
    it is the whole reason this experiment is worth running separately.
    """
    recording, version = _load(directory)
    mapping = json.loads((directory / "polymarket_markets.json").read_text())
    complement: dict[str, str] = {}
    condition: dict[str, str] = {}
    for m in mapping["markets"]:
        complement[m["yes"]] = m["no"]
        complement[m["no"]] = m["yes"]
        #: both tokens of one market are one cluster - they move in opposite
        #: directions and are one draw of the market, not two
        condition[m["yes"]] = condition[m["no"]] = m["condition"]

    def hedge_for(quote):
        other = complement.get(quote.market)
        return (other, Side.YES) if other else None

    from pmvl_markets.recording.polymarket_capture import with_mint_fills

    #: Most fills on these markets are mints: a buyer of the complement crossing a
    #: resting bid. The tape shows only the taker's side, so those are added back here.
    tape = with_mint_fills(recording.trades, complement)
    console.print(
        f"{len(recording.trades)} prints on the tape, {len(tape) - len(recording.trades)} "
        "mint fills inferred on the complement token"
    )

    #: The tape is only known complete up to the newest print actually recorded.
    #: Polymarket indexes trades in batches, so a quote whose window runs past this
    #: point may have fills that were never published before recording stopped.
    horizon = max((t.traded_at for t in recording.trades), default=None)
    last_book = max((s.response_received_at for s in recording.snapshots), default=None)
    if horizon and last_book:
        gap = (last_book - horizon).total_seconds()
        console.print(
            f"tape complete through {horizon.isoformat()} - "
            + (f"[yellow]{gap:.0f}s short of the last book; quotes past it are excluded"
               if gap > 0 else "[green]covers every book in the recording")
        )

    table = Table(title=f"polymarket maker (v{version}, zero fees)")
    for column in ("policy", "quotes", "excluded", "fills", "fill rate",
                   "hedged pair", "mark E@60", "mark E@300", "EXIT E@60", "EXIT E@300"):
        table.add_column(column, justify="right" if column != "policy" else "left")

    reports = {}
    for name, ticks in (("join", 0), ("improve", 1)):
        policy = QuotePolicy(
            side=Side.YES, improve_ticks=ticks, size=Decimal(size),
            lifetime_seconds=lifetime, placement_latency_ms=latency_ms,
            min_spread=Decimal(str(min_spread)),
            maker_fee_rate=Decimal("0"), taker_fee_rate=Decimal("0"),
            #: Polymarket ticks at 0.001, not the 0.01 the Kalshi default assumes.
            tick=Decimal("0.001"),
            name=f"poly-{name}",
        )
        report = run_experiment(
            recording.snapshots, tape, policy,
            strategy_version=version, hedge_for=hedge_for,
            tape_complete_through=horizon, cluster_of=condition,
        )
        body = report.as_dict()
        reports[policy.name] = body
        _write_quote_ledger(directory / f"maker_quote_ledger_{policy.name}.csv", report)
        row = body["estimators"][FillEstimator.QUEUE_MODEL.value]
        table.add_row(
            policy.name, str(body["quotes_placed"]),
            str(body["quotes_excluded_tape_incomplete"]), str(row["fills"]),
            row["fill_rate"], str(row["hedged_pair_pnl_per_contract"]),
            row["expectancy_per_quote_usd"]["60"], row["expectancy_per_quote_usd"]["300"],
            row["exit_expectancy_per_quote_usd"]["60"],
            row["exit_expectancy_per_quote_usd"]["300"],
        )
    console.print(table)
    target = out or (directory / "polymarket_maker_report.json")
    target.write_text(json.dumps({"venue": "polymarket", "fees": "zero (measured)",
                                  "reports": reports}, indent=2) + "\n")
    console.print(f"wrote {target}")


@app.command("daily-scan")
def daily_scan(
    directory: Path = typer.Argument(..., help="A day directory written by `pmvl record sessions`."),
    ledger: Path = typer.Option(
        Path("artifacts/forward/edge_watch.csv"),
        help="Rolling one-line-per-day ledger, appended to.",
    ),
) -> None:
    """Scan one recorded day for a deterministic route, and write the answer down.

    Runs every day the recorder produces, including the days that find nothing -
    especially those. A watchdog that only speaks when it sees something cannot be
    told apart from a broken one, so this appends a line whether or not anything
    clears, and the line says how much was looked at.

    It answers one question: did a synchronized, fee-paying, sequentially-survivable
    route exist today. Not "was the market interesting"; the threshold is the one the
    audit established - roughly 1.3c to beat fees and 3-4c to survive a single
    adverse fill - and it is not adjustable from the command line.
    """
    from pmvl_markets.arbitrage.dislocation import (
        THRESHOLDS_CENTS,
        harvest,
        harvest_implication_pairs,
    )

    manifest_path = directory / "study.json"
    if not manifest_path.exists():
        console.print(f"[red]no study manifest at {manifest_path}")
        raise typer.Exit(1)
    manifest = json.loads(manifest_path.read_text())

    snapshots = []
    markets: dict[str, Any] = {}
    versions: set[str] = set()
    for record in manifest["sessions"]:
        session_dir = directory / f"session-{record['session']:02d}"
        if not (session_dir / "snapshots.jsonl").exists():
            continue
        recording = load_recording(session_dir)
        version = recording.single_version
        if version is not None:
            versions.add(version)
        snapshots.extend(recording.snapshots)
        markets.update(recording.markets)

    if not snapshots:
        console.print(f"[yellow]{directory} holds no snapshots")
        raise typer.Exit(1)

    date = manifest["sessions"][0]["date"] if manifest["sessions"] else "unknown"
    version = sorted(versions)[0] if len(versions) == 1 else "|".join(sorted(versions))

    pairs = harvest_implication_pairs(snapshots, markets, strategy_version=version)
    sets = harvest(snapshots, strategy_version=version)

    #: A route only counts if it clears the fee barrier *and* survives being filled
    #: one leg at a time. Either alone has already been shown to mean nothing.
    survivors = [
        e
        for report in (pairs, sets)
        for e in report.episodes
        if e.threshold_cents >= 3 and e.survives_sequencing
    ]

    table = Table(title=f"edge watch - {date} ({version})")
    for column in ("structure", "observations", "unpriceable",
                   *[f">={c}c" for c in THRESHOLDS_CENTS], "best edge", "survivors"):
        table.add_column(column, justify="right" if column != "structure" else "left")
    for name, report in (("implication", pairs), ("complete-set", sets)):
        body = report.as_dict()
        best = body["gross_edge_per_set_percentiles"].get("max", "-")
        table.add_row(
            name, str(body["market_observations"]), str(body["unpriceable_observations"]),
            *[str(body["thresholds"][str(c)]["observations_clearing"]) for c in THRESHOLDS_CENTS],
            best,
            str(sum(1 for e in report.episodes
                    if e.threshold_cents >= 3 and e.survives_sequencing)),
        )
    console.print(table)

    if survivors:
        console.print(
            f"\n[bold red]{len(survivors)} route(s) cleared 3c AND survived sequential "
            "execution today. This has not happened before; verify before believing it."
        )
        for e in survivors[:5]:
            console.print(f"   {e.as_dict()}")
    else:
        console.print(
            "\n[green]no route cleared 3c with a positive sequential floor - "
            "the same answer as every day so far"
        )

    ledger.parent.mkdir(parents=True, exist_ok=True)
    new = not ledger.exists()
    with ledger.open("a", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(
                ["date", "recorder_version", "snapshots", "pair_observations",
                 "set_observations", "pair_best_edge", "set_best_edge",
                 "clearing_3c", "survivors_3c", "scanned_at"]
            )
        writer.writerow([
            date, version, len(snapshots),
            pairs.as_dict()["market_observations"], sets.as_dict()["market_observations"],
            pairs.edge_percentiles().get("max", ""), sets.edge_percentiles().get("max", ""),
            sum(r.as_dict()["thresholds"]["3"]["observations_clearing"] for r in (pairs, sets)),
            len(survivors),
            datetime.now(timezone.utc).isoformat(),
        ])
    console.print(f"appended to {ledger}")


@app.command("maker-days")
def maker_days(
    directories: list[Path] = typer.Argument(..., help="Day directories to pool."),
    planned_days: int = typer.Option(20, help="Eligible days the pre-registration targets."),
    size: int = typer.Option(50, help="Contracts per quote."),
    lifetime: int = typer.Option(60, help="Seconds a quote rests before cancellation."),
    latency_ms: int = typer.Option(300, help="Delay before the order rests on the book."),
    out: Path = typer.Option(None, help="Where to write the pooled report."),
) -> None:
    """Pool whole days and resample calendar dates, per the multi-day pre-registration.

    This is the only analysis that ever puts an interval on the *day* factor. Every
    earlier result was conditional on one afternoon, however many sessions it held.

    Refuses to mix recorder versions. Day 1 was recorded under 1.0.0, whose skew
    bound counted local queueing and whose tape starved in busy hours; it was
    excluded from this study before day 3 existed, and not because of its result.

    Before the planned number of days the verdict is labelled an interim and decides
    nothing. The study does not stop early on a good interim.
    """
    policies = [
        QuotePolicy(
            side=Side(side_name), improve_ticks=ticks, size=Decimal(size),
            lifetime_seconds=lifetime, placement_latency_ms=latency_ms,
            name=f"{'improve' if ticks else 'join'}-{side_name}",
        )
        for side_name in ("yes", "no")
        for ticks in (0, 1)
    ]
    results: list[SessionResult] = []
    versions: dict[str, set[str]] = {}
    for directory in directories:
        manifest = json.loads((directory / "study.json").read_text())
        for record in manifest["sessions"]:
            session_dir = directory / f"session-{record['session']:02d}"
            if not (session_dir / "snapshots.jsonl").exists():
                continue
            recording = load_recording(session_dir)
            version = recording.single_version
            if version is None:
                console.print(f"[red]{session_dir} spans recorder versions; refusing")
                raise typer.Exit(1)
            versions.setdefault(version, set()).add(directory.name)
            for policy in policies:
                results.append(SessionResult(
                    #: Sessions are numbered 1-20 within each day, so session 3 of two
                    #: different days would collide as one cluster. The day's position
                    #: goes into the id to keep them apart.
                    session=record["session"] + 100 * directories.index(directory),
                    date=record["date"], started_at=record["started_at"],
                    report=run_experiment(recording.snapshots, recording.trades, policy,
                                          strategy_version=version),
                ))
    if len(versions) > 1:
        console.print(
            f"[red]these directories span recorder versions "
            f"{ {v: sorted(d) for v, d in versions.items()} } - not poolable"
        )
        raise typer.Exit(1)

    dates = sorted({r.date for r in results})
    study = pool_sessions(results, planned_sessions=planned_days * 20)
    body = study.as_dict()
    body["planned_days"] = planned_days
    body["days_pooled"] = len(dates)
    if len(dates) < planned_days:
        body["verdict"] = "INTERIM_NOT_A_DECISION"
        body["why"] = (f"{len(dates)} of {planned_days} eligible days; the multi-day "
                       "pre-registration decides once, at the planned count")

    table = Table(title=f"maker - {len(dates)} day(s) pooled, date-level clustering")
    for column in ("policy", "quotes", "fills", "mean E[PnL]/quote", "date CI95",
                   "session CI95", "hedged pair"):
        table.add_column(column, justify="right" if column != "policy" else "left")
    for row in body["policies"]:
        def fmt(ci):
            return f"[{ci[0]}, {ci[1]}]" if ci else "-"
        table.add_row(row["policy"]["name"], str(row["quotes_placed"]),
                      str(row["fills"]["QUEUE_MODEL"]), row["mean_pnl_per_quote_usd"],
                      fmt(row["date_cluster_ci95"]), fmt(row["session_cluster_ci95"]),
                      str(row["hedged_pair_pnl_per_contract"]))
    console.print(table)
    console.print(f"\n[bold]{body['verdict']}[/bold]: {body['why']}")
    console.print(f"dates pooled: {dates}")
    target = out or Path("artifacts/forward/maker_days_report.json")
    target.write_text(json.dumps(body, indent=2) + "\n")
    console.print(f"wrote {target}")


@app.command("maker-replication")
def maker_replication(
    day_one: Path = typer.Argument(..., help="The first study directory."),
    day_two: Path = typer.Argument(..., help="The second study directory."),
    size: int = typer.Option(50, help="Contracts per quote."),
    lifetime: int = typer.Option(60, help="Seconds a quote rests before cancellation."),
    latency_ms: int = typer.Option(300, help="Delay before the order rests on the book."),
    out: Path = typer.Option(None, help="Where to write the replication report."),
) -> None:
    """Apply the pre-registered comparison between two days. Compares, never pools.

    Pooling would answer "what is the average across these days", which two days
    cannot support - and here it is barred outright, because the days were recorded
    under different recorder versions. Comparison answers "did the finding hold up",
    which is the question a second day can actually settle.
    """
    policies = [
        QuotePolicy(
            side=Side(side_name), improve_ticks=ticks, size=Decimal(size),
            lifetime_seconds=lifetime, placement_latency_ms=latency_ms,
            name=f"{'improve' if ticks else 'join'}-{side_name}",
        )
        for side_name in ("yes", "no")
        for ticks in (0, 1)
    ]
    one, version_one = _run_study(day_one, policies, size=size, lifetime=lifetime,
                                  latency_ms=latency_ms)
    two, version_two = _run_study(day_two, policies, size=size, lifetime=lifetime,
                                  latency_ms=latency_ms)

    report = compare_days(
        one, two,
        day_one_label=f"{day_one.name} ({', '.join(one.dates)}, recorder {version_one})",
        day_two_label=f"{day_two.name} ({', '.join(two.dates)}, recorder {version_two})",
    )
    body = report.as_dict()
    body["day_one_sessions"] = one.completed_sessions
    body["day_two_sessions"] = two.completed_sessions
    body["recorder_versions"] = {"day_one": version_one, "day_two": version_two}

    table = Table(title=f"replication - {body['day_one']} vs {body['day_two']}")
    for column in ("policy", "day 1 mean", "day 1 CI95", "day 2 mean", "day 2 CI95",
                   "negative", "inside d1", "positive d2"):
        table.add_column(column, justify="right" if column != "policy" else "left")
    for row in body["policies"]:
        def ci(v):
            return f"[{v[0]}, {v[1]}]" if v else "-"
        table.add_row(
            row["policy"], row["day_one_mean"], ci(row["day_one_ci95"]),
            row["day_two_mean"], ci(row["day_two_ci95"]),
            "yes" if row["day_two_mean_negative"] else "NO",
            "yes" if row["inside_day_one_interval"] else "no",
            "YES" if row["day_two_excludes_zero_above"] else "no",
        )
    console.print(table)
    console.print(f"\n[bold]{body['verdict']}[/bold]: {body['why']}")
    console.print(f"not pooled: {body['not_pooled_because']}")

    target = out or (day_two / "replication_report.json")
    target.write_text(json.dumps(body, indent=2) + "\n")
    console.print(f"wrote {target}")


@app.command("maker-study")
def maker_study(
    directory: Path = typer.Argument(..., help="Study directory written by `pmvl record sessions`."),
    size: int = typer.Option(50, help="Contracts per quote."),
    lifetime: int = typer.Option(60, help="Seconds a quote rests before cancellation."),
    latency_ms: int = typer.Option(300, help="Delay before the order rests on the book."),
    max_skew_ms: float = typer.Option(
        0.0,
        help="Sensitivity: drop snapshots whose skew bound exceeds this. 0 keeps all.",
    ),
    min_session_snapshots: int = typer.Option(
        0,
        help="Sensitivity: drop sessions that captured fewer snapshots than this.",
    ),
    out: Path = typer.Option(None, help="Where to write the pooled report."),
) -> None:
    """Pool every session and resample sessions and dates, not quotes.

    The two sensitivity options default to off, so the bare command is the
    pre-registered analysis and nothing else. Passing either produces a *labelled*
    sensitivity run: the report records what was dropped and why, so a filtered
    result can never be mistaken for the primary one.

    The pre-registration decides once, at the planned session count. Run before then
    and this prints interim numbers labelled as not a decision, because a study that
    can stop the moment it likes an interim is a search with extra steps.
    """
    manifest_path = directory / "study.json"
    if not manifest_path.exists():
        console.print(f"[red]no study manifest at {manifest_path}")
        raise typer.Exit(1)
    manifest = json.loads(manifest_path.read_text())

    policies = [
        QuotePolicy(
            side=Side(side_name), improve_ticks=ticks, size=Decimal(size),
            lifetime_seconds=lifetime, placement_latency_ms=latency_ms,
            name=f"{'improve' if ticks else 'join'}-{side_name}",
        )
        for side_name in ("yes", "no")
        for ticks in (0, 1)
    ]

    results: list[SessionResult] = []
    versions: set[str] = set()
    dropped_snapshots = 0
    dropped_sessions: list[int] = []
    for record in manifest["sessions"]:
        session_dir = directory / f"session-{record['session']:02d}"
        if not (session_dir / "snapshots.jsonl").exists():
            console.print(
                f"session {record['session']}: no snapshots recorded - counted as a "
                "session with zero quotes, not skipped"
            )
            continue
        recording = load_recording(session_dir)
        if max_skew_ms > 0:
            kept = [s for s in recording.snapshots if float(s.skew_bound_ms) <= max_skew_ms]
            dropped_snapshots += len(recording.snapshots) - len(kept)
            recording.snapshots = kept
        if min_session_snapshots and len(recording.snapshots) < min_session_snapshots:
            dropped_sessions.append(record["session"])
            continue
        version = recording.single_version
        if version is None:
            console.print(
                f"[red]session {record['session']} spans strategy versions "
                f"{sorted(recording.strategy_versions)}; refusing to pool it"
            )
            raise typer.Exit(1)
        versions.add(version)
        for policy in policies:
            results.append(
                SessionResult(
                    session=record["session"],
                    date=record["date"],
                    started_at=record["started_at"],
                    report=run_experiment(
                        recording.snapshots, recording.trades, policy,
                        strategy_version=version,
                    ),
                )
            )

    if len(versions) > 1:
        console.print(f"[red]study spans strategy versions {sorted(versions)}")
        raise typer.Exit(1)
    if not results:
        console.print("[red]no session in this study carries a recording")
        raise typer.Exit(1)

    study = pool_sessions(results, planned_sessions=manifest["planned_sessions"])
    body = study.as_dict()
    filtered = bool(max_skew_ms) or bool(min_session_snapshots)
    body["analysis"] = (
        "pre-registered primary"
        if not filtered
        else "SENSITIVITY - not the pre-registered primary analysis"
    )
    body["filters"] = {
        "max_skew_ms": max_skew_ms or None,
        "min_session_snapshots": min_session_snapshots or None,
        "snapshots_dropped": dropped_snapshots,
        "sessions_dropped": dropped_sessions,
    }
    if filtered:
        console.print(
            f"[yellow]SENSITIVITY RUN - dropped {dropped_snapshots} snapshots and "
            f"sessions {dropped_sessions}; this is not the pre-registered analysis"
        )

    table = Table(
        title=f"maker study - {body['completed_sessions']}/{body['planned_sessions']} "
        f"sessions over {body['distinct_dates']} date(s)"
    )
    for column in ("policy", "quotes", "fills", "mean E[PnL]/quote",
                   "session CI95", "date CI95", "family-wise (date)",
                   "hedged pair", "markout 60s", "markets", "overlap"):
        table.add_column(column, justify="right" if column != "policy" else "left")
    for row in body["policies"]:
        def fmt(ci):
            return f"[{ci[0]}, {ci[1]}]" if ci else "-"
        table.add_row(
            row["policy"]["name"],
            str(row["quotes_placed"]),
            str(row["fills"]["QUEUE_MODEL"]),
            row["mean_pnl_per_quote_usd"],
            fmt(row["session_cluster_ci95"]),
            fmt(row["date_cluster_ci95"]),
            fmt(row["date_cluster_ci_family_wise"]),
            str(row["hedged_pair_pnl_per_contract"]),
            str(row["mean_markout_given_fill_60s"]),
            str(row["distinct_markets_across_sessions"]),
            str(row["mean_consecutive_session_overlap"]),
        )
    console.print(table)
    console.print(f"\n[bold]{body['verdict']}[/bold]: {body['why']}")
    console.print(body["independence_note"])

    default_name = (
        "maker_study_report.json"
        if not filtered
        else f"maker_study_sensitivity_skew{int(max_skew_ms)}_min{min_session_snapshots}.json"
    )
    target = out or (directory / default_name)
    target.write_text(json.dumps(body, indent=2) + "\n")
    console.print(f"wrote {target}")

    if not filtered:
        #: One row per quote per policy, session and date attached. The unfilled ones
        #: are most of the file and all of the denominator; a summary alone would make
        #: the fill rate unrecoverable from the artifact, and the losers unverifiable.
        ledger = directory / "study_quote_ledger.csv"
        with ledger.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["policy", "session", "date", "market", "event", "placed_at",
                 "price", "size", "queue_ahead", "spread",
                 "at_level_volume", "through_volume",
                 *[f"filled_{e.value}" for e in FillEstimator],
                 *[f"markout_{h}s" for h in MARKOUT_HORIZONS],
                 "hedge_cost", "liquidation_pnl"]
            )
            rows = 0
            for result in results:
                for outcome in result.report.outcomes:
                    q = outcome.quote
                    writer.writerow(
                        [result.report.policy.name, result.session, result.date,
                         q.market, q.event_ticker, q.placed_at.isoformat(),
                         q.price, q.size, q.queue_ahead, q.spread_at_placement,
                         outcome.relevant_volume_at_level, outcome.relevant_volume_through,
                         *[outcome.filled_size[e] for e in FillEstimator],
                         *[outcome.markout[h] for h in MARKOUT_HORIZONS],
                         outcome.hedge_cost, outcome.liquidation_pnl]
                    )
                    rows += 1
        console.print(f"wrote {ledger} ({rows} quotes across all policies and sessions)")


@app.command("dislocation")
def dislocation(
    directory: Path = typer.Argument(Path("artifacts/forward"), help="Recording directory."),
    ceiling: int = typer.Option(10000, help="Maximum sets considered per observation."),
    structure: str = typer.Option(
        "implication",
        help="'implication' across ladder rungs, or 'complete-set' inside one market.",
    ),
    out: Path = typer.Option(None, help="Where to write the report."),
) -> None:
    """How often a synchronized dislocation clears 1c through 5c."""
    recording, version = _load(directory)
    if structure == "complete-set":
        report = harvest(
            recording.snapshots, strategy_version=version, ceiling=Decimal(ceiling)
        )
    elif structure == "implication":
        if not recording.markets:
            console.print(
                "[red]this recording has no markets.jsonl, so no rung's rules are "
                "known and no implication can be proven from it"
            )
            raise typer.Exit(1)
        report = harvest_implication_pairs(
            recording.snapshots,
            recording.markets,
            strategy_version=version,
            ceiling=Decimal(ceiling),
        )
    else:
        raise typer.BadParameter("structure must be 'implication' or 'complete-set'")
    body = report.as_dict()
    body["structure_requested"] = structure

    table = Table(title=f"synchronized dislocation - {structure} (v{version})")
    for column in ("threshold", "clearing", "near miss", "rate", "episodes",
                   "survive seq", "median dur s", "max capacity", "max net"):
        table.add_column(column, justify="right" if column != "threshold" else "left")
    for cents in THRESHOLDS_CENTS:
        row = body["thresholds"][str(cents)]
        table.add_row(
            f"{cents}c",
            str(row["observations_clearing"]),
            str(row["near_misses_within_one_cent"]),
            row["observation_rate"],
            str(row["episodes"]),
            str(row["episodes_surviving_sequential_execution"]),
            str(row["median_duration_seconds"]),
            row["max_capacity_sets"],
            row["max_net_after_fees_usd"],
        )
    console.print(table)
    console.print(f"gross edge, all pairs:      {body['gross_edge_per_set_percentiles']}")
    if body.get("gross_edge_adjacent_rungs_only"):
        console.print(
            f"gross edge, adjacent rungs: {body['gross_edge_adjacent_rungs_only']}"
        )
    console.print(
        f"{body['market_observations']:,} market observations over "
        f"{body['markets']} markets, {body['event_days']} event-days "
        f"across {body['distinct_dates']} date(s)"
    )

    target = out or (directory / f"dislocation_report_{structure}.json")
    target.write_text(json.dumps(body, indent=2) + "\n")

    episodes = directory / f"dislocation_episodes_{structure}.csv"
    with episodes.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["market", "event", "threshold_cents", "started_at", "ended_at",
             "duration_seconds", "snapshots", "peak_gross_edge", "peak_capacity_sets",
             "peak_net_after_fees", "capital_required", "max_skew_bound_ms",
             "sequential_net_floor", "failure_rescue_floor", "survives_sequencing"]
        )
        for episode in report.episodes:
            row = episode.as_dict()
            writer.writerow([row[k] for k in (
                "market", "event_ticker", "threshold_cents", "started_at", "ended_at",
                "duration_seconds", "snapshots", "peak_gross_edge_per_set",
                "peak_capacity_sets", "peak_net_after_fees_usd", "capital_required_usd",
             "max_skew_bound_ms",
                "sequential_net_floor_usd", "failure_rescue_floor_usd",
                "survives_sequencing")])

    ledger = directory / f"observation_ledger_{structure}.csv"
    with ledger.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["market", "event", "observed_at", "yes_ask", "no_ask", "gross_edge_per_set",
             "top_of_book_sets", "rung_gap",
             *[f"capacity_{c}c" for c in THRESHOLDS_CENTS],
             *[f"net_{c}c" for c in THRESHOLDS_CENTS],
             *[f"capital_{c}c" for c in THRESHOLDS_CENTS]]
        )
        for obs in report.observations:
            writer.writerow(
                [obs.market, obs.event_ticker, obs.observed_at.isoformat(),
                 obs.yes_ask, obs.no_ask, obs.gross_edge_per_set, obs.top_of_book_sets,
                 obs.rung_gap,
                 *[obs.capacity_sets[c] for c in THRESHOLDS_CENTS],
                 *[obs.net_after_fees[c] for c in THRESHOLDS_CENTS],
                 *[obs.capital_required[c] for c in THRESHOLDS_CENTS]]
            )
    console.print(
        f"wrote {target}\nwrote {episodes} ({len(report.episodes)} episodes)\n"
        f"wrote {ledger} ({len(report.observations)} observations, every one kept)"
    )
