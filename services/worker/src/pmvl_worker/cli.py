"""``pmvl`` CLI - every pipeline stage, plus the scheduler."""

from __future__ import annotations

import asyncio
import functools
import json
from typing import Any, Callable

import typer
from rich.console import Console
from rich.table import Table

from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import setup_logging

from . import jobs

app = typer.Typer(
    add_completion=False,
    help="Prediction Market Value Lab - research pipeline. Read-only; never trades.",
)
console = Console()


def _skip_guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """A skip is a successful no-op at the CLI, not a crash."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except jobs.JobSkipped as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return None

    return wrapper


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(payload, default=str))
    else:
        console.print(payload)


@app.callback()
def main() -> None:
    setup_logging()


@app.command()
@_skip_guard
def ingest(
    market_limit: int = typer.Option(None, help="Max markets to fetch across both venues"),
    orderbook_limit: int = typer.Option(None, help="Max orderbooks to fetch"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch live markets, events and orderbooks from Kalshi and Polymarket."""
    result = asyncio.run(
        jobs.job_ingest(market_limit=market_limit, orderbook_limit=orderbook_limit)
    )
    _emit(result, as_json=as_json)


@app.command()
@_skip_guard
def orderbooks(
    limit: int = typer.Option(None), as_json: bool = typer.Option(False, "--json")
) -> None:
    """Refresh orderbooks for already-ingested markets."""
    _emit(asyncio.run(jobs.job_orderbooks(limit=limit)), as_json=as_json)


@app.command()
@_skip_guard
def score(
    limit: int = typer.Option(None), as_json: bool = typer.Option(False, "--json")
) -> None:
    """Run the fair-probability ensemble without publishing recommendations."""
    _emit(asyncio.run(jobs.job_score(limit=limit)), as_json=as_json)


@app.command()
@_skip_guard
def rank(
    limit: int = typer.Option(None), as_json: bool = typer.Option(False, "--json")
) -> None:
    """Score, rank and publish the Top-N for each resolution horizon."""
    _emit(asyncio.run(jobs.job_rank(limit=limit)), as_json=as_json)


@app.command()
@_skip_guard
def arbitrage(as_json: bool = typer.Option(False, "--json")) -> None:
    """Run all five arbitrage scanners."""
    _emit(jobs.job_arbitrage(), as_json=as_json)


@app.command()
@_skip_guard
def settle(
    lookback_days: int = typer.Option(45), as_json: bool = typer.Option(False, "--json")
) -> None:
    """Sync settlement results and grade past recommendations."""
    _emit(asyncio.run(jobs.job_settle(lookback_days=lookback_days)), as_json=as_json)


@app.command()
@_skip_guard
def snapshot(
    force: bool = typer.Option(False, help="Allow a second snapshot on the same day"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Freeze the current recommendation batch into the immutable track record."""
    _emit(jobs.job_snapshot(force=force), as_json=as_json)


@app.command()
@_skip_guard
def backtest(as_json: bool = typer.Option(False, "--json")) -> None:
    """Walk-forward backtest across all strategies."""
    result = jobs.job_backtest()
    if as_json:
        _emit(result, as_json=True)
        return

    table = Table(title="Backtest results (walk-forward, settled snapshots only)")
    for column in ("Strategy", "Recs", "Settled", "Win rate", "ROI", "Brier", "vs market", "Data quality"):
        table.add_column(column)
    for run in result["strategies"]:
        metrics = run.get("metrics", {})
        table.add_row(
            run["strategy"],
            str(run["n_recommendations"]),
            str(run["n_settled"]),
            _fmt(metrics.get("win_rate")),
            _fmt(metrics.get("roi")),
            _fmt(metrics.get("brier_score")),
            _fmt(metrics.get("brier_improvement_vs_market")),
            run["data_quality"],
        )
    console.print(table)
    if result["strategies"] and all(r["n_settled"] == 0 for r in result["strategies"]):
        console.print(
            "[yellow]No settled snapshots yet.[/yellow] Recommendations must reach "
            "their resolution date before the backtest has anything to measure. "
            "Run `pmvl snapshot` daily and `pmvl settle` to accumulate history."
        )


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


@app.command()
def readiness(as_json: bool = typer.Option(False, "--json")) -> None:
    """How far the live track record is from being able to support its claims."""
    from pmvl_markets.backtest import track_record_readiness
    from pmvl_shared.db import session_scope

    with session_scope() as db:
        payload = track_record_readiness(db).as_dict()

    if as_json:
        _emit(payload, as_json=True)
        return

    table = Table(title="Track record accrual")
    for column in ("Milestone", "Have", "Needs", "Remaining", "Projected"):
        table.add_column(column)
    for milestone in payload["milestones"]:
        table.add_row(
            milestone["name"],
            str(milestone["have"]),
            str(milestone["needs"]),
            "-" if milestone["met"] else str(milestone["remaining"]),
            "met" if milestone["met"] else _fmt(milestone.get("projected_date")),
        )
    console.print(table)

    console.print(
        f"published={payload['published_total']} "
        f"settled={payload['settled_recommendations']} "
        f"pending={payload['pending_recommendations']} "
        f"days={payload['days_of_history']} "
        f"rate={_fmt(payload['settled_per_day'])}/day"
    )
    colour = "yellow" if payload["pipeline_stalled"] else "green"
    console.print(f"[{colour}]{payload['summary']}[/{colour}]")
    if payload["pipeline_stalled"]:
        console.print(
            "[dim]Start the clock with `pmvl schedule` as a long-lived process.[/dim]"
        )


@app.command()
@_skip_guard
def calibrate(as_json: bool = typer.Option(False, "--json")) -> None:
    """Fit a calibration map on settled recommendations, or say why not.

    Fitting nothing is a successful run, and the expected one until several weeks
    of real settled history exist.
    """
    payload = jobs.job_calibrate()
    if as_json:
        _emit(payload, as_json=True)
        return

    table = Table(title="Calibration fit")
    table.add_column("Field")
    table.add_column("Value")
    for key in (
        "method", "applied", "n_train", "n_validation",
        "brier_identity", "brier_fitted", "brier_improvement",
        "min_brier_improvement",
    ):
        table.add_row(key, _fmt(payload.get(key)))
    console.print(table)
    colour = "green" if payload.get("applied") else "yellow"
    console.print(f"[{colour}]{payload.get('reason', '')}[/{colour}]")


@app.command()
def retrodict(
    settled_within_days: int = typer.Option(
        60, help="Only markets that settled within this many days"
    ),
    max_markets: int = typer.Option(200, help="Cap on markets sampled"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Replay the models against settled markets: do they beat the market price?

    Distinct from `backtest`, which reads only published snapshots. This reaches
    backwards through live endpoints and its honesty rests on defences rather than
    on structure - see `pmvl_markets.retrodiction.harness`.
    """
    payload = asyncio.run(
        jobs.job_retrodict(
            settled_within_days=settled_within_days, max_markets=max_markets
        )
    )
    if as_json:
        _emit(payload, as_json=True)
        return

    sample = payload.get("sample", {})
    result = payload.get("result", {})

    console.print(
        f"[bold]Sample[/bold]: {sample.get('n_markets', 0)} settled markets, "
        f"YES base rate {_fmt(sample.get('yes_base_rate'))}, "
        f"{sample.get('settled_from') or '-'} to {sample.get('settled_to') or '-'}"
    )

    table = Table(title="Retrodiction - model vs the market's own price")
    for column in ("Split", "N", "Brier (model)", "Brier (market)", "Improvement", "t"):
        table.add_column(column)
    table.add_row(
        "[bold]all[/bold]",
        str(result.get("n_scored_against_market", 0)),
        _fmt(result.get("brier_model")),
        _fmt(result.get("brier_market")),
        _fmt(result.get("brier_improvement_vs_market")),
        "",
    )
    for dimension, group in (result.get("segments") or {}).items():
        for label, block in group.items():
            improvement = block.get("brier_improvement_vs_market")
            wins = (improvement or 0) > 0 and block.get("distinguishable_from_zero")
            name = f"{dimension} {label}"
            table.add_row(
                f"[green]{name}[/green]" if wins else name,
                str(block["n"]),
                _fmt(block["brier_model"]),
                _fmt(block["brier_market"]),
                _fmt(improvement),
                _fmt(block.get("t_statistic")),
            )
    console.print(table)

    search = result.get("segment_search") or {}
    if search:
        winners = search.get("segments_beating_market") or []
        console.print(
            f"[dim]Searched {search.get('segments_examined')} segments; "
            f"~{search.get('expected_false_positives_at_t2')} would clear |t|>2 on "
            f"noise alone.[/dim]"
        )
        console.print(
            f"[green]Beat the market:[/green] {', '.join(winners)}"
            if winners
            else "[yellow]No segment beat the market at |t|>2.[/yellow]"
        )
        console.print(f"[dim]{search.get('note')}[/dim]")

    skips = result.get("skips") or {}
    if skips:
        console.print(
            "[dim]Skipped:[/dim] "
            + ", ".join(f"{reason} ({count})" for reason, count in list(skips.items())[:4])
        )

    interpretation = payload.get("interpretation") or payload.get("note") or ""
    improvement = result.get("brier_improvement_vs_market")
    colour = "green" if (improvement or 0) > 0 else "yellow"
    console.print(f"[{colour}]{interpretation}[/{colour}]")


@app.command()
@_skip_guard
def prune(
    keep_days: int = typer.Option(30), as_json: bool = typer.Option(False, "--json")
) -> None:
    """Drop orderbook snapshots older than the retention window."""
    _emit(jobs.job_prune(keep_days=keep_days), as_json=as_json)


@app.command("collect-forecasts")
@_skip_guard
def collect_forecasts(as_json: bool = typer.Option(False, "--json")) -> None:
    """Collect NWS gridpoint forecasts. This is the product carrying published_at."""
    import asyncio

    _emit(asyncio.run(jobs.job_forecast_observations()), as_json=as_json)


@app.command("collect-observations")
@_skip_guard
def collect_observations(
    limit_per_station: int = typer.Option(36, help="observations requested per station"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Collect settlement-station observations. Research collection only."""
    import asyncio

    _emit(asyncio.run(jobs.job_station_observations(limit_per_station=limit_per_station)),
          as_json=as_json)


@app.command("collect-books")
@_skip_guard
def collect_books(
    max_seconds: int = typer.Option(
        0, help="bound the in-window capture loop; 0 means until the window ends"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Archive weather orderbooks, faster inside an NWS issuance window."""
    import asyncio

    _emit(asyncio.run(jobs.job_research_books(max_seconds=max_seconds)), as_json=as_json)


@app.command("collect-trades")
@_skip_guard
def collect_trades(as_json: bool = typer.Option(False, "--json")) -> None:
    """Archive weather trade prints. Raw archival only; nothing reads them."""
    import asyncio

    _emit(asyncio.run(jobs.job_research_trades()), as_json=as_json)


@app.command("map-stations")
@_skip_guard
def map_stations(as_json: bool = typer.Option(False, "--json")) -> None:
    """Populate markets.settlement_station_id from the venue's settlement rules."""
    _emit(jobs.job_map_settlement_stations(), as_json=as_json)


@app.command("station-audit")
def station_audit(as_json: bool = typer.Option(False, "--json")) -> None:
    """Compare the five places a settlement station is described."""
    from sqlalchemy import select

    from pmvl_shared.db import session_scope

    from pmvl_markets.alpha.station_audit import audit_stations, render, summarise
    from pmvl_markets.db_models import Market, SourceObservationRow
    from pmvl_markets.normalize.weather_stations import RESEARCH_SERIES_PREFIX
    from pmvl_markets.probability.categories.weather import _STATIONS

    with session_scope() as db:
        rows = [
            (r.series_ticker or "", r.settlement_station_id or "",
             r.settlement_rules_raw or "", r.platform_market_id)
            for r in db.scalars(
                select(Market).where(
                    Market.series_ticker.like(f"{RESEARCH_SERIES_PREFIX}%")
                )
            ).all()
        ]
        gridpoints: dict[str, set[str]] = {}
        for station, location in db.execute(
            select(
                SourceObservationRow.settlement_station_id,
                SourceObservationRow.source_location,
            ).distinct()
        ):
            if station and location:
                gridpoints.setdefault(station, set()).add(location)

    report = summarise(
        audit_stations(
            model_stations=_STATIONS,
            market_rows=rows,
            observed_gridpoints={k: sorted(v) for k, v in gridpoints.items()},
        )
    )
    if as_json:
        _emit(report, as_json=True)
        return
    for line in render(report):
        console.print(line)


@app.command("collection-health")
def collection_health(
    days: int = typer.Option(1, help="lookback window in days"),
    as_json: bool = typer.Option(False, "--json"),
    persist: bool = typer.Option(False, "--persist", help="append a snapshot"),
    history: bool = typer.Option(False, "--history", help="show retained snapshots"),
) -> None:
    """Daily collection-health report. Reports coverage; computes no returns."""
    if history:
        from pmvl_shared.research_archive import ResearchArchive, default_archive_path

        archive = ResearchArchive(default_archive_path("."))
        with archive.connect() as con:
            rows = archive.health_history(con, limit=30)
        if as_json:
            _emit({"history": rows}, as_json=True)
            return
        console.print("[bold]retained collection-health snapshots[/bold]")
        for row in rows:
            mark = "PASS" if row["all_gates_passing"] else "FAIL"
            console.print(
                f"  {row['snapshot_date']}  [{mark}]  obs/station-day="
                f"{row['observations_per_station_day']}  books={row['archived_books']}"
                f"  trades={row['archived_trades']}  best_window={row['best_window_books']}"
                f"  unmapped={row['unmapped_markets']}"
            )
        return
    if persist:
        _emit(jobs.job_collection_health(), as_json=as_json)
        return
    from pmvl_markets.alpha.collection_health import build_report, render

    from pmvl_shared.db import session_scope

    with session_scope() as db:
        report = build_report(db, days=days)
    if as_json:
        _emit(report, as_json=True)
        return
    for line in render(report):
        console.print(line)


@app.command("alpha-readiness")
def alpha_readiness(as_json: bool = typer.Option(False, "--json")) -> None:
    """How far the alpha engine is from evaluating a strategy on real history.

    Deliberately a readiness report and not a result. The infrastructure exists;
    the point-in-time data does not yet. Printing a strategy result computed on
    what is currently in the database would be printing a number about demo
    rows, which is the most misleading thing this repository could publish.
    """
    from pmvl_shared.db import session_scope

    from pmvl_markets.alpha.readiness import assess_readiness

    with session_scope() as db:
        report = assess_readiness(db).as_dict()

    if as_json:
        _emit(report, as_json=True)
        return

    console.print(f"[bold]{report['verdict']}[/bold]")
    console.print(report["headline"])
    console.print("")
    for name, value in report["counts"].items():
        console.print(f"  {name:34s} {value}")
    if report["blockers"]:
        console.print("\n[yellow]Blockers:[/yellow]")
        for blocker in report["blockers"]:
            console.print(f"  - {blocker}")
    if report["next_steps"]:
        console.print("\n[cyan]Next:[/cyan]")
        for step in report["next_steps"]:
            console.print(f"  - {step}")


@app.command()
def status() -> None:
    """Show job health and row counts."""
    from sqlalchemy import func, select

    from pmvl_markets.db_models import (
        ArbitrageOpportunity,
        BacktestRun,
        JobRun,
        Market,
        ModelPrediction,
        OrderbookSnapshot,
        Recommendation,
        RecommendationSnapshot,
        Settlement,
    )
    from pmvl_shared.db import session_scope

    settings = get_settings()
    console.print(f"[bold]Environment:[/bold] {settings.environment}")
    console.print(f"[bold]Database:[/bold] {settings.redacted()['database_url']}")

    with session_scope() as db:
        counts = Table(title="Row counts")
        counts.add_column("Table")
        counts.add_column("Rows", justify="right")
        for model in (
            Market, OrderbookSnapshot, ModelPrediction, Recommendation,
            RecommendationSnapshot, ArbitrageOpportunity, Settlement, BacktestRun,
        ):
            counts.add_row(
                model.__tablename__,
                str(db.scalar(select(func.count()).select_from(model))),
            )
        console.print(counts)

        latest = Table(title="Latest job runs")
        for column in ("Job", "Status", "Started", "Duration", "Records", "Error"):
            latest.add_column(column)
        subquery = (
            select(JobRun.job_name, func.max(JobRun.started_at).label("latest"))
            .group_by(JobRun.job_name)
            .subquery()
        )
        rows = db.scalars(
            select(JobRun)
            .join(
                subquery,
                (JobRun.job_name == subquery.c.job_name)
                & (JobRun.started_at == subquery.c.latest),
            )
            .order_by(JobRun.job_name)
        ).all()
        for row in rows:
            latest.add_row(
                row.job_name,
                row.status,
                row.started_at.strftime("%Y-%m-%d %H:%M:%SZ") if row.started_at else "-",
                f"{row.duration_seconds:.1f}s" if row.duration_seconds else "-",
                str(row.records_written),
                (row.error or "")[:60],
            )
        console.print(latest if rows else "[dim]no jobs have run yet[/dim]")


@app.command()
@_skip_guard
def pipeline(
    market_limit: int = typer.Option(None),
    orderbook_limit: int = typer.Option(None),
) -> None:
    """Run the full daily sequence: ingest -> rank -> arbitrage -> snapshot -> settle -> backtest."""
    asyncio.run(jobs.job_ingest(market_limit=market_limit, orderbook_limit=orderbook_limit))
    asyncio.run(jobs.job_rank())
    jobs.job_arbitrage()
    jobs.job_snapshot()
    asyncio.run(jobs.job_settle())
    jobs.job_backtest()
    console.print("[green]pipeline complete[/green] - run `pmvl status` for details")


@app.command("seed-demo")
def seed_demo(
    days: int = typer.Option(45, help="Days of synthetic history"),
    per_day: int = typer.Option(10),
    seed: int = typer.Option(42, help="RNG seed for reproducibility"),
) -> None:
    """Seed a SYNTHETIC, clearly-labelled demo track record.

    Every row is written with provenance=demo and is excluded from production API
    responses unless explicitly requested. This exists so the backtest, calibration
    and track-record pages are reviewable before real recommendations have had time
    to settle. It is not, and must never be presented as, real performance.
    """
    from pmvl_markets.demo import seed_demo_history
    from pmvl_shared.db import session_scope

    with session_scope() as db:
        report = seed_demo_history(db, days=days, per_day=per_day, seed=seed)
    console.print("[yellow]SYNTHETIC DEMO DATA written (provenance=demo)[/yellow]")
    console.print(report.as_dict())


@app.command("purge-demo")
def purge_demo() -> None:
    """Delete every demo-provenance row. Live data is untouched."""
    from pmvl_markets.demo import purge_demo_data
    from pmvl_shared.db import session_scope

    with session_scope() as db:
        removed = purge_demo_data(db)
    console.print(removed)


@app.command()
def schedule() -> None:
    """Run the recurring scheduler in the foreground."""
    from .scheduler import run_scheduler

    run_scheduler()


if __name__ == "__main__":
    app()


@app.command("model-vs-market-readiness")
def model_vs_market_readiness(
    as_json: bool = typer.Option(False, "--json"),
    data_dir: str = typer.Option("data", help="directory holding the two stores"),
    allow_approximation: bool = typer.Option(
        False,
        "--allow-approximation",
        help="admit MODELLING_APPROXIMATION parity (requires explicit policy)",
    ),
) -> None:
    """Whether a Model-vs-Market dataset can be built, and what blocks it.

    Read-only against both stores. Reports evidence and exclusion counts rather
    than a bare verdict, and separates defect-class exclusions (a bug) from
    shortfall-class ones (data has not arrived) - they look identical from a row
    count and need opposite responses.

    Computes no returns, no PnL and no alpha claim.
    """
    import subprocess
    from pathlib import Path

    from pmvl_markets.research.dataset import build_report, render

    commit = ""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        pass

    from pmvl_markets.research.build import build_cohort, target_dates_with_labels
    from pmvl_markets.research.dataset import MIN_EVENTS_FOR_CALIBRATION, cohort_gates

    root = Path(data_dir)
    report = build_report(data_dir=root, code_commit=commit)

    # Assemble every day for which a FINAL settlement bulletin exists.
    merged: dict[str, Any] = {}
    totals = {"contract_observations": 0, "unique_markets": 0}
    events: set[str] = set()
    adm_events: set[str] = set()
    adm_rows = 0
    parity: dict[str, int] = {}
    exclusions: dict[str, int] = {}
    for day in target_dates_with_labels(root):
        cohort = build_cohort(data_dir=root, target_date=day)
        summary = cohort.summary(allow_approximation=allow_approximation)
        totals["contract_observations"] += summary["contract_observations"]
        totals["unique_markets"] += summary["unique_markets"]
        events.update(o.event_key for o in cohort.observations)
        for observation in cohort.admissible(allow_approximation):
            adm_events.add(observation.event_key)
            adm_rows += 1
        for key, n in summary["by_parity"].items():
            parity[key] = parity.get(key, 0) + n
        for key, n in summary["exclusions"].items():
            exclusions[key] = exclusions.get(key, 0) + n
    merged = {
        **totals,
        "independent_settlement_events": len(events),
        "admissible_contract_observations": adm_rows,
        "admissible_independent_events": len(adm_events),
        "by_parity": dict(sorted(parity.items())),
        "exclusions": dict(sorted(exclusions.items(), key=lambda kv: -kv[1])),
    }
    gates = cohort_gates(merged, allow_approximation=allow_approximation)

    # Gates added in 4C0.6. Each pins something that actually went wrong.
    from pmvl_markets.research.protocol import FROZEN_PROTOCOL

    import sqlite3 as _sq

    con = _sq.connect(f"file:{root / 'pmvl.db'}?mode=ro", uri=True)
    try:
        ineligible = con.execute(
            "SELECT COUNT(*) FROM source_observations WHERE research_eligible = 0"
        ).fetchone()[0]
        unknown = con.execute(
            "SELECT COUNT(*) FROM source_observations "
            "WHERE validity_status = 'UNKNOWN'"
        ).fetchone()[0]
        venue_labels = con.execute(
            "SELECT COUNT(*) FROM settlements s JOIN markets m ON m.id = s.market_id "
            "WHERE m.series_ticker LIKE 'KXHIGH%' AND s.result IN ('yes','no')"
        ).fetchone()[0]
        physical = con.execute(
            "SELECT COUNT(*) FROM daily_climate_observations WHERE is_final = 1"
        ).fetchone()[0]
        unresolved_weather = con.execute(
            "SELECT COUNT(*) FROM markets m WHERE m.series_ticker LIKE 'KXHIGH%' "
            "AND m.expected_resolution_time < datetime('now') "
            "AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.market_id = m.id)"
        ).fetchone()[0]
    finally:
        con.close()

    gates = gates + [
        (
            "forecast defects quarantined (UNKNOWN = 0)",
            unknown == 0,
            f"{unknown} UNKNOWN, {ineligible} ineligible excluded from cohorts",
        ),
        (
            "venue terminal settlement coverage",
            unresolved_weather == 0,
            f"{venue_labels} venue labels, {unresolved_weather} weather markets "
            "still unresolved",
        ),
        (
            "physical quantity archived",
            physical > 0,
            f"{physical} final CLI daily maxima",
        ),
        (
            "evaluation protocol frozen",
            True,
            f"{FROZEN_PROTOCOL.version} hash={FROZEN_PROTOCOL.content_hash()} "
            f"offset=T-{FROZEN_PROTOCOL.decision_offset_hours}h "
            f"sigma_fitted={FROZEN_PROTOCOL.sigma_fitted}",
        ),
        (
            "independent unit = station-day",
            FROZEN_PROTOCOL.independent_unit == "station_day",
            FROZEN_PROTOCOL.independent_unit,
        ),
    ]
    authorized = all(ok for _, ok, _ in gates)

    payload = report.as_dict()
    payload["cohort"] = merged
    payload["gates"] = [
        {"gate": name, "passing": ok, "evidence": why} for name, ok, why in gates
    ]
    payload["phase_4c1_authorized"] = authorized
    if as_json:
        _emit(payload, as_json=True)
        return

    console.print(render(report, with_verdict=False))
    console.print("--- FIRST ADMISSIBLE COHORT ---")
    console.print(f"  contract observations           {merged['contract_observations']:>6}")
    console.print(f"  unique markets                  {merged['unique_markets']:>6}")
    console.print(f"  independent settlement events   {merged['independent_settlement_events']:>6}   <- inferential N")
    console.print(f"  admissible contract rows        {adm_rows:>6}")
    console.print(f"  admissible independent events   {len(adm_events):>6}")
    console.print(f"  parity                          {merged['by_parity']}")
    console.print(f"  exclusions                      {merged['exclusions']}")
    console.print("")
    console.print("--- PHASE 4C1 GATES ---")
    for name, ok, why in gates:
        console.print(f"  [{'PASS' if ok else 'FAIL'}] {name:<46} {why}")
    console.print("")
    from pmvl_markets.research.coverage import forecast_coverage, render_coverage

    console.print(render_coverage(forecast_coverage(data_dir=root)))

    from pmvl_markets.research.coverage import (
        render_conversions,
        station_day_conversions,
    )

    console.print(render_conversions(station_day_conversions(data_dir=root)))

    from datetime import datetime, timezone

    from pmvl_markets.research.supervision import (
        collector_health,
        decision_watch,
        detect_gaps,
    )

    health = collector_health(data_dir=root)
    console.print("")
    console.print("--- SUPERVISION ---")
    console.print(
        f"  collector liveness            {health.state.value} "
        f"(capture age {health.capture_age_seconds}s)"
    )
    epoch = None
    try:
        from pmvl_shared.research_archive import ResearchArchive, default_archive_path

        archive = ResearchArchive(default_archive_path("."))
        with archive.connect() as con:
            row = con.execute(
                "SELECT started_at FROM supervision_epochs ORDER BY started_at DESC "
                "LIMIT 1"
            ).fetchone()
        if row:
            epoch = datetime.fromisoformat(row[0])
    except Exception:  # noqa: BLE001
        epoch = None

    gaps = detect_gaps(data_dir=root)
    # Attributed by where a gap STARTS. The outage that the epoch resolved ends
    # after it and is not a failure of the supervised era.
    new_gaps = [g for g in gaps if epoch and g.last_good_capture >= epoch]
    console.print(f"  supervision epoch             {epoch.isoformat() if epoch else 'none'}")
    console.print(f"  collection gaps (all history) {len(gaps)}")
    console.print(f"  gaps started since epoch      {len(new_gaps)}   (must be 0)")

    watch = decision_watch(data_dir=root)
    covered = [w for w in watch if w.status == "DECISION_COVERED"]
    post = [w for w in covered if epoch and w.decision_at >= epoch]
    console.print(f"  decision instants covered     {len(covered)} of {len(watch)}")
    console.print(f"  covered since epoch           {len(post)}")
    console.print("")
    console.print(
        f"CLEAN INDEPENDENT SETTLED EVENTS = {len(adm_events)} / "
        f"{FROZEN_PROTOCOL.minimum_independent_events}"
    )
    console.print("")
    console.print(
        "PHASE 4C1 AUTHORIZED" if authorized else "PHASE 4C1 NOT AUTHORIZED"
    )
