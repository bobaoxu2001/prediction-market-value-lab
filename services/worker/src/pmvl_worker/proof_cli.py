"""``pmvl proof`` - the adversarial audit, run as a command.

This exists to make the headline claim hard to overstate. Every gate is evaluated
against stored evidence, the verdict is the highest rung every gate permits, and the
gates that blocked it are printed next to it. A run that cannot answer a gate prints
NOT_ESTABLISHED rather than omitting the line, because a missing check and a passed
check must never look the same.
"""

from __future__ import annotations

import csv
import json
import subprocess
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console

from pmvl_markets.arbitrage.profit_proof import (
    Gate,
    GateResult,
    ProofLevel,
    classify,
)
from pmvl_shared.timeutil import iso, utcnow

app = typer.Typer(add_completion=False, help="Adversarial proof of the profit claim.")
console = Console()

ARTIFACTS = Path("artifacts/profit_proof")


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return "unknown"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _load_certificates(paths: list[Path]) -> list[dict]:
    out = []
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text())
        for cert in payload.get("certificates", []):
            cert["_source"] = path.name
            out.append(cert)
    return out


def _economic_exposure_id(cert: dict) -> str:
    """Collapse detections that rest on the same books into one economic exposure.

    A ladder of N rungs yields O(N^2) structures over N books, and they are not N^2
    independent opportunities - they compete for the same resting size. Counting them
    separately inflates both the opportunity count and any capacity built from it, so
    the identity is the sorted set of contracts actually traded.
    """
    legs = sorted(leg["contract_key"] for leg in cert.get("legs", []))
    return f"{cert.get('event_key','')}|{'+'.join(legs)}"


@app.command("stable-profit")
def stable_profit(
    scans: list[Path] = typer.Option(
        None, "--scan", help="Exported scan JSON. Repeatable."
    ),
    max_snapshot_skew_seconds: float = typer.Option(1.0),
    min_independent_event_days: int = typer.Option(
        20, help="Pre-registered minimum before repeatability may be claimed"
    ),
    out_dir: Path = typer.Option(ARTIFACTS, help="Where artifacts are written"),
) -> None:
    """Evaluate every gate and print the highest verdict the evidence supports."""
    paths = list(scans or [])
    certs = _load_certificates(paths)
    gates: list[Gate] = []
    metrics: dict[str, object] = {}

    if not certs:
        gates.append(
            Gate("evidence_present", GateResult.NOT_ESTABLISHED,
                 "no scan exports supplied", ProofLevel.STRUCTURAL_PROOF_ONLY)
        )
        bundle = classify(gates)
        _render(bundle, metrics, out_dir)
        return

    # ---- structural -------------------------------------------------------
    proven = [c for c in certs if (c.get("proof") or {}).get("status") == "proven"
              or (c.get("proof") or {}).get("proven") is True]
    gates.append(Gate(
        "structural_proof", GateResult.PASS if proven else GateResult.FAIL,
        f"{len(proven)} of {len(certs)} certificates carry a settlement proof",
        ProofLevel.STRUCTURAL_PROOF_ONLY,
    ))

    # ---- gross ------------------------------------------------------------
    def dec(cert, key, default="0"):
        v = cert.get(key)
        return Decimal(str(v)) if v not in (None, "") else Decimal(default)

    gross_pos = [c for c in proven if dec(c, "payoff_floor_usd") > dec(c, "gross_cost_usd") > 0]
    gates.append(Gate(
        "gross_edge_observed",
        GateResult.PASS if gross_pos else GateResult.FAIL,
        f"{len(gross_pos)} structures priced below their payoff floor before fees",
        ProofLevel.GROSS_ARBITRAGE_OBSERVED,
    ))

    # ---- net --------------------------------------------------------------
    net_pos = [c for c in proven if dec(c, "net_floor_usd") > 0]
    gates.append(Gate(
        "net_edge_after_fees",
        GateResult.PASS if net_pos else GateResult.FAIL,
        f"{len(net_pos)} structures clear the floor after fees",
        ProofLevel.NET_ARBITRAGE_OBSERVED,
    ))

    # ---- executable: simultaneity ----------------------------------------
    skews = [c.get("snapshot_skew_seconds") for c in net_pos]
    measured = [s for s in skews if s is not None]
    simultaneous = [c for c in net_pos
                    if c.get("snapshot_skew_seconds") is not None
                    and c["snapshot_skew_seconds"] <= max_snapshot_skew_seconds]
    if not net_pos:
        gates.append(Gate(
            "simultaneous_quotes", GateResult.NOT_ESTABLISHED,
            "no net-positive structure to test",
            ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED,
        ))
    elif len(measured) < len(net_pos):
        gates.append(Gate(
            "simultaneous_quotes", GateResult.NOT_ESTABLISHED,
            f"only {len(measured)} of {len(net_pos)} net-positive structures "
            "recorded quote timestamps on every leg",
            ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED,
        ))
    else:
        worst = max(measured)
        gates.append(Gate(
            "simultaneous_quotes",
            GateResult.PASS if simultaneous else GateResult.FAIL,
            f"{len(simultaneous)} of {len(net_pos)} net-positive structures have legs "
            f"quoted within {max_snapshot_skew_seconds:.0f}s; worst skew {worst:.0f}s",
            ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED,
        ))

    accepted = [c for c in certs if c.get("accepted")]
    gates.append(Gate(
        "accepted_by_all_execution_gates",
        GateResult.PASS if accepted else GateResult.FAIL,
        f"{len(accepted)} structures passed depth, freshness, simultaneity and sizing",
        ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED,
    ))

    # ---- deduplication ----------------------------------------------------
    by_exposure = defaultdict(list)
    for c in net_pos:
        by_exposure[_economic_exposure_id(c)].append(c)
    metrics["raw_net_positive_detections"] = len(net_pos)
    metrics["deduplicated_economic_exposures"] = len(by_exposure)

    # ---- forward sample ---------------------------------------------------
    days = set()
    for c in accepted:
        for ts in c.get("book_observed_at") or []:
            try:
                days.add((_parse(ts).date().isoformat(), c.get("event_key", "")))
            except Exception:
                pass
    metrics["independent_event_days"] = len(days)
    gates.append(Gate(
        "forward_sample_size",
        GateResult.PASS if len(days) >= min_independent_event_days
        else GateResult.NOT_ESTABLISHED,
        f"{len(days)} independent event-days observed; "
        f"{min_independent_event_days} pre-registered as the minimum",
        ProofLevel.STABLE_PROFIT_EVIDENCE,
    ))

    metrics["certificates"] = len(certs)
    metrics["structurally_proven"] = len(proven)
    metrics["gross_positive"] = len(gross_pos)
    metrics["net_positive"] = len(net_pos)
    metrics["accepted"] = len(accepted)
    metrics["total_guaranteed_profit_usd"] = str(
        sum((dec(c, "net_floor_usd") for c in accepted), Decimal("0"))
    )
    metrics["capital_required_usd"] = str(
        sum((dec(c, "total_cost_usd") for c in accepted), Decimal("0"))
    )
    metrics["worst_skew_among_net_positive_s"] = max(measured) if measured else None

    bundle = classify(gates)
    bundle.metrics = metrics
    _render(bundle, metrics, out_dir)


def _render(bundle, metrics: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print("=" * 64)
    console.print("[bold]STABLE PROFIT PROOF[/bold]")
    console.print("=" * 64)
    console.print(f"Git SHA: {_git_sha()}")
    console.print(f"Evaluated: {iso(utcnow())}\n")

    for gate in bundle.gates:
        colour = {"PASS": "green", "FAIL": "red", "NOT_ESTABLISHED": "yellow"}[
            gate.result.value
        ]
        console.print(
            f"  [{colour}]{gate.result.value:<16}[/{colour}] {gate.name:<34} {gate.detail}"
        )

    console.print("\n" + "-" * 64)
    for key in (
        "certificates", "structurally_proven", "gross_positive", "net_positive",
        "raw_net_positive_detections", "deduplicated_economic_exposures",
        "accepted", "independent_event_days",
        "total_guaranteed_profit_usd", "capital_required_usd",
        "worst_skew_among_net_positive_s",
    ):
        if key in metrics:
            console.print(f"  {key:<38} {metrics[key]}")

    console.print("-" * 64)
    verdict_colour = {
        ProofLevel.STABLE_PROFIT_EVIDENCE: "green",
        ProofLevel.FALSIFIED: "red",
        ProofLevel.INSUFFICIENT_EVIDENCE: "yellow",
    }.get(bundle.verdict, "cyan")
    console.print(f"\nFINAL VERDICT: [{verdict_colour}]{bundle.verdict.value}[/{verdict_colour}]")
    console.print(f"\nMost important reason:\n  {bundle.reason}")
    console.print(f"\nLargest remaining uncertainty:\n  {bundle.largest_uncertainty}")
    console.print("=" * 64)

    (out_dir / "final_verdict.json").write_text(
        json.dumps({**bundle.as_dict(), "git_sha": _git_sha(),
                    "evaluated_at": iso(utcnow())}, indent=2, default=str)
    )
    with (out_dir / "gates.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["gate", "result", "detail", "guards"])
        wr.writeheader()
        for gate in bundle.gates:
            wr.writerow(gate.as_dict())
