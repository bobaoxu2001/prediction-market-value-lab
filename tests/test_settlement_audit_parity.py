"""Venue label and audit evidence must be judged separately.

Written after KMIA 2026-08-29, where the venue paid the 90-91F bucket, the
archived audit value read 85.0F, and nothing in the pipeline could report the
contradiction. Two distinct defects sat underneath it:

* ``parity.classify`` compares provenance only - station, product code, day
  definition - so a value that contradicts its own outcome is invisible to it;
* NWS published two FINAL bulletins for that station-day reading 90.0F and
  85.0F, and ``build_cohort`` resolves duplicates with ``setdefault`` over
  ``ORDER BY available_at``, making the winner a function of which HTTP
  response landed first (0.86s apart in the real data).

The invariant these protect is one-directional: audit evidence may contradict a
venue label and must be visible when it does, but it may never move one.
"""

from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal as D
from pathlib import Path

import pytest

from pmvl_markets.research.settlement_audit import (
    AuditObservation,
    AuditStatus,
    SettlementParity,
    VenueContract,
    VenueLabelStatus,
    classify,
    venue_implied_range,
)

BUILD_SRC = (
    Path(__file__).resolve().parents[1]
    / "packages/market-normalization/src/pmvl_markets/research/build.py"
)


def _kmia_ladder(*, b905_yes: bool = True) -> list[VenueContract]:
    """The real KMIA 2026-08-29 ladder as the venue settled it."""
    return [
        VenueContract("KXHIGHMIA-26AUG29-B88.5", D(88), D(89), False),
        VenueContract("KXHIGHMIA-26AUG29-B90.5", D(90), D(91), b905_yes),
        VenueContract("KXHIGHMIA-26AUG29-B92.5", D(92), D(93), False),
        VenueContract("KXHIGHMIA-26AUG29-B94.5", D(94), D(95), False),
        VenueContract("KXHIGHMIA-26AUG29-T88", None, D(87), False),
        VenueContract("KXHIGHMIA-26AUG29-T95", D(96), None, False),
    ]


def test_venue_yes_with_matching_audit() -> None:
    """Test 1: bucket 90-91 YES, audit 90 -> self-consistent and matching."""
    p = classify(
        contracts=_kmia_ladder(),
        audit=[AuditObservation(D("90.0"), "CLIMIA")],
    )
    assert p.venue_label_status is VenueLabelStatus.SELF_CONSISTENT
    assert p.audit_status is AuditStatus.MATCH
    assert (p.venue_implied_low, p.venue_implied_high) == (D(90), D(91))
    assert not p.is_contradiction


def test_venue_yes_with_conflicting_audit() -> None:
    """Test 2: bucket 90-91 YES, audit 85 -> venue stands, audit conflicts."""
    p = classify(
        contracts=_kmia_ladder(),
        audit=[AuditObservation(D("85.0"), "CLIMIA")],
    )
    # The venue verdict is unchanged by the disagreeing audit value.
    assert p.venue_label_status is VenueLabelStatus.SELF_CONSISTENT
    assert (p.venue_implied_low, p.venue_implied_high) == (D(90), D(91))
    assert p.audit_status is AuditStatus.CONFLICT
    assert p.is_contradiction


def test_duplicate_conflicting_finals_are_surfaced_not_resolved() -> None:
    """Test 3: the real KMIA case. Both values kept, neither selected."""
    p = classify(
        contracts=_kmia_ladder(),
        audit=[
            AuditObservation(D("85.0"), "CLIMIA", published_at="2026-08-30T09:10"),
            AuditObservation(D("90.0"), "CLIMIA", published_at="2026-08-30T08:24"),
        ],
    )
    assert p.audit_status is AuditStatus.DUPLICATE_FINAL_CONFLICT
    assert p.audit_values == (D("85.0"), D("90.0"))
    assert p.selected_audit_value is None, (
        "picking a winner between two disagreeing FINAL bulletins invents "
        "evidence; the disagreement is the finding"
    )
    assert p.is_contradiction
    assert p.venue_label_status is VenueLabelStatus.SELF_CONSISTENT


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_fetch_order_cannot_change_the_verdict(order: tuple[int, int]) -> None:
    """Test 4: reversing arrival order changes nothing.

    This is the property ``setdefault`` over ``ORDER BY available_at`` lacks.
    """
    obs = [
        AuditObservation(D("85.0"), "CLIMIA", available_at="2026-08-31T09:25:28"),
        AuditObservation(D("90.0"), "CLIMIA", available_at="2026-08-31T09:25:29"),
    ]
    p = classify(contracts=_kmia_ladder(), audit=[obs[order[0]], obs[order[1]]])
    assert p.audit_status is AuditStatus.DUPLICATE_FINAL_CONFLICT
    assert p.audit_values == (D("85.0"), D("90.0"))
    assert p.selected_audit_value is None
    assert (p.venue_implied_low, p.venue_implied_high) == (D(90), D(91))


def test_audit_unavailable_leaves_the_venue_label_intact() -> None:
    """Test 5: no audit evidence does not invalidate a venue label."""
    p = classify(contracts=_kmia_ladder(), audit=[])
    assert p.venue_label_status is VenueLabelStatus.SELF_CONSISTENT
    assert p.audit_status is AuditStatus.UNAVAILABLE
    assert p.selected_audit_value is None
    assert not p.is_contradiction


def test_impossible_venue_representation_fails_venue_parity() -> None:
    """Test 6: two disjoint buckets both YES cannot both be true."""
    contracts = _kmia_ladder()
    contracts[0] = VenueContract("KXHIGHMIA-26AUG29-B88.5", D(88), D(89), True)
    p = classify(contracts=contracts, audit=[AuditObservation(D("90.0"), "CLIMIA")])
    assert p.venue_label_status is VenueLabelStatus.SELF_INCONSISTENT
    assert (p.venue_implied_low, p.venue_implied_high) == (None, None)
    assert p.is_contradiction
    assert venue_implied_range(contracts) == (None, None)


def test_outcome_yes_is_assigned_from_the_venue_result_only() -> None:
    """Test 7: label isolation, enforced against the builder's own source.

    A unit test cannot prove the negative on its own, so this reads the
    assignment: ``outcome_yes`` must be derived from the venue result and must
    not mention the audit value bound to ``settled_value``.
    """
    tree = ast.parse(BUILD_SRC.read_text())
    assigns = [
        kw
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "outcome_yes"
    ]
    assert assigns, "no outcome_yes assignment found in build.py"
    for kw in assigns:
        names = {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
        assert "venue_result" in names, f"outcome_yes not venue-derived: {names}"
        assert "settled_value" not in names, (
            "outcome_yes must never be derived from the audit value"
        )


def test_settlement_value_f_reaches_no_metric() -> None:
    """The audit value is diagnostic only.

    ``settlement_value_f`` is assigned in build.py and rendered in as_dict; if
    it ever gains a reader in research code, that reader must be reviewed
    against the rule that audit evidence cannot influence a research result.
    """
    root = Path(__file__).resolve().parents[1] / "packages/market-normalization/src"
    readers = []
    for path in root.rglob("*.py"):
        if path.name in {"build.py", "cohort.py", "settlement_audit.py"}:
            continue
        if "settlement_value_f" in path.read_text():
            readers.append(str(path.relative_to(root)))
    assert not readers, f"audit value gained a consumer: {readers}"


def test_parity_is_frozen_and_reports_itself() -> None:
    p = classify(contracts=_kmia_ladder(), audit=[AuditObservation(D("90.0"), "C")])
    assert isinstance(p, SettlementParity)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.venue_label_status = VenueLabelStatus.SELF_INCONSISTENT  # type: ignore[misc]
    d = p.as_dict()
    assert d["venue_label_status"] == "VENUE_LABEL_SELF_CONSISTENT"
    assert d["audit_status"] == "AUDIT_MATCH"
