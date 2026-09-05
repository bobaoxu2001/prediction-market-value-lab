"""``pmvl exec`` - operator surface for the Phase 5A execution harness.

There is deliberately no ``live-order`` command, and ``--env production`` is
accepted only for reads. Asking this CLI to mutate a real account produces a
local refusal before any request is built.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from pmvl_markets.execution import (
    ExecutionLedger,
    ExecutionMode,
    KalshiAccountClient,
    KillSwitch,
    LiveTradingNotAuthorized,
    OrderAction,
    OrderIntent,
    Outcome,
    RiskGate,
    credentials_present,
    recover_after_restart,
)
from pmvl_markets.execution.harness import ExecutionHarness
from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

app = typer.Typer(
    add_completion=False,
    help=(
        "Kalshi execution harness. SHADOW / DEMO / PRODUCTION_READ_ONLY only - "
        "real-money order placement is not implemented."
    ),
)
console = Console()
log = get_logger(__name__)

_MODES = {
    "shadow": ExecutionMode.SHADOW,
    "demo": ExecutionMode.DEMO,
    "production": ExecutionMode.PRODUCTION_READ_ONLY,
    "production_read_only": ExecutionMode.PRODUCTION_READ_ONLY,
}


def _mode(value: str) -> ExecutionMode:
    key = (value or "").strip().lower()
    if key not in _MODES:
        raise typer.BadParameter(
            f"unknown mode {value!r}; choose one of {sorted(set(_MODES))}"
        )
    return _MODES[key]


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(payload, default=str))
    else:
        console.print(payload)


def _ledger() -> ExecutionLedger:
    return ExecutionLedger(get_settings().execution_db_path)


def _kill_switch() -> KillSwitch:
    return KillSwitch.from_settings()


def _require_credentials(mode: ExecutionMode) -> None:
    if not credentials_present(mode):
        console.print(
            f"[yellow]BLOCKED_MISSING_CREDENTIALS[/yellow] for mode {mode.value}"
        )
        raise typer.Exit(code=2)


@app.command("account-status")
def account_status(
    mode: str = typer.Option("production", help="shadow | demo | production"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Balance summary. Read-only in every mode."""
    m = _mode(mode)
    _require_credentials(m)

    async def _run() -> dict[str, Any]:
        client = KalshiAccountClient(m)
        try:
            balance = await client.get_balance()
            return {
                "mode": m.value,
                "environment": balance.environment,
                "balance_usd": str(balance.balance_usd),
                "available_usd": str(balance.available_usd)
                if balance.available_usd is not None
                else None,
            }
        finally:
            await client.aclose()

    _emit(asyncio.run(_run()), as_json=as_json)


@app.command()
def positions(
    mode: str = typer.Option("production"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    m = _mode(mode)
    _require_credentials(m)

    async def _run() -> dict[str, Any]:
        client = KalshiAccountClient(m)
        try:
            rows = await client.get_positions()
            return {
                "mode": m.value,
                "positions": [
                    {
                        "ticker": p.ticker,
                        "outcome": p.outcome.value,
                        "quantity": p.quantity,
                        "exposure_usd": str(p.exposure_usd)
                        if p.exposure_usd is not None
                        else None,
                    }
                    for p in rows
                ],
            }
        finally:
            await client.aclose()

    _emit(asyncio.run(_run()), as_json=as_json)


@app.command("open-orders")
def open_orders(
    mode: str = typer.Option("production"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    m = _mode(mode)
    _require_credentials(m)

    async def _run() -> dict[str, Any]:
        client = KalshiAccountClient(m)
        try:
            rows = await client.get_orders(status="resting")
            return {
                "mode": m.value,
                "orders": [
                    {
                        "exchange_order_id": o.exchange_order_id,
                        "client_order_id": o.client_order_id,
                        "ticker": o.ticker,
                        "outcome": o.outcome.value,
                        "action": o.action.value,
                        "status": o.status.value,
                        "limit_price_cents": o.limit_price_cents,
                        "remaining": o.remaining_quantity,
                    }
                    for o in rows
                ],
            }
        finally:
            await client.aclose()

    _emit(asyncio.run(_run()), as_json=as_json)


@app.command()
def fills(
    mode: str = typer.Option("production"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    m = _mode(mode)
    _require_credentials(m)

    async def _run() -> dict[str, Any]:
        client = KalshiAccountClient(m)
        try:
            rows = await client.get_fills()
            return {
                "mode": m.value,
                "fills": [
                    {
                        "fill_id": f.fill_id,
                        "ticker": f.ticker,
                        "outcome": f.outcome.value,
                        "action": f.action.value,
                        "quantity": f.quantity,
                        "price_cents": f.price_cents,
                        "fee_usd": str(f.fee_usd) if f.fee_usd is not None else None,
                    }
                    for f in rows
                ],
            }
        finally:
            await client.aclose()

    _emit(asyncio.run(_run()), as_json=as_json)


def _build_intent(
    mode: ExecutionMode,
    ticker: str,
    outcome: str,
    action: str,
    price_cents: int,
    quantity: int,
    edge: str,
    decision_epoch: str = "",
) -> OrderIntent:
    """Build the intent an operator command describes.

    ``decision_epoch`` is what makes two invocations the *same* logical decision.
    It used to be derived from the wall clock to minute granularity, which meant
    re-running an identical command 60 seconds later minted a fresh
    ``client_order_id`` and placed a SECOND order - exactly the duplicate the
    idempotency module exists to prevent. It is now derived from the order's own
    terms, so a repeated command is a retry (deduplicated) and a deliberate
    second tranche is an explicit ``--epoch``.
    """
    now = utcnow()
    from decimal import Decimal

    decision_epoch = decision_epoch or (
        f"{ticker}:{outcome.lower()}:{action.lower()}:{price_cents}:{quantity}"
    )

    return OrderIntent(
        intent_id=f"cli-{ticker}-{now.strftime('%Y%m%dT%H%M%S')}",
        ticker=ticker,
        outcome=Outcome(outcome.lower()),
        action=OrderAction(action.lower()),
        limit_price_cents=price_cents,
        quantity=quantity,
        mode=mode,
        strategy="operator-cli",
        strategy_version="1",
        decision_epoch=decision_epoch,
        source_signal_id="manual",
        expected_edge_after_fees=Decimal(edge),
        decision_timestamp=now,
        signal_timestamp=now,
        market_data_timestamp=now,
        expires_at=now + timedelta(minutes=5),
        reason="manual operator order",
    )


@app.command("shadow-order")
def shadow_order(
    ticker: str = typer.Argument(...),
    outcome: str = typer.Option("yes", help="yes | no"),
    action: str = typer.Option("buy", help="buy | sell"),
    price_cents: int = typer.Option(..., "--price"),
    quantity: int = typer.Option(1, "--qty"),
    edge: str = typer.Option("0.05", help="expected edge after fees"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the full pipeline and stop before the venue write."""
    intent = _build_intent(
        ExecutionMode.SHADOW, ticker, outcome, action, price_cents, quantity, edge
    )
    harness = ExecutionHarness(
        ExecutionMode.SHADOW,
        ledger=_ledger(),
        risk_gate=RiskGate(kill_switch=_kill_switch()),
    )
    planned = asyncio.run(harness.plan(intent))
    _emit(
        {
            "mode": "shadow",
            "client_order_id": planned.client_order_id,
            "would_submit": planned.would_submit,
            "notional_usd": str(planned.projected_notional_usd),
            "risk": planned.decision.as_dict(),
        },
        as_json=as_json,
    )


@app.command("demo-order")
def demo_order(
    ticker: str = typer.Argument(...),
    outcome: str = typer.Option("yes"),
    action: str = typer.Option("buy"),
    price_cents: int = typer.Option(..., "--price"),
    quantity: int = typer.Option(1, "--qty"),
    edge: str = typer.Option("0.05"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Submit one order to the Kalshi DEMO sandbox."""
    _require_credentials(ExecutionMode.DEMO)
    from pmvl_markets.execution import KalshiExecutionClient

    intent = _build_intent(
        ExecutionMode.DEMO, ticker, outcome, action, price_cents, quantity, edge
    )

    async def _run() -> dict[str, Any]:
        account = KalshiAccountClient(ExecutionMode.DEMO)
        execution = KalshiExecutionClient(ExecutionMode.DEMO)
        harness = ExecutionHarness(
            ExecutionMode.DEMO,
            ledger=_ledger(),
            risk_gate=RiskGate(kill_switch=_kill_switch()),
            account_client=account,
            execution_client=execution,
        )
        try:
            result = await harness.submit(intent)
            return {
                "mode": "demo",
                "client_order_id": result.client_order_id,
                "status": result.status.value,
                "exchange_order_id": result.exchange_order_id,
                "error": result.error,
            }
        finally:
            await account.aclose()
            await execution.aclose()

    _emit(asyncio.run(_run()), as_json=as_json)


@app.command("demo-cancel")
def demo_cancel(
    client_order_id: str = typer.Argument(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    _require_credentials(ExecutionMode.DEMO)
    from pmvl_markets.execution import KalshiExecutionClient

    async def _run() -> dict[str, Any]:
        execution = KalshiExecutionClient(ExecutionMode.DEMO)
        harness = ExecutionHarness(
            ExecutionMode.DEMO,
            ledger=_ledger(),
            risk_gate=RiskGate(kill_switch=_kill_switch()),
            execution_client=execution,
        )
        try:
            result = await harness.cancel(client_order_id)
            return {
                "client_order_id": result.client_order_id,
                "status": result.status.value,
                "already_resolved": result.already_resolved,
                "error": result.error,
            }
        finally:
            await execution.aclose()

    _emit(asyncio.run(_run()), as_json=as_json)


@app.command()
def reconcile(
    mode: str = typer.Option("demo"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare the local ledger against the venue. Never trades to fix a gap."""
    m = _mode(mode)
    _require_credentials(m)

    async def _run() -> dict[str, Any]:
        client = KalshiAccountClient(m)
        try:
            orders = await client.get_orders()
            fill_rows = await client.get_fills()
            position_rows = await client.get_positions()
        finally:
            await client.aclose()
        report = recover_after_restart(
            _ledger(), orders, fill_rows, position_rows, mode=m
        )
        return {
            "mode": m.value,
            "summary": report.summary(),
            "clean": report.is_clean,
            "blocking": [
                {
                    "kind": d.kind.value,
                    "client_order_id": d.client_order_id,
                    "ticker": d.ticker,
                    "detail": d.detail,
                }
                for d in report.blocking
            ],
        }

    _emit(asyncio.run(_run()), as_json=as_json)


@app.command()
def kill(reason: str = typer.Option("operator request")) -> None:
    """Engage the persistent kill switch. Blocks new orders in every mode."""
    path = Path(get_settings().execution_kill_switch_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{utcnow().isoformat()} {reason}\n", encoding="utf-8")
    console.print(f"[red]EXECUTION DISABLED[/red] via {path}")


@app.command("un-kill-demo")
def un_kill_demo(
    confirm: bool = typer.Option(False, "--confirm", help="required"),
) -> None:
    """Remove the persistent kill flag.

    Named for demo on purpose: clearing it cannot enable production writes,
    because no mode can perform them.
    """
    if not confirm:
        raise typer.BadParameter("pass --confirm to clear the kill switch")
    path = Path(get_settings().execution_kill_switch_path)
    if path.exists():
        path.unlink()
        console.print(f"[yellow]kill flag removed[/yellow] ({path})")
    else:
        console.print("no kill flag present")


@app.command()
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """What this build can and cannot do. Reads no credentials."""
    settings = get_settings()
    switch = _kill_switch()
    _emit(
        {
            "modes_available": [m.value for m in ExecutionMode],
            "live_autonomous_trading_authorized": False,
            "production_write_blocked": True,
            "execution_enabled": settings.execution_enabled,
            "kill_switch_engaged": switch.engaged,
            "kill_switch_reason": switch.reason,
            "demo_credentials_present": credentials_present(ExecutionMode.DEMO),
            "production_credentials_present": credentials_present(
                ExecutionMode.PRODUCTION_READ_ONLY
            ),
            "execution_db": settings.execution_db_path,
        },
        as_json=as_json,
    )


# There is deliberately no `live-order` command. Not a stub, not a command that
# raises - nothing. A refusing stub would still be a live-order entry point in
# `--help`, and the correct signal to an operator is that the capability does not
# exist in this build at all.


@app.command("shadow-replay")
def shadow_replay(
    limit: int = typer.Option(500, help="How many research decisions to read."),
    allow_demo: bool = typer.Option(
        False,
        "--allow-demo-provenance",
        help="Admit provenance='demo' seed rows. They are NOT model output.",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run the research record through the real risk path. Sends nothing.

    Reads `recommendations` read-only and stops at the shadow gateway. Every
    decision is counted, including the refusals - the denominator is the point.
    """
    import asyncio

    from pmvl_markets.execution import (
        ExecutionMode,
        RiskGate,
        ShadowSignalPipeline,
        SignalExecutionAdapter,
        load_research_decisions,
    )

    settings = get_settings()
    # Read-only path to the research DB, derived from the configured URL. The
    # execution layer consumes research output and must never write to it.
    url = settings.database_url
    db_path = url.split("://", 1)[1] if "://" in url else url
    db_path = "/" + db_path.lstrip("/")
    decisions = load_research_decisions(db_path, limit=limit)
    pipeline = ShadowSignalPipeline(
        ledger=_ledger(),
        adapter=SignalExecutionAdapter(
            mode=ExecutionMode.SHADOW, allow_demo_provenance=allow_demo
        ),
        risk_gate=RiskGate(kill_switch=_kill_switch()),
    )
    report = asyncio.run(pipeline.run(decisions))
    payload = report.as_dict()
    payload["demo_provenance_admitted"] = allow_demo
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(payload)


@app.command("reconcile-order")
def reconcile_order(
    client_order_id: str = typer.Argument(...),
    mode: str = typer.Option("demo", help="demo | production"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Resolve one ambiguous order using venue evidence only.

    There is deliberately no flag that clears an order without evidence. If the
    venue is still ambiguous the order stays blocked, and that is the feature.
    """
    import asyncio

    from pmvl_markets.execution import ExecutionMode, KalshiAccountClient
    from pmvl_markets.execution.reconcile import resolve_unknown_order

    target = (
        ExecutionMode.PRODUCTION_READ_ONLY
        if mode.lower().startswith("prod")
        else ExecutionMode.DEMO
    )
    _require_credentials(target)

    async def fetch() -> tuple[Any, list[Any]]:
        client = KalshiAccountClient(target)
        try:
            orders = await client.get_orders()
            fills = await client.get_fills()
        finally:
            await client.aclose()
        match = [o for o in orders if o.client_order_id == client_order_id]
        found = match[0] if match else None
        # Fills carry no client_order_id - the venue joins them by its own order
        # id, which is how reconcile.py matches them too. Filtering on a field
        # `Fill` does not have raised AttributeError before resolve_unknown_order
        # was ever reached, so the only sanctioned exit from UNKNOWN could not run.
        exchange_id = found.exchange_order_id if found else None
        if exchange_id is None:
            with _ledger().connect() as con:
                record = _ledger().get(con, client_order_id)
            exchange_id = record.exchange_order_id if record else None
        related = (
            [f for f in fills if f.exchange_order_id == exchange_id]
            if exchange_id
            else []
        )
        return found, related

    remote_order, remote_fills = asyncio.run(fetch())
    result = resolve_unknown_order(
        _ledger(),
        client_order_id,
        mode=target,
        remote_order=remote_order,
        remote_fills=remote_fills,
    )
    if json_out:
        console.print_json(data=result.as_dict())
    else:
        console.print(result.as_dict())



@app.command("discrepancies")
def discrepancies(
    mode: str = typer.Option("demo", help="demo | production | shadow"),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Clear this mode's open discrepancies after investigating them.",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List, and optionally clear, blocking reconciliation discrepancies.

    Clearing is an operator act with an audit trail, not an automatic recovery:
    a discrepancy means the venue held exposure this system did not know about,
    and only a person can say they have looked. Without this command the rows
    were write-only - one reconciliation against an account holding any position
    left a permanent block with no remedy short of hand-editing execution.db.
    """
    target = _mode(mode)
    ledger = _ledger()
    with ledger.connect() as con:
        rows = [dict(r) for r in ledger.open_blocking_discrepancies(con, mode=target)]
        cleared = (
            ledger.clear_blocking_discrepancies(con, mode=target) if clear else 0
        )
    payload = {
        "mode": target.value,
        "open": rows,
        "cleared": cleared,
    }
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(payload)


@app.command("demo-arm")
def demo_arm(
    minutes: int = typer.Option(30, help="How long the arm stays valid."),
    reason: str = typer.Option(..., help="Why, for the audit trail."),
    confirm: bool = typer.Option(False, "--confirm", help="required"),
) -> None:
    """Permit DEMO sandbox writes for a bounded window.

    This is not a safety switch. It satisfies the *configuration* arm of the kill
    switch for DEMO only, and only until it expires - an operator who forgets to
    disarm gets a system that disarms itself. `pmvl exec kill` still overrides it
    absolutely, and nothing here can enable a production write, because no
    execution mode can perform one.
    """
    from datetime import timedelta

    if not confirm:
        raise typer.BadParameter("pass --confirm to arm demo execution")
    if minutes <= 0 or minutes > 240:
        raise typer.BadParameter("arm window must be between 1 and 240 minutes")

    expiry = utcnow() + timedelta(minutes=minutes)
    path = Path(get_settings().execution_demo_arm_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{expiry.isoformat()} {reason}\n", encoding="utf-8")
    log.warning(
        "DEMO execution armed until %s by operator: %s", expiry.isoformat(), reason
    )
    console.print(
        f"[yellow]DEMO writes armed[/yellow] until {expiry.isoformat()} ({path})"
    )


@app.command("demo-disarm")
def demo_disarm() -> None:
    """Revoke the demo arm immediately."""
    path = Path(get_settings().execution_demo_arm_path)
    if path.exists():
        path.unlink()
        log.warning("DEMO execution disarmed by operator")
        console.print("[green]demo arm removed[/green]")
    else:
        console.print("no demo arm present")
