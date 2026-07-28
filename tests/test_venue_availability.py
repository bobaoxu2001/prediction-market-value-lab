"""Venue availability must never be inferred from an exchange listing.

Moomoo brokers some Kalshi event contracts, but which ones changes without notice
and is gated by jurisdiction and account type. There is no discovery source wired up
here, so claiming availability from a Kalshi listing would be a confident claim the
user cannot act on. These tests pin that behaviour.
"""

from __future__ import annotations

import pytest

from pmvl_shared.enums import (
    DISCOVERABLE_VENUES,
    UNDISCOVERABLE_VENUES,
    VenueAvailability,
    availability_for,
)


class TestUnverifiedByDefault:
    @pytest.mark.parametrize("venue", sorted(UNDISCOVERABLE_VENUES))
    def test_broker_venues_are_unverified(self, venue: str) -> None:
        """Regardless of where the contract was actually observed."""
        for observed in ({"kalshi"}, {"polymarket"}, {"kalshi", "polymarket"}, set()):
            status = availability_for(venue, observed_platforms=observed)
            assert status is VenueAvailability.UNVERIFIED, (
                f"{venue} reported {status} from observations {observed}"
            )

    def test_moomoo_is_never_an_actionable_claim(self) -> None:
        status = availability_for("moomoo", observed_platforms={"kalshi"})
        assert not status.is_actionable_claim
        assert status.display_label == "Unverified"

    def test_kalshi_listing_does_not_imply_moomoo(self) -> None:
        """The specific inference that must not exist."""
        kalshi = availability_for("kalshi", observed_platforms={"kalshi"})
        moomoo = availability_for("moomoo", observed_platforms={"kalshi"})
        assert kalshi.is_actionable_claim
        assert not moomoo.is_actionable_claim
        assert kalshi is not moomoo


class TestObservedVenues:
    def test_observed_venue_is_actionable(self) -> None:
        status = availability_for("kalshi", observed_platforms={"kalshi"})
        assert status is VenueAvailability.OBSERVED_VIA_PUBLIC_API
        assert status.is_actionable_claim

    def test_discoverable_but_absent_is_confirmed_unavailable(self) -> None:
        """We read Polymarket, so 'not found there' is a real answer, not ignorance."""
        status = availability_for("polymarket", observed_platforms={"kalshi"})
        assert status is VenueAvailability.CONFIRMED_UNAVAILABLE
        assert not status.is_actionable_claim

    def test_unknown_venue_is_unverified(self) -> None:
        assert (
            availability_for("some-new-broker", observed_platforms={"kalshi"})
            is VenueAvailability.UNVERIFIED
        )

    def test_case_and_whitespace_insensitive(self) -> None:
        assert (
            availability_for("  MooMoo  ", observed_platforms={"KALSHI"})
            is VenueAvailability.UNVERIFIED
        )
        assert (
            availability_for("Kalshi", observed_platforms={" kalshi "})
            is VenueAvailability.OBSERVED_VIA_PUBLIC_API
        )


class TestVenueSets:
    def test_broker_venues_are_not_discoverable(self) -> None:
        assert not (DISCOVERABLE_VENUES & UNDISCOVERABLE_VENUES)

    def test_moomoo_is_listed_as_undiscoverable(self) -> None:
        assert "moomoo" in UNDISCOVERABLE_VENUES


@pytest.mark.integration
class TestApiSurface:
    def test_market_detail_reports_every_venue(self, clean_db) -> None:  # noqa: ANN001
        from datetime import timedelta

        from fastapi.testclient import TestClient

        from pmvl_api.main import app
        from pmvl_markets.db_models import Market
        from pmvl_shared.timeutil import utcnow

        row = Market(
            platform="kalshi",
            platform_market_id="KXAVAIL-TEST",
            title="Availability surface test",
            status="open",
            created_at=utcnow(),
            expected_resolution_time=utcnow() + timedelta(hours=6),
        )
        clean_db.add(row)
        clean_db.commit()

        payload = TestClient(app).get(f"/markets/{row.id}").json()
        venues = {
            v["venue"]: v for v in payload["data"]["market"]["venue_availability"]
        }
        assert venues["kalshi"]["is_actionable_claim"] is True
        assert venues["moomoo"]["is_actionable_claim"] is False
        assert venues["moomoo"]["label"] == "Unverified"
        # The response must say WHY, not just report a status.
        assert "not inferred" in venues["moomoo"]["note"]
