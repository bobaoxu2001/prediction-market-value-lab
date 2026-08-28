"""Regression tests for Phase 4C0.6: recovery, quarantine, settlement lifecycle.

Each class pins a defect that actually occurred, not a hypothetical one.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_markets.research.forecast_audit import (
    RecoveryClass,
    ValidityStatus,
    classify_observation,
    max_temperatures_by_day,
)
from pmvl_markets.research.protocol import FROZEN_PROTOCOL, EvaluationProtocol

RAW = (
    '{"properties":{"maxTemperature":{"uom":"wmoUnit:degC","values":['
    '{"validTime":"2026-08-26T00:00:00+00:00/PT6H","value":27.7777777},'
    '{"validTime":"2026-08-27T00:00:00+00:00/PT6H","value":27.2222222}]}}}'
)


class TestForecastRecoveryFromImmutableRaw:
    def test_raw_containing_the_target_date_is_recoverable(self) -> None:
        by_day = max_temperatures_by_day(RAW)
        v = classify_observation(
            observation_id=1, claimed_date="2026-08-26",
            stored_value=Decimal("81.0000"), raw_by_day=by_day,
        )
        assert v.recovery is RecoveryClass.RECOVERABLE_FROM_IMMUTABLE_RAW
        assert v.raw_value == Decimal("82.0000")

    def test_agreement_with_its_own_payload_is_not_affected(self) -> None:
        v = classify_observation(
            observation_id=1, claimed_date="2026-08-26",
            stored_value=Decimal("82.0000"), raw_by_day=max_temperatures_by_day(RAW),
        )
        assert v.recovery is RecoveryClass.NOT_AFFECTED
        assert v.validity is ValidityStatus.VALID

    def test_missing_raw_is_unrecoverable_and_fails_closed(self) -> None:
        v = classify_observation(
            observation_id=1, claimed_date="2026-08-26",
            stored_value=Decimal("81"), raw_by_day={},
        )
        assert v.recovery is RecoveryClass.UNRECOVERABLE_RAW_MISSING
        assert v.validity is ValidityStatus.UNKNOWN

    def test_raw_lacking_the_claimed_date_is_insufficient(self) -> None:
        """The value was not knowable then, so it cannot be re-derived now."""
        v = classify_observation(
            observation_id=1, claimed_date="2026-09-30",
            stored_value=Decimal("81"), raw_by_day=max_temperatures_by_day(RAW),
        )
        assert v.recovery is RecoveryClass.UNRECOVERABLE_RAW_INSUFFICIENT

    def test_unreadable_payload_yields_no_days_rather_than_raising(self) -> None:
        assert max_temperatures_by_day("not json") == {}

    def test_recovery_never_calls_a_provider(self) -> None:
        """Re-querying today would be feature backfill."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/forecast_audit.py"
        ).read_text()
        for banned in ("httpx", "requests", "get_json", "aiohttp", "urllib"):
            assert banned not in source

    def test_rederive_inherits_the_original_availability(self) -> None:
        """available_at records when we could have known, not when we re-read."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/rederive.py"
        ).read_text()
        assert "available_at=row.available_at" in source
        assert "available_at=utcnow()" not in source


class TestDefectQuarantineFailsClosed:
    def test_only_valid_is_research_eligible(self) -> None:
        from pmvl_markets.research.forecast_audit import RESEARCH_ELIGIBLE

        assert RESEARCH_ELIGIBLE == frozenset({ValidityStatus.VALID})
        for status in (
            ValidityStatus.UNKNOWN,
            ValidityStatus.DEFECT_TARGET_DAY_SHIFT,
            ValidityStatus.SUPERSEDED_BY_RAW_REDERIVATION,
        ):
            assert status not in RESEARCH_ELIGIBLE

    def test_the_cohort_query_filters_on_research_eligible(self) -> None:
        """A broad SELECT must not sweep defective rows back in."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/build.py"
        ).read_text()
        assert "research_eligible = 1" in source

    def test_a_second_run_does_not_regress_the_superseded_label(self) -> None:
        """Found during recovery: the idempotent re-run rewrote validity_status
        from the audit verdict before checking whether a correction existed,
        downgrading SUPERSEDED_BY_RAW_REDERIVATION to DEFECT_TARGET_DAY_SHIFT.

        Eligibility was unaffected either way, which is exactly why it hid.
        """
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/rederive.py"
        ).read_text()
        marker = "if row.id in corrected:"
        block = source[source.index(marker): source.index(marker) + 900]
        assert "SUPERSEDED_BY_RAW_REDERIVATION" in block


class TestSettlementCandidateSelection:
    """The starvation defect, proven on real data before it was fixed.

    20,084 unresolved due markets, a 400 cap, and no ORDER BY - so the planner
    returned rows in expected_resolution_time order and the 42 weather markets a
    cohort needs sat at positions 15,925-15,966. Newest-first alone did not fix
    it either: they were still at 3,815-3,856 behind thousands of ball games.
    """

    def _source(self) -> str:
        return Path(
            "packages/market-normalization/src/pmvl_markets/backtest/settlement.py"
        ).read_text()

    def test_the_research_universe_is_selected_first(self) -> None:
        source = self._source()
        assert "is_research_market" in source
        assert "pending = research[:limit]" in source

    def test_selection_is_not_left_to_the_query_planner(self) -> None:
        source = self._source()
        assert "sorted(" in source and "expected_resolution_time" in source

    def test_the_long_tail_is_sampled_so_nothing_starves_forever(self) -> None:
        assert "random.sample" in self._source()

    def test_a_failed_lookup_never_becomes_a_terminal_result(self) -> None:
        """A transient error and 'not final yet' look identical from outside."""
        source = self._source()
        assert "venue_lookup_failed += 1" in source
        block = source[source.index("venue_lookup_failed += 1"):][:400]
        assert "return row, None" in block

    def test_the_counters_distinguish_the_two_failure_modes(self) -> None:
        """"400 checked, 0 settled" was true and told nobody anything."""
        from pmvl_markets.backtest.settlement import SettlementReport

        d = SettlementReport().as_dict()
        for key in (
            "unresolved_due", "research_due", "candidates_selected",
            "venue_finalized", "venue_not_final", "venue_lookup_failed",
            "remaining_unresolved",
        ):
            assert key in d


class TestSettlementAuthoritySeparation:
    def test_the_label_is_the_venue_result_not_the_physical_quantity(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/build.py"
        ).read_text()
        assert 'outcome_yes=(venue_result == "yes")' in source
        assert "outcome_yes=spec.contains(settled_value)" not in source

    def test_the_physical_quantity_is_still_archived_as_audit_evidence(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/build.py"
        ).read_text()
        assert "daily_climate_observations" in source
        assert "settlement_value_f=settled_value" in source

    def test_labels_use_final_bulletins_and_exclude_backfill(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/build.py"
        ).read_text()
        assert "is_final = 1" in source
        assert "is_backfill = 0" in source


class TestProtocolFreeze:
    def test_the_protocol_has_a_content_hash(self) -> None:
        assert len(FROZEN_PROTOCOL.content_hash()) == 16

    @pytest.mark.parametrize(
        "field,value",
        [
            ("decision_offset_hours", 3.0),
            ("model_version", "something-else"),
            ("primary_market_probability", "executable"),
            ("primary_metric", "accuracy"),
            ("independent_unit", "contract"),
        ],
    )
    def test_changing_any_frozen_choice_changes_the_hash(self, field, value) -> None:  # noqa: ANN001
        altered = EvaluationProtocol(**{**FROZEN_PROTOCOL.__dict__, field: value})
        assert altered.content_hash() != FROZEN_PROTOCOL.content_hash()

    def test_sigma_is_not_fitted(self) -> None:
        assert FROZEN_PROTOCOL.sigma_fitted is False
        assert FROZEN_PROTOCOL.sigma_source == "PROVISIONAL_PRIOR"

    def test_the_independent_unit_is_the_station_day(self) -> None:
        assert FROZEN_PROTOCOL.independent_unit == "station_day"
        assert FROZEN_PROTOCOL.bootstrap_unit == "station_day"

    def test_the_offset_is_a_single_value_not_a_search_space(self) -> None:
        assert isinstance(FROZEN_PROTOCOL.decision_offset_hours, float)

    def test_consensus_and_actionable_benchmarks_stay_separate(self) -> None:
        assert (
            FROZEN_PROTOCOL.primary_market_probability
            != FROZEN_PROTOCOL.secondary_market_probability
        )

    def test_the_gate_is_thirty_independent_events(self) -> None:
        assert FROZEN_PROTOCOL.minimum_independent_events == 30


class TestLeakageAfterAuthoritySplit:
    """Venue settlement fields must stay unreachable from feature construction."""

    def test_the_probability_model_cannot_see_any_settlement_field(self) -> None:
        from pmvl_markets.research.probability import contract_probability

        params = set(inspect.signature(contract_probability).parameters)
        for banned in (
            "venue", "settle", "result", "outcome", "expiration", "daily_max",
        ):
            assert not any(banned in p for p in params), f"{banned} reachable"

    def test_the_pit_layer_never_mentions_settlement(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/pit.py"
        ).read_text()
        for banned in ("settlements", "expiration_value", "venue_result"):
            assert banned not in source

    def test_probability_is_computed_before_the_label_is_read(self) -> None:
        from pmvl_markets.research import build

        body = ast.unparse(ast.parse(inspect.getsource(build.build_cohort)))
        assert body.index("contract_probability(") < body.index("outcome_yes=")


class TestStationDayConversion:
    """The deficit must say which lifecycle stage is holding, not just its size."""

    def _row(self, **kw):  # noqa: ANN003, ANN202
        from pmvl_markets.research.coverage import StationDay

        base = dict(
            target_date=date(2026, 8, 26), station="KNYC",
            has_forecast=True, has_orderbook=True,
            has_cli_final=True, has_venue_terminal=True,
        )
        base.update(kw)
        return StationDay(**base)

    def test_all_ingredients_present_is_clean(self) -> None:
        from pmvl_markets.research.coverage import ConversionBlocker

        r = self._row()
        assert r.clean and r.blocker is ConversionBlocker.NONE

    def test_the_blocker_is_the_first_missing_stage(self) -> None:
        """Reporting whichever check ran last would point at the wrong stage."""
        from pmvl_markets.research.coverage import ConversionBlocker

        r = self._row(has_orderbook=False, has_forecast=False, has_cli_final=False)
        assert r.blocker is ConversionBlocker.ORDERBOOK_MISSING

    def test_cli_pending_is_distinguished_from_venue_pending(self) -> None:
        """They need different waits and different diagnoses."""
        from pmvl_markets.research.coverage import ConversionBlocker

        assert self._row(has_cli_final=False).blocker is (
            ConversionBlocker.CLI_FINAL_PENDING
        )
        assert self._row(has_venue_terminal=False).blocker is (
            ConversionBlocker.VENUE_TERMINAL_PENDING
        )

    def test_a_missing_ingredient_is_never_clean(self) -> None:
        for field in (
            "has_forecast", "has_orderbook", "has_cli_final", "has_venue_terminal",
        ):
            assert not self._row(**{field: False}).clean

    def test_ingredient_complete_is_not_claimed_to_be_sufficient(self) -> None:
        """The cohort also needs a book within tolerance AT the decision instant;
        KDEN on 2026-08-25 had every ingredient and still failed on staleness."""
        from pmvl_markets.research.coverage import render_conversions

        text = render_conversions([self._row()])
        assert "necessary, not sufficient" in text


class TestDecisionInstantStaleness:
    """A station-day can hold every ingredient and still be unconvertible.

    An 18-hour collection outage on 2026-08-27 left books present for the
    station-day but none within tolerance at the frozen decision instant. The
    table reported "book: YES" and the outage looked survivable; the cohort
    disagreed, and the cohort was right.
    """

    def _row(self, **kw):  # noqa: ANN003, ANN202
        from pmvl_markets.research.coverage import StationDay

        base = dict(
            target_date=date(2026, 8, 27), station="KNYC",
            has_forecast=True, has_orderbook=True,
            has_cli_final=True, has_venue_terminal=True,
            book_at_decision=True,
        )
        base.update(kw)
        return StationDay(**base)

    def test_a_stale_decision_instant_is_not_clean(self) -> None:
        from pmvl_markets.research.coverage import ConversionBlocker

        r = self._row(book_at_decision=False)
        assert not r.clean
        assert r.blocker is ConversionBlocker.ORDERBOOK_STALE_AT_DECISION

    def test_staleness_outranks_the_label_blockers(self) -> None:
        """Waiting on a CLI final for a station-day whose decision instant can
        never be covered is waiting for something that will not help."""
        from pmvl_markets.research.coverage import ConversionBlocker

        r = self._row(book_at_decision=False, has_cli_final=False,
                      has_venue_terminal=False)
        assert r.blocker is ConversionBlocker.ORDERBOOK_STALE_AT_DECISION

    def test_a_future_decision_instant_is_not_reported_as_failed(self) -> None:
        """None means 'not yet due'. Marking it failed would write off
        station-days that are still perfectly recoverable."""
        r = self._row(book_at_decision=None, has_cli_final=False,
                      has_venue_terminal=False)
        from pmvl_markets.research.coverage import ConversionBlocker

        assert r.blocker is ConversionBlocker.CLI_FINAL_PENDING

    def test_a_covered_decision_instant_with_all_labels_is_clean(self) -> None:
        assert self._row().clean

    def test_the_table_uses_the_cohort_tolerance_not_its_own(self) -> None:
        """Two different tolerances would let the table and the cohort disagree."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/coverage.py"
        ).read_text()
        assert "from .pit import MAX_ORDERBOOK_AGE" in source
        assert "MAX_ORDERBOOK_AGE.total_seconds()" in source
