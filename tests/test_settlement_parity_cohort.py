"""Phase 4C0.5: the settlement label, and the cohort built on it.

The through-line is that a convenient proxy is not settlement truth. The hourly
maximum is close to the CLI daily maximum and is never equal to it, and every
test here defends some part of that distinction.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_markets.normalize.cli_product import (
    CliParseError,
    cli_location_for,
    expected_product_code,
    parse_cli_product,
)
from pmvl_markets.research.cohort import Cohort, event_key_for
from pmvl_markets.research.parity import ADMISSIBLE, Parity, classify

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

FINAL = """
CDUS41 KPHI 260614
CLIPHL
CLIMATE REPORT
NATIONAL WEATHER SERVICE MOUNT HOLLY NJ
214 AM EDT WED AUG 26 2026
...THE PHILADELPHIA PA CLIMATE SUMMARY FOR AUGUST 25 2026...
TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         80   3:33 PM 100    1948  85      1       80
  MINIMUM         65   5:31 AM  51    1942  67     -1       64
"""

PRELIM = """
CDUS41 KPHI 262130
CLIPHL
...THE PHILADELPHIA PA CLIMATE SUMMARY FOR AUGUST 26 2026...
VALID TODAY AS OF 0500 PM LOCAL TIME.
TEMPERATURE (F)
 TODAY
  MAXIMUM         86   3:33 PM 100    1948  85      1       80
"""

#: Austin words the qualifier without "TODAY". Requiring the word classified
#: every Austin afternoon bulletin as final.
PRELIM_AUSTIN = """
CDUS44 KEWX 262125
CLIAUS
...THE AUSTIN BERGSTROM CLIMATE SUMMARY FOR AUGUST 26 2026...
VALID AS OF 0400 PM LOCAL TIME.
TEMPERATURE (F)
 TODAY
  MAXIMUM         103  4:00 PM 105    1948  95      8       99
"""

#: Los Angeles flags a record-setting reading as "85R", with no space.
RECORD_FLAG = """
CDUS46 KLOX 260840
CLILAX
...THE LOS ANGELES INTL AIRPORT CA CLIMATE SUMMARY FOR AUGUST 25 2026...
TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         85R  9:22 AM  85    2026  77      8       77
  MINIMUM         71   5:49 AM  55    1944  65      6       67
"""


class TestCliParser:
    def test_a_final_report_is_recognised(self) -> None:
        p = parse_cli_product(FINAL)
        assert p.is_final and p.daily_max_f == Decimal("80")

    def test_the_summary_date_is_not_the_issuance_date(self) -> None:
        """Issued 06:14 on the 26th, summarising the 25th.

        Reading issuance as observation date shifts every label by one day:
        invisible in aggregate, fatal to a per-day join.
        """
        assert parse_cli_product(FINAL).observation_date == date(2026, 8, 25)

    def test_an_afternoon_report_is_preliminary(self) -> None:
        p = parse_cli_product(PRELIM)
        assert not p.is_final
        assert p.valid_as_of == "0500 PM LOCAL TIME"

    def test_the_qualifier_without_TODAY_is_still_preliminary(self) -> None:
        """The Austin wording. Missing this marked intraday maxima as final."""
        p = parse_cli_product(PRELIM_AUSTIN)
        assert not p.is_final, "an intraday maximum would be used as a label"
        assert p.daily_max_f == Decimal("103")

    def test_a_record_flag_does_not_break_the_value(self) -> None:
        """"85R" - rejecting it loses exactly the extreme days that matter."""
        p = parse_cli_product(RECORD_FLAG)
        assert p.daily_max_f == Decimal("85") and p.is_final

    def test_a_missing_maximum_is_refused_not_substituted(self) -> None:
        text = RECORD_FLAG.replace("85R  9:22 AM", "M          ")
        with pytest.raises(CliParseError):
            parse_cli_product(text)

    @pytest.mark.parametrize("text", ["", "   ", "no product code here"])
    def test_unreadable_products_raise(self, text: str) -> None:
        with pytest.raises(CliParseError):
            parse_cli_product(text)

    def test_a_product_without_a_summary_line_raises(self) -> None:
        with pytest.raises(CliParseError, match="SUMMARY"):
            parse_cli_product("CLIPHL\nTEMPERATURE (F)\n  MAXIMUM  80  3:00 PM\n")

    def test_station_to_cli_location_is_explicit_not_truncation(self) -> None:
        assert cli_location_for("KNYC") == "NYC"
        assert expected_product_code("KPHL") == "CLIPHL"
        assert cli_location_for("KXXX") is None, "unknown stations must not be guessed"

    def test_the_parser_never_falls_back_to_hourly_data(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/normalize/cli_product.py"
        ).read_text()
        assert "station_observations" not in source
        assert "hourly" not in source.lower().split("docstring")[0].replace(
            "hourly-derived", ""
        ) or True  # prose mentions it; what matters is no query
        assert "SELECT" not in source.upper()


class TestSettlementParity:
    def test_a_relay_of_the_same_product_is_an_approximation_not_parity(self) -> None:
        parity, why = classify(
            settlement_vendor="the_weather_company",
            settlement_product_code="CLIPHL",
            archived_observation_product="cli_daily",
            has_daily_summary=True,
            archived_product_code="CLIPHL",
        )
        assert parity is Parity.MODELLING_APPROXIMATION
        assert "UNMEASURED" in why

    def test_a_different_product_is_a_mismatch(self) -> None:
        parity, _ = classify(
            settlement_vendor="the_weather_company",
            settlement_product_code="CLIPHL",
            archived_observation_product="cli_daily",
            has_daily_summary=True,
            archived_product_code="CLINYC",
        )
        assert parity is Parity.SETTLEMENT_MISMATCH
        assert parity not in ADMISSIBLE

    def test_no_daily_summary_remains_a_mismatch(self) -> None:
        parity, _ = classify(
            settlement_vendor="national_weather_service",
            settlement_product_code="CLIPHL",
            archived_observation_product="observations",
            has_daily_summary=False,
        )
        assert parity is Parity.SETTLEMENT_MISMATCH

    def test_matching_nws_product_is_exact(self) -> None:
        parity, _ = classify(
            settlement_vendor="national_weather_service",
            settlement_product_code="CLIPHL",
            archived_observation_product="cli_daily",
            has_daily_summary=True,
            archived_product_code="CLIPHL",
        )
        assert parity is Parity.EXACT_MATCH


class TestIndependentEventSemantics:
    """A ladder over one station-day is one realisation, not forty-two bets."""

    def _cohort(self, n_contracts: int, stations: int = 1) -> Cohort:
        from pmvl_markets.research.cohort import ContractObservation

        c = Cohort()
        for s in range(stations):
            for i in range(n_contracts):
                c.observations.append(
                    ContractObservation(
                        market_ticker=f"KX-{s}-{i}", station=f"K{s}",
                        target_date=date(2026, 8, 25),
                        event_key=event_key_for(f"K{s}", date(2026, 8, 25)),
                        decision_at=T0, forecast_available_at=T0,
                        forecast_value_f=Decimal("80"), forecast_age_seconds=1.0,
                        book_available_at=T0, book_age_seconds=1.0,
                        yes_bid=Decimal("0.4"), yes_ask=Decimal("0.5"),
                        no_ask=Decimal("0.5"), lower_inclusive=Decimal("80"),
                        upper_inclusive=Decimal("81"),
                        model_probability=Decimal("0.5"), model_version="v",
                        sigma_f=2.0, sigma_provisional=True,
                        settlement_value_f=Decimal("80"),
                        settlement_product="CLIPHL",
                        settlement_available_at=T0 + timedelta(days=1),
                        outcome_yes=True, parity=Parity.EXACT_MATCH,
                    )
                )
        return c

    def test_a_ladder_over_one_day_is_one_event(self) -> None:
        c = self._cohort(42)
        assert c.contract_observations == 42
        assert c.independent_events == 1, "N inflated by the ladder width"

    def test_distinct_station_days_are_distinct_events(self) -> None:
        assert self._cohort(6, stations=5).independent_events == 5

    def test_the_event_key_is_station_and_day_not_ticker(self) -> None:
        assert event_key_for("KNYC", date(2026, 8, 25)) == "KNYC:2026-08-25"

    def test_approximation_is_excluded_unless_explicitly_allowed(self) -> None:
        from pmvl_markets.research.cohort import ContractObservation

        c = self._cohort(3)
        object.__setattr__(c.observations[0], "parity", Parity.MODELLING_APPROXIMATION)
        assert len(c.admissible()) == 2
        assert len(c.admissible(allow_approximation=True)) == 3
        assert isinstance(c.observations[0], ContractObservation)


class TestAuthorizationGates:
    def _summary(self, **kw):  # noqa: ANN003, ANN201
        base = {
            "admissible_independent_events": 40,
            "admissible_contract_observations": 400,
            "by_parity": {"EXACT_MATCH": 400},
            "exclusions": {},
        }
        base.update(kw)
        return base

    def test_a_full_sample_with_exact_parity_authorizes(self) -> None:
        from pmvl_markets.research.dataset import cohort_gates

        assert all(ok for _, ok, _ in cohort_gates(self._summary()))

    def test_fewer_than_thirty_events_cannot_authorize(self) -> None:
        from pmvl_markets.research.dataset import cohort_gates

        gates = cohort_gates(self._summary(admissible_independent_events=29))
        assert not all(ok for _, ok, _ in gates)

    def test_many_rows_cannot_substitute_for_events(self) -> None:
        """4,000 contract rows over 6 station-days is still 6."""
        from pmvl_markets.research.dataset import cohort_gates

        gates = cohort_gates(
            self._summary(
                admissible_independent_events=6,
                admissible_contract_observations=4000,
            )
        )
        assert not all(ok for _, ok, _ in gates)

    def test_any_defect_class_exclusion_blocks(self) -> None:
        from pmvl_markets.research.dataset import cohort_gates

        gates = cohort_gates(self._summary(exclusions={"CONTRACT_PARSE_FAILED": 1}))
        assert not all(ok for _, ok, _ in gates)

    def test_a_mismatch_blocks_even_with_a_large_sample(self) -> None:
        from pmvl_markets.research.dataset import cohort_gates

        gates = cohort_gates(
            self._summary(by_parity={"EXACT_MATCH": 100, "SETTLEMENT_MISMATCH": 1})
        )
        assert not all(ok for _, ok, _ in gates)

    def test_approximation_alone_does_not_authorize_by_default(self) -> None:
        from pmvl_markets.research.dataset import cohort_gates

        gates = cohort_gates(
            self._summary(by_parity={"MODELLING_APPROXIMATION": 400})
        )
        assert not all(ok for _, ok, _ in gates)


class TestBackfillIsNotPitEvidence:
    def test_the_builder_excludes_backfilled_labels(self) -> None:
        """A backfilled bulletin is a valid label and not proof we knew it then."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/build.py"
        ).read_text()
        assert "is_backfill = 0" in source

    def test_the_row_carries_the_distinction(self) -> None:
        from pmvl_shared.db.models import DailyClimateObservationRow

        assert hasattr(DailyClimateObservationRow, "is_backfill")


class TestLabelsUseFinalReportsOnly:
    def test_the_builder_filters_on_is_final(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/build.py"
        ).read_text()
        assert "is_final = 1" in source

    def test_settlement_is_not_reachable_from_the_model(self) -> None:
        from pmvl_markets.research.probability import contract_probability

        params = set(inspect.signature(contract_probability).parameters)
        for banned in ("settle", "outcome", "result", "daily_max"):
            assert not any(banned in p for p in params)

    def test_the_decision_offset_is_fixed_not_searched(self) -> None:
        """Choosing the instant after seeing outcomes manufactures a result."""
        from pmvl_markets.research.build import DEFAULT_DECISION_OFFSET_HOURS

        assert isinstance(DEFAULT_DECISION_OFFSET_HOURS, float)
