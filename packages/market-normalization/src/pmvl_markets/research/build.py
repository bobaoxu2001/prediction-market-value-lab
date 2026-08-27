"""Deterministic cohort construction from the immutable stores.

Read-only against both databases: research must never mutate the archive it is
measuring. Given the same stores and the same parameters this returns the same
rows, so a result can be re-derived rather than trusted.

The decision instant is a parameter, not a search. Choosing it after seeing
outcomes - "the timestamp where the edge looks best" - is the cheapest way to
manufacture a result, so it is fixed as an offset before market close and the
offset is recorded on every row.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .cohort import Cohort, ContractObservation, event_key_for
from .contract import ContractParseError, parse_contract
from .exclusions import Exclusion
from .parity import classify
from .pit import select_forecast_as_of, select_orderbook_as_of
from .prices import MarketState
from .probability import contract_probability

#: How long before market close the observation is taken. Fixed, not searched.
DEFAULT_DECISION_OFFSET_HOURS = 6.0


def _utc(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace(" ", "T"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _dec(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class _Rec:
    """Adapter giving a sqlite row the ``available_at`` the PIT layer needs."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.__dict__.update(dict(row))
        self.available_at = _utc(row["available_at"])


def build_cohort(
    *,
    data_dir: Path,
    target_date: date,
    decision_offset_hours: float = DEFAULT_DECISION_OFFSET_HOURS,
) -> Cohort:
    """Assemble every admissible observation for one target weather day."""
    cohort = Cohort()
    op_path, ar_path = data_dir / "pmvl.db", data_dir / "research-archive.db"
    if not op_path.exists() or not ar_path.exists():
        return cohort

    op = sqlite3.connect(f"file:{op_path}?mode=ro", uri=True)
    ar = sqlite3.connect(f"file:{ar_path}?mode=ro", uri=True)
    op.row_factory = ar.row_factory = sqlite3.Row
    try:
        # Settlement labels come from FINAL CLI bulletins only. A preliminary
        # report carries the maximum so far and would bias every label downward,
        # worst on the hot afternoons where a threshold sits near its strike.
        # The independent PHYSICAL quantity - audit evidence, not the label.
        physical: dict[str, tuple[Decimal, datetime, str]] = {}
        for row in op.execute(
            "SELECT station_id, daily_max_f, available_at, product_code "
            "FROM daily_climate_observations "
            "WHERE is_final = 1 AND observation_date = ? AND is_backfill = 0 "
            "ORDER BY available_at",
            (target_date.isoformat(),),
        ):
            value = _dec(row["daily_max_f"])
            if value is not None:
                physical.setdefault(
                    row["station_id"], (value, _utc(row["available_at"]), row["product_code"])
                )

        # The LABEL is the venue's own terminal result.
        #
        # Which side paid is a fact about the contract, and the venue is the only
        # authority on it. Deriving it from the archived CLI maximum instead
        # would make the label depend on a source the contract does not settle
        # on, and would quietly convert an independent audit measurement into the
        # thing it is supposed to audit.
        venue: dict[str, tuple[str, datetime]] = {}
        for row in op.execute(
            "SELECT m.platform_market_id, s.result, s.settled_at "
            "FROM settlements s JOIN markets m ON m.id = s.market_id "
            "WHERE m.series_ticker LIKE 'KXHIGH%' AND s.result IN ('yes','no')"
        ):
            venue[row["platform_market_id"]] = (
                row["result"], _utc(row["settled_at"])
            )

        stamp = target_date.strftime("%y%b%d").upper()
        markets = [
            dict(r)
            for r in op.execute(
                "SELECT * FROM markets WHERE series_ticker LIKE 'KXHIGH%' "
                "AND platform_market_id LIKE ?",
                (f"%-{stamp}-%",),
            )
        ]

        for market in markets:
            station = market["settlement_station_id"]
            if not station:
                cohort.exclude(Exclusion.STATION_UNMAPPED)
                continue
            try:
                spec = parse_contract(
                    strike_type=market["strike_type"],
                    floor_strike=market["floor_strike"],
                    cap_strike=market["cap_strike"],
                )
            except ContractParseError:
                cohort.exclude(Exclusion.CONTRACT_PARSE_FAILED)
                continue

            terminal = venue.get(market["platform_market_id"])
            if terminal is None:
                cohort.exclude(Exclusion.NO_SETTLEMENT)
                continue
            venue_result, settled_available_at = terminal

            measured = physical.get(station)
            if measured is None:
                # No independent physical observation to audit against. The
                # venue label alone is enough to score a contract, but a
                # station-day without it cannot support parity work, so it is
                # refused rather than admitted half-provenanced.
                cohort.exclude(Exclusion.NO_SETTLEMENT)
                continue
            settled_value, _physical_available_at, product = measured

            if not market["close_time"]:
                cohort.exclude(Exclusion.TARGET_DATE_MISMATCH)
                continue
            decision_at = _utc(market["close_time"]) - timedelta(
                hours=decision_offset_hours
            )

            books = [
                _Rec(r)
                for r in ar.execute(
                    "SELECT * FROM archived_books WHERE platform_market_id = ? "
                    "ORDER BY available_at",
                    (market["platform_market_id"],),
                )
            ]
            chosen_book = select_orderbook_as_of(books, cutoff=decision_at)
            if not chosen_book.ok:
                cohort.exclude(chosen_book.reason)
                continue

            forecasts = [
                _Rec(r)
                for r in op.execute(
                    # research_eligible only: excludes both the superseded
                    # originals of the target-day defect and anything whose
                    # eligibility was never established (which defaults to 0, so
                    # an unaudited row is refused rather than assumed fine).
                    "SELECT * FROM source_observations WHERE settlement_station_id = ? "
                    "AND date(forecast_valid_start) = ? AND research_eligible = 1 "
                    "ORDER BY available_at",
                    (station, target_date.isoformat()),
                )
            ]
            chosen_forecast = select_forecast_as_of(forecasts, cutoff=decision_at)
            if not chosen_forecast.ok:
                cohort.exclude(chosen_forecast.reason)
                continue

            book, forecast = chosen_book.record, chosen_forecast.record
            state = MarketState(
                _dec(book.__dict__.get("best_yes_bid")),
                _dec(book.__dict__.get("best_yes_ask")),
                _dec(book.__dict__.get("best_no_bid")),
                _dec(book.__dict__.get("best_no_ask")),
            )
            invalid = state.validate()
            if invalid is not None:
                cohort.exclude(invalid)
                continue

            forecast_value = _dec(forecast.__dict__.get("value"))
            if forecast_value is None:
                cohort.exclude(Exclusion.NO_FORECAST_AVAILABLE)
                continue

            estimate = contract_probability(
                spec,
                forecast_high_f=forecast_value,
                forecast_available_at=forecast.available_at,
                information_cutoff=decision_at,
                lead_days=0,
            )
            parity, _reason = classify(
                settlement_vendor=market["settlement_source_vendor"],
                settlement_product_code=market["settlement_product_code"],
                archived_observation_product="cli_daily",
                has_daily_summary=True,
                archived_product_code=product,
            )

            cohort.observations.append(
                ContractObservation(
                    market_ticker=market["platform_market_id"],
                    station=station,
                    target_date=target_date,
                    event_key=event_key_for(station, target_date),
                    decision_at=decision_at,
                    forecast_available_at=forecast.available_at,
                    forecast_value_f=forecast_value,
                    forecast_age_seconds=chosen_forecast.age_seconds or 0.0,
                    book_available_at=book.available_at,
                    book_age_seconds=chosen_book.age_seconds or 0.0,
                    yes_bid=state.yes_bid,
                    yes_ask=state.yes_ask,
                    no_ask=state.no_ask,
                    lower_inclusive=spec.lower,
                    upper_inclusive=spec.upper,
                    model_probability=estimate.probability,
                    model_version=estimate.model_version,
                    sigma_f=estimate.sigma_f,
                    sigma_provisional=estimate.provisional,
                    settlement_value_f=settled_value,
                    settlement_product=product,
                    settlement_available_at=settled_available_at,
                    # From the venue, never derived from the physical quantity.
                    outcome_yes=(venue_result == "yes"),
                    parity=parity,
                )
            )
    finally:
        op.close()
        ar.close()
    return cohort


def target_dates_with_labels(data_dir: Path) -> list[date]:
    """Days for which a FINAL CLI bulletin has been archived."""
    path = data_dir / "pmvl.db"
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            date.fromisoformat(r[0])
            for r in con.execute(
                "SELECT DISTINCT observation_date FROM daily_climate_observations "
                "WHERE is_final = 1 ORDER BY observation_date"
            )
        ]
    finally:
        con.close()
