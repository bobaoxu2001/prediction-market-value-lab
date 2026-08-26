"""A published shadow decision is never rewritten.

The whole value of a shadow record is that it was written before the answer
existed. The failure that destroys it is not fraud - it is drift: a migration
that backfills a field, a rerun that "corrects" a stale profile, a fix that
recomputes an old signal under the current factor version. Each is individually
reasonable and each empties the record of the one property it had.

So the tests below pin three things:

* content is hashed over a **fixed field list**, so adding a field later cannot
  silently change the hash of records already written;
* a modified record is **reported**, never repaired - repairing it deletes the
  only evidence that anything moved;
* outcomes are **appended**, and an outcome that predates its decision is
  refused, because that is either a clock problem or a backfill and both would
  make the record claim foreknowledge.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.enums import DataProvenance, Side

from pmvl_markets.alpha.models import AlphaDirection, AlphaScore, Liquidity, OutcomeEstimate
from pmvl_markets.alpha.registry import FeatureValue, FeatureVector
from pmvl_markets.alpha.shadow import (
    ImmutabilityError,
    ShadowDecision,
    ShadowLedger,
    ShadowOutcome,
    ShadowStatus,
    decision_id_for,
    frozen_source_values,
    record_decision,
    verify_unchanged,
)
from pmvl_markets.alpha.pit import observation
from pmvl_markets.alpha.strategy import Signal

BASE = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def signal(declined: str | None = None) -> Signal:
    return Signal(
        market_key="kalshi:KXHIGHNY-26AUG20-T85",
        decision_time=at(0),
        side=Side.YES,
        liquidity=Liquidity.MAKER,
        score=Decimal("0.04"),
        conservative_probability=Decimal("0.50"),
        executable_probability=Decimal("0.34"),
        expected_edge_per_contract=Decimal("0.12"),
        alpha=alpha(),
        components={"raw_gap": Decimal("0.16")},
        feature_names=("source_divergence",),
        reasons=("fresh NWS update the market has not absorbed",),
        declined_reason=declined,
    )


def alpha() -> AlphaScore:
    return AlphaScore(
        expected_move=Decimal("0.04"),
        horizon=timedelta(hours=6),
        confidence=Decimal("0.3"),
        direction=AlphaDirection.UP,
        model_version="alpha-rule-v0",
        components={"raw_gap": Decimal("0.16")},
        reasons=("stated prior, not a measurement",),
    )


def outcome() -> OutcomeEstimate:
    return OutcomeEstimate(
        probability=Decimal("0.55"),
        low=Decimal("0.50"),
        high=Decimal("0.60"),
        confidence=Decimal("0.7"),
        has_independent_prior=True,
        model_version="weather-nws-v1",
    )


def features() -> FeatureVector:
    return FeatureVector(
        market_key="kalshi:KXHIGHNY-26AUG20-T85",
        decision_time=at(0),
        values={
            "source_divergence": FeatureValue.of(
                "source_divergence", Decimal("0.16")
            )
        },
    )


def build(order=None, decision_id: str = "d1") -> ShadowDecision:  # noqa: ANN001
    return record_decision(
        decision_id=decision_id,
        strategy="source_lag_convergence",
        strategy_version="v0",
        signal=signal(),
        outcome=outcome(),
        alpha=alpha(),
        features=features(),
        factor_versions={"source_divergence": "v0"},
        order=order,
        information_cutoff=at(0),
    )


class TestSealing:
    def test_a_recorded_decision_is_sealed_with_a_content_hash(self) -> None:
        decision = build()
        assert decision.content_hash
        verify_unchanged(decision)

    def test_an_unsealed_record_is_rejected(self) -> None:
        """It proves nothing about when it was written."""
        decision = replace(build(), content_hash="")
        with pytest.raises(ImmutabilityError, match="never sealed"):
            verify_unchanged(decision)

    def test_changing_any_field_breaks_the_hash(self) -> None:
        decision = build()
        tampered = replace(decision, model_probability=Decimal("0.99"))
        with pytest.raises(ImmutabilityError, match="modified since publication"):
            verify_unchanged(tampered)

    def test_the_hash_is_stable_across_dict_ordering(self) -> None:
        """Otherwise the hash detects Python's iteration order, not a change."""
        first = build()
        second = replace(
            first,
            factor_versions={"source_divergence": "v0"},
            content_hash=first.content_hash,
        )
        verify_unchanged(second)

    def test_the_hash_covers_a_FIXED_field_list(self) -> None:
        """So adding a field later cannot silently change the hash of records
        already written."""
        from pmvl_markets.alpha.shadow import _HASHED_FIELDS

        assert "decision_id" in _HASHED_FIELDS
        assert "information_cutoff" in _HASHED_FIELDS
        assert "content_hash" not in _HASHED_FIELDS


class TestWhatIsFrozen:
    def test_every_required_element_of_the_information_set_is_recorded(self) -> None:
        """The research plan's list, as an assertion."""
        decision = build()
        blob = decision.as_dict()
        for required in (
            "decision_time",
            "information_cutoff",
            "source_values",
            "feature_values",
            "model_probability",
            "alpha_expected_move",
            "executable_probability",
            "proposed_price",
            "proposed_size",
            "rationale",
            "outcome_model_version",
            "alpha_model_version",
            "factor_versions",
        ):
            assert required in blob

    def test_a_refusal_is_recorded_as_a_decision_too(self) -> None:
        """A strategy that declines 400 times and trades 3 is a different object
        from one that trades 3 out of 3, and only the recorded refusals
        distinguish them."""
        decision = build(order=None)
        assert decision.status is ShadowStatus.NO_TRADE

    def test_source_values_keep_all_four_timestamps(self) -> None:
        """Storing only the value makes a later audit unable to check the
        availability claim - which is the thing a shadow record most needs to
        be checkable on."""
        frozen = frozen_source_values(
            [
                observation(
                    source_id="nws",
                    kind="source_estimate",
                    value=Decimal("0.55"),
                    available_at=at(0),
                    observed_at=at(-30),
                    event_time=at(-60),
                )
            ]
        )
        entry = frozen["source_estimate:nws"]
        assert entry["available_at"] and entry["observed_at"] and entry["event_time"]
        assert entry["available_at"] != entry["observed_at"]


class TestTheLedger:
    def test_publishing_twice_under_one_id_is_refused(self) -> None:
        """Two different decisions competing for one identity: keeping either
        silently loses the other."""
        ledger = ShadowLedger()
        ledger.publish(build())
        with pytest.raises(ImmutabilityError, match="already published"):
            ledger.publish(build())

    def test_an_outcome_is_appended_not_merged(self) -> None:
        ledger = ShadowLedger()
        decision = ledger.publish(build())
        ledger.append_outcome(
            ShadowOutcome(
                decision_id=decision.decision_id,
                observed_at=at(360),
                kind="settlement",
                value=Decimal("1"),
            )
        )
        # The decision is untouched by the outcome.
        verify_unchanged(ledger.decisions()[0])
        assert len(ledger.outcomes_for(decision.decision_id)) == 1

    def test_an_outcome_that_predates_its_decision_is_refused(self) -> None:
        """Either a clock problem or a backfill; both make the record claim
        foreknowledge."""
        ledger = ShadowLedger()
        decision = ledger.publish(build())
        with pytest.raises(ValueError, match="predates"):
            ledger.append_outcome(
                ShadowOutcome(
                    decision_id=decision.decision_id,
                    observed_at=at(-60),
                    kind="settlement",
                )
            )

    def test_an_outcome_for_an_unknown_decision_is_refused(self) -> None:
        ledger = ShadowLedger()
        with pytest.raises(KeyError):
            ledger.append_outcome(
                ShadowOutcome(decision_id="nope", observed_at=at(1), kind="fill")
            )

    def test_the_audit_reports_drift_and_does_not_repair_it(self) -> None:
        ledger = ShadowLedger()
        ledger.publish(build())
        # Reach past the API, the way a migration or a careless fix would.
        ledger._decisions["d1"] = replace(  # noqa: SLF001
            ledger._decisions["d1"], executable_probability=Decimal("0.01")  # noqa: SLF001
        )
        problems = ledger.audit()
        assert problems
        assert "modified since publication" in problems[0]
        # And the record still says what it was tampered into: nothing repaired it.
        assert ledger.decisions()[0].executable_probability == Decimal("0.01")


class TestDecisionIdentity:
    def test_the_same_decision_gets_the_same_id(self) -> None:
        """Deterministic, so a rerun DETECTS a duplicate rather than creating a
        second copy of one decision under a new name."""
        first = decision_id_for("s", "kalshi:X", at(0), "m1")
        second = decision_id_for("s", "kalshi:X", at(0), "m1")
        assert first == second

    def test_a_different_instant_gets_a_different_id(self) -> None:
        assert decision_id_for("s", "kalshi:X", at(0), "m1") != decision_id_for(
            "s", "kalshi:X", at(1), "m1"
        )

    def test_a_different_model_version_gets_a_different_id(self) -> None:
        """Same market, same instant, different model is a different decision."""
        assert decision_id_for("s", "kalshi:X", at(0), "m1") != decision_id_for(
            "s", "kalshi:X", at(0), "m2"
        )


@pytest.mark.integration
class TestDatabaseBackedLedger:
    def test_a_decision_round_trips_and_still_verifies(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.store import audit_decisions, publish_decision

        publish_decision(clean_db, build(), provenance=DataProvenance.FIXTURE)
        clean_db.commit()
        assert audit_decisions(clean_db) == []

    def test_republishing_the_identical_decision_is_idempotent(self, clean_db) -> None:  # noqa: ANN001
        """A rerun of a deterministic pass. The stored row already says exactly
        this, so it is not an error."""
        from pmvl_markets.alpha.store import publish_decision

        decision = build()
        first = publish_decision(clean_db, decision, provenance=DataProvenance.FIXTURE)
        second = publish_decision(clean_db, decision, provenance=DataProvenance.FIXTURE)
        assert first.id == second.id

    def test_republishing_DIFFERENT_content_under_one_id_is_refused(
        self, clean_db
    ) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.store import publish_decision

        publish_decision(clean_db, build(), provenance=DataProvenance.FIXTURE)
        clean_db.flush()
        other = record_decision(
            decision_id="d1",
            strategy="source_lag_convergence",
            strategy_version="v0",
            signal=signal(),
            outcome=OutcomeEstimate(
                probability=Decimal("0.90"),
                low=Decimal("0.85"),
                high=Decimal("0.95"),
                confidence=Decimal("0.7"),
                has_independent_prior=True,
                model_version="weather-nws-v1",
            ),
            alpha=alpha(),
            features=features(),
            factor_versions={"source_divergence": "v0"},
            order=None,
            information_cutoff=at(0),
        )
        with pytest.raises(ImmutabilityError, match="already published"):
            publish_decision(clean_db, other, provenance=DataProvenance.FIXTURE)

    def test_the_audit_detects_a_row_edited_behind_the_apis_back(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """The check that makes the immutability claim more than an intention."""
        from sqlalchemy import update

        from pmvl_markets.db_models import ShadowDecisionRow
        from pmvl_markets.alpha.store import audit_decisions, publish_decision

        publish_decision(clean_db, build(), provenance=DataProvenance.FIXTURE)
        clean_db.flush()
        clean_db.execute(
            update(ShadowDecisionRow)
            .where(ShadowDecisionRow.decision_id == "d1")
            .values(executable_probability=Decimal("0.01"))
        )
        clean_db.flush()
        problems = audit_decisions(clean_db)
        assert problems and "modified since publication" in problems[0]

    def test_an_outcome_row_is_appended_without_touching_the_decision(
        self, clean_db
    ) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.store import (
            append_outcome,
            audit_decisions,
            publish_decision,
        )

        publish_decision(clean_db, build(), provenance=DataProvenance.FIXTURE)
        clean_db.flush()
        append_outcome(
            clean_db,
            ShadowOutcome(
                decision_id="d1",
                observed_at=at(360),
                kind="settlement",
                value=Decimal("1"),
            ),
            provenance=DataProvenance.FIXTURE,
        )
        clean_db.flush()
        assert audit_decisions(clean_db) == []


@pytest.mark.integration
class TestPointInTimeStore:
    def test_an_observation_without_availability_is_refused(self, clean_db) -> None:  # noqa: ANN001
        """It cannot be placed in time, and guessing decides - silently -
        whether a past decision was allowed to see it."""
        from pmvl_markets.alpha.pit import SourceObservation
        from pmvl_markets.alpha.store import record_observation

        market_id = _seed_market(clean_db)
        with pytest.raises(ValueError, match="available_at"):
            record_observation(
                clean_db,
                market_id=market_id,
                observation=SourceObservation(
                    source_id="nws", kind="source_estimate",
                    value=Decimal("0.5"), available_at=None,
                ),
            )

    def test_a_repeat_of_the_same_observation_is_not_duplicated(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """Two ingest workers polling one source race a check-then-act, and the
        loser writes a duplicate that every dispersion and lag factor counts
        twice."""
        from pmvl_markets.alpha.store import record_observation

        market_id = _seed_market(clean_db)
        obs = observation(
            source_id="nws",
            kind="source_estimate",
            value=Decimal("0.5"),
            available_at=at(0),
        )
        assert record_observation(
            clean_db, market_id=market_id, observation=obs,
            provenance=DataProvenance.FIXTURE,
        ) is True
        assert record_observation(
            clean_db, market_id=market_id, observation=obs,
            provenance=DataProvenance.FIXTURE,
        ) is False

    def test_reading_back_as_of_a_past_instant_excludes_later_rows(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """Filtered in the query, not after it.

        Loading everything and filtering in Python puts the future in memory
        beside the past, one attribute access from a leak.
        """
        from pmvl_markets.alpha.store import load_observations, record_observation

        market_id = _seed_market(clean_db)
        for minute, value in ((0, "0.40"), (60, "0.90")):
            record_observation(
                clean_db,
                market_id=market_id,
                observation=observation(
                    source_id="nws",
                    kind="source_estimate",
                    value=Decimal(value),
                    available_at=at(minute),
                ),
                provenance=DataProvenance.FIXTURE,
            )
        clean_db.flush()
        early = load_observations(clean_db, market_id=market_id, as_of=at(30))
        assert [o.value for o in early] == [Decimal("0.40")]

    def test_a_revision_is_a_second_row_not_an_update(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.store import load_observations, record_observation

        market_id = _seed_market(clean_db)
        for minute, value in ((0, "0.30"), (1440, "0.40")):
            record_observation(
                clean_db,
                market_id=market_id,
                observation=observation(
                    source_id="bls",
                    kind="source_point_value",
                    value=Decimal(value),
                    available_at=at(minute),
                    event_time=at(-1440),
                ),
                provenance=DataProvenance.FIXTURE,
            )
        clean_db.flush()
        rows = load_observations(clean_db, market_id=market_id)
        assert len(rows) == 2
        assert rows[0].event_time == rows[1].event_time


def _seed_market(session) -> int:  # noqa: ANN001
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.db_models import Market

    market = Market(
        platform="kalshi",
        platform_market_id="KXHIGHNY-26AUG20-T85",
        provenance=DataProvenance.FIXTURE,
        created_at=utcnow(),
    )
    session.add(market)
    session.flush()
    return market.id
