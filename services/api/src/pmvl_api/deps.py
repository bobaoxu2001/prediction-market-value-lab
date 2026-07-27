"""Shared API dependencies, serialisation and the provenance guard."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterator

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from pmvl_shared.config import Settings, get_settings
from pmvl_shared.db import get_session_factory
from pmvl_shared.enums import DataProvenance

UTC = timezone.utc


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def settings_dep() -> Settings:
    return get_settings()


class DataMode(str, Enum):
    """Which provenance the caller wants.

    ``live`` is the default everywhere. Demo rows are only ever returned when the
    caller explicitly asks for them, so there is no way for a production page to
    render synthetic data by omission or by forgetting a filter.
    """

    LIVE = "live"
    DEMO = "demo"
    ALL = "all"

    @property
    def provenances(self) -> tuple[str, ...] | None:
        if self is DataMode.LIVE:
            return (DataProvenance.LIVE.value, DataProvenance.FIXTURE.value)
        if self is DataMode.DEMO:
            return (DataProvenance.DEMO.value,)
        return None  # ALL - no filter


def data_mode(
    mode: DataMode = Query(
        DataMode.LIVE,
        description=(
            "Provenance filter. 'live' (default) returns only real venue data. "
            "'demo' returns synthetic illustrative data, which is never a real "
            "opportunity. 'all' mixes both and is intended for debugging."
        ),
    )
) -> DataMode:
    return mode


def apply_provenance(stmt, column, mode: DataMode):  # noqa: ANN001, ANN201
    """Attach the provenance filter to a select statement."""
    allowed = mode.provenances
    if allowed is None:
        return stmt
    return stmt.where(column.in_(allowed))


def jsonable(value: Any) -> Any:
    """Serialise Decimals as strings so no precision is lost in transit.

    Sending a Decimal as a JSON number would hand the browser a float and quietly
    reintroduce exactly the representation error the backend is built to avoid. The
    frontend formats these strings for display and never does arithmetic on money.

    Datetimes are always emitted with an explicit ``Z``. SQLite drops tzinfo on
    round-trip, so rows loaded from the database carry naive datetimes; emitting one
    bare makes ``new Date(...)`` in the browser interpret it as **local** time, and
    every timestamp on the site is then wrong by the viewer's UTC offset. A market
    resolving at 03:05Z was rendering as "8h 29m ago" for a viewer at UTC+8. The
    database stores UTC by contract, so a naive value is UTC and is labelled as such.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


DISCLAIMER = (
    "Research and information only. Not investment advice, not a solicitation, and "
    "not an offer to trade. This platform is read-only: it holds no funds, stores no "
    "wallet keys, and places no orders. Past or simulated performance does not "
    "indicate future results."
)


def demo_notice(mode: DataMode) -> str | None:
    if mode is DataMode.LIVE:
        return None
    return (
        "This response contains SYNTHETIC DEMO DATA (provenance=demo). These are not "
        "real markets, prices, opportunities, or results. Demo rows exist so the "
        "backtest and track-record surfaces can be reviewed before live "
        "recommendations have had time to settle."
    )


def envelope(data: Any, mode: DataMode, **extra: Any) -> dict[str, Any]:
    """Standard response wrapper carrying the provenance contract."""
    payload: dict[str, Any] = {
        "data": jsonable(data),
        "data_mode": mode.value,
        "disclaimer": DISCLAIMER,
    }
    notice = demo_notice(mode)
    if notice:
        payload["demo_notice"] = notice
    payload.update({k: jsonable(v) for k, v in extra.items()})
    return payload


DbDep = Depends(get_db)
ModeDep = Depends(data_mode)
SettingsDep = Depends(settings_dep)
