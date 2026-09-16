"""Reading cancellation clauses, and the rule that a reading is not a proof.

The interesting assertions are the negative ones. A pattern that fires on the wrong
sentence does not produce a bad recommendation here - it produces a *certified* one,
because this is the only input that lets a pair reach GUARANTEED. So the tie-breaks
and the review requirement carry more weight than the happy-path extraction.
"""

from __future__ import annotations

import json

import pytest

from pmvl_markets.matching.cancellation import (
    AttestationStore,
    RuleAttestation,
    VoidHandling,
    draft_attestation,
    read_cancellation,
)
from pmvl_shared.enums import Platform


# ------------------------------------------------------------------- reading
def test_a_rule_that_covers_non_occurrence_is_read_as_resolving_anyway():
    reading = read_cancellation(
        "If the game is not played by March 3, 2026, this market will resolve to No."
    )
    assert reading.handling is VoidHandling.RESOLVES_ANYWAY
    assert "not played by March 3" in reading.evidence_quote


def test_an_explicit_void_clause_is_read_as_voiding():
    reading = read_cancellation(
        "This market will be voided if the event is postponed beyond the season."
    )
    assert reading.handling is VoidHandling.VOIDS


def test_a_fifty_fifty_clause_is_read_as_splitting():
    assert read_cancellation(
        "In the event of a tie, this market resolves 50-50."
    ).handling is VoidHandling.SPLITS


def test_a_rule_that_says_nothing_about_cancellation_is_unstated():
    reading = read_cancellation(
        "This market resolves to Yes if the Federal Reserve raises rates in March."
    )
    assert reading.handling is VoidHandling.UNSTATED
    assert reading.evidence_quote == ""


def test_empty_rule_text_is_unstated_rather_than_an_error():
    assert read_cancellation("").handling is VoidHandling.UNSTATED


def test_the_permissive_reading_loses_the_tie():
    """Rules commonly state both. The clause that admits a void has to win.

    Reading only the first matching sentence would certify a pair whose rules say,
    two sentences later, that it can be voided after all.
    """
    both = (
        "The game resolves No if not played by Mar 3. "
        "If the season is abandoned the market will be voided."
    )
    assert read_cancellation(both).handling is VoidHandling.VOIDS


def test_a_postponement_clause_without_a_named_outcome_is_not_a_resolution():
    """"If postponed..." says nothing about who gets paid until it names an outcome."""
    assert read_cancellation(
        "If the event is postponed, an announcement will follow."
    ).handling is VoidHandling.UNSTATED


def test_every_reading_marks_itself_as_needing_review():
    assert read_cancellation("resolves to No if not played").requires_review is True


# --------------------------------------------------------------- attestations
def test_an_attestation_must_name_a_reviewer():
    with pytest.raises(ValueError):
        RuleAttestation(
            venue=Platform.KALSHI, market_ref="K", resolution_hash="h",
            handling=VoidHandling.RESOLVES_ANYWAY, reviewed_by="  ",
            reviewed_at="2026-09-16T00:00:00Z",
        )


def test_an_attestation_must_name_the_rule_terms_it_covers():
    with pytest.raises(ValueError):
        RuleAttestation(
            venue=Platform.KALSHI, market_ref="K", resolution_hash="",
            handling=VoidHandling.RESOLVES_ANYWAY, reviewed_by="ops",
            reviewed_at="2026-09-16T00:00:00Z",
        )


def _attestation(hash_="h1"):
    return RuleAttestation(
        venue=Platform.KALSHI, market_ref="KXT", resolution_hash=hash_,
        handling=VoidHandling.RESOLVES_ANYWAY, reviewed_by="ops@example",
        reviewed_at="2026-09-16T00:00:00Z", evidence_quote="resolves No if not played",
    )


def test_an_attestation_covers_only_the_rule_terms_it_was_made_against():
    store = AttestationStore()
    store.record(_attestation("h1"))
    assert store.get(Platform.KALSHI, "KXT", "h1") is not None
    # The venue edited its rules; the hash moved and the reading no longer applies.
    assert store.get(Platform.KALSHI, "KXT", "h2") is None


def test_a_lapsed_attestation_is_distinguishable_from_one_that_never_existed():
    """A queue of stale readings and a backlog of unread ones are different problems."""
    store = AttestationStore()
    store.record(_attestation("h1"))
    assert store.lapsed_for(Platform.KALSHI, "KXT") is True
    assert store.lapsed_for(Platform.KALSHI, "OTHER") is False


def test_an_attestation_for_one_venue_does_not_cover_the_other():
    store = AttestationStore()
    store.record(_attestation())
    assert store.get(Platform.POLYMARKET, "KXT", "h1") is None


def test_attestations_survive_a_round_trip_through_disk(tmp_path):
    path = tmp_path / "attestations.json"
    store = AttestationStore(path)
    store.record(_attestation())
    store.save()

    reloaded = AttestationStore(path)
    assert len(reloaded) == 1
    found = reloaded.get(Platform.KALSHI, "KXT", "h1")
    assert found is not None
    assert found.reviewed_by == "ops@example"
    assert found.handling is VoidHandling.RESOLVES_ANYWAY


def test_a_draft_leaves_the_reviewer_blank_so_it_cannot_become_an_attestation():
    draft = draft_attestation(
        venue=Platform.KALSHI,
        market_ref="KXT",
        resolution_hash="h1",
        rules_text="If the game is not played by March 3, this market resolves to No.",
    )
    assert draft["handling"] == VoidHandling.RESOLVES_ANYWAY.value
    assert draft["reviewed_by"] == ""
    assert "confirm against the published rules" in draft["note"]
    # Filling in the reviewer is the act that turns a proposal into evidence.
    with pytest.raises(ValueError):
        RuleAttestation.from_dict(draft)
    signed = {**draft, "reviewed_by": "ops@example"}
    assert RuleAttestation.from_dict(signed).reviewed_by == "ops@example"


def test_a_draft_is_json_serialisable_so_it_can_be_sent_for_review(tmp_path):
    draft = draft_attestation(
        venue=Platform.POLYMARKET, market_ref="0xm", resolution_hash="h",
        rules_text="This market will be voided if the match is cancelled.",
        source_url="https://polymarket.com/event/x",
    )
    path = tmp_path / "draft.json"
    path.write_text(json.dumps([draft], indent=2))
    assert json.loads(path.read_text())[0]["handling"] == "voids"
