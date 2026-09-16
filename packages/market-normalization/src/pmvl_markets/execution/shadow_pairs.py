"""The shadow run: scan both venues, try both directions, execute nothing real.

What this produces is a :class:`ShadowPairRun` - a coverage funnel plus one journal
per pair that got far enough to attempt. Both halves matter. The journals say what
happened to the pairs that were tried; the funnel says how many were never tried and
why, which is the half that turns "no opportunities today" from a claim about the
market into a claim about this pipeline's reach.

Fills come from the real observed depth of the books that were scanned, consumed
level by level. That is a simulation of a real ladder, not a fabricated one - but it
is still a simulation, and every journal is stamped ``shadow`` and carries the
provenance of the books it was filled from. A shadow fill is evidence about this
system's logic. It is not evidence that a venue would have filled the order.

Both leg orientations are tried for every pair, because the direction that pays is not
knowable in advance: buying Kalshi YES against Polymarket NO and buying Kalshi NO
against Polymarket YES are different trades against different sides of two books, and
a scanner that checks one of them finds roughly half the opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping

from pmvl_shared.enums import DataProvenance, Platform, Side
from pmvl_shared.money import ZERO
from pmvl_shared.schemas import NormalizedMarket, OrderBook
from pmvl_shared.timeutil import iso, utcnow

from ..arbitrage.coverage import CoverageFunnel, Stage
from ..matching.cancellation import AttestationStore
from ..matching.candidates import MatchCandidate, generate_candidates
from ..matching.equivalence import EquivalenceScore, EquivalenceVerdict, score_equivalence
from .assessment import AccessStatus
from .candidates import CandidateRejection, build_pair_candidate
from .funding import FundingLedger
from .modes import ExecutionMode
from .simulator import BookBackedVenueClient, ladder_from_book
from .twoleg import PairCandidate, PairJournal, PairState, TwoLegExecutor

__all__ = ["ShadowPairRun", "run_shadow_pairs", "MarketBook"]


@dataclass(frozen=True, slots=True)
class MarketBook:
    """A market and the book observed for it, kept together so they cannot drift."""

    market: NormalizedMarket
    book: OrderBook


@dataclass
class ShadowPairRun:
    """Everything one shadow pass observed, in a form that can be filed and re-read."""

    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    mode: ExecutionMode = ExecutionMode.SHADOW
    funnel: CoverageFunnel = field(default_factory=CoverageFunnel)
    journals: list[PairJournal] = field(default_factory=list)
    #: Pairs that could not even be built, with the stage that stopped them.
    rejections: list[tuple[str, str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Provenance of the books that produced the fills. ``live`` means real depth;
    #: ``demo`` means the numbers are illustrative and prove nothing about a market.
    book_provenance: set[str] = field(default_factory=set)

    @property
    def completed(self) -> list[PairJournal]:
        return [j for j in self.journals if j.arbitrage_completed]

    @property
    def escalated(self) -> list[PairJournal]:
        return [j for j in self.journals if j.state is PairState.ESCALATED]

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at) if self.finished_at else None,
            "mode": self.mode.value,
            "book_provenance": sorted(self.book_provenance),
            "funnel": self.funnel.as_dict(),
            "attempted": len(self.journals),
            "completed": len(self.completed),
            "escalated": len(self.escalated),
            "journals": [j.as_dict() for j in self.journals],
            "rejections": [
                {"pair": pair, "stage": stage, "reason": reason}
                for pair, stage, reason in self.rejections
            ],
            "errors": list(self.errors),
        }

    def summary(self) -> str:
        lines = [self.funnel.render(), ""]
        if not self.journals:
            lines.append(
                "No pair reached execution. That is a valid outcome, and the funnel "
                "above says which filter ended each one."
            )
            return "\n".join(lines)
        lines.append(f"attempted {len(self.journals)} pair(s):")
        for journal in self.journals:
            pnl = journal.realised_pnl_usd
            lines.append(
                f"  {journal.pair_id:28} {journal.state.value:16} "
                f"arb={'yes' if journal.arbitrage_completed else 'no ':3} "
                f"pnl={'n/a' if pnl is None else f'${pnl}'}"
            )
        return "\n".join(lines)


def _orientations(score: EquivalenceScore) -> list[tuple[Side, Side]]:
    """Both complementary pairings, honouring an inverted outcome mapping.

    When one venue words the question as the negation of the other's, its YES is the
    other's NO, so the hedging leg is the *same* nominal side rather than the
    opposite one. Getting this backwards produces a pair that is long the same
    outcome twice, which the settlement evaluator catches - but catching it there
    means every such pair is wasted work.
    """
    pairs = [(Side.YES, Side.NO), (Side.NO, Side.YES)]
    if not score.polarity_inverted:
        return pairs
    flip = {Side.YES: Side.NO, Side.NO: Side.YES}
    return [(a, flip[b]) for a, b in pairs]


def run_shadow_pairs(
    universe: Iterable[MarketBook],
    *,
    attestations: AttestationStore,
    funding: FundingLedger,
    max_quote_age_seconds: int = 120,
    max_pairs: int = 4000,
    max_sets: Decimal = Decimal("10000"),
    access: Mapping[Platform, AccessStatus] | None = None,
    label: str = "",
) -> ShadowPairRun:
    """Scan, build and shadow-execute every pair the universe supports.

    ``access`` carries what was independently established about this account's right
    to trade each venue. It defaults to ``UNVERIFIED``, which blocks - permission is
    something that has to be checked and passed in, never something absent from the
    arguments.
    """
    run = ShadowPairRun()
    run.funnel.label = label or "cross-venue shadow pass"
    access = access or {}

    entries = list(universe)
    by_platform: dict[Platform, list[MarketBook]] = {}
    for entry in entries:
        by_platform.setdefault(entry.market.platform, []).append(entry)
        run.book_provenance.add(entry.book.provenance.value)
    run.funnel.survived(Stage.MARKETS_LOADED, len(entries))

    kalshi = by_platform.get(Platform.KALSHI, [])
    polymarket = by_platform.get(Platform.POLYMARKET, [])
    if not kalshi or not polymarket:
        run.funnel.dropped(
            Stage.PAIRS_PROPOSED,
            "one venue contributed no markets with books; no pair can exist",
            len(entries),
        )
        run.finished_at = utcnow()
        return run

    book_by_key = {
        (e.market.platform, e.market.platform_market_id): e.book for e in entries
    }
    market_by_key = {
        (e.market.platform, e.market.platform_market_id): e.market for e in entries
    }

    candidates = generate_candidates(
        [e.market for e in kalshi], [e.market for e in polymarket]
    )[:max_pairs]
    run.funnel.survived(Stage.PAIRS_PROPOSED, len(candidates))
    # Record the prescreen's rejections explicitly. Without this a run that proposed
    # nothing renders as "stage not reached", which reads as a pipeline that never
    # ran rather than as a matcher that compared every combination and liked none -
    # and those call for opposite fixes. It also makes the recall ceiling visible:
    # survivors over possible pairs is the fraction of the cross-product this
    # matcher is even willing to look at.
    possible = len(kalshi) * len(polymarket)
    run.funnel.dropped(
        Stage.PAIRS_PROPOSED,
        "prescreen found no shared tokens or entities",
        max(possible - len(candidates), 0),
    )

    window_start = min((e.book.observed_at for e in entries), default=None)
    window_end = max((e.book.observed_at for e in entries), default=None)
    if window_start and window_end:
        run.funnel.observation_window = f"{iso(window_start)} .. {iso(window_end)}"

    for candidate in candidates:
        try:
            _process_candidate(
                candidate,
                run=run,
                book_by_key=book_by_key,
                market_by_key=market_by_key,
                attestations=attestations,
                funding=funding,
                max_quote_age_seconds=max_quote_age_seconds,
                max_sets=max_sets,
                access=access,
            )
        except Exception as exc:  # noqa: BLE001
            run.errors.append(f"{candidate.key}: {exc}")

    run.finished_at = utcnow()
    return run


def _process_candidate(
    candidate: MatchCandidate,
    *,
    run: ShadowPairRun,
    book_by_key,
    market_by_key,
    attestations: AttestationStore,
    funding: FundingLedger,
    max_quote_age_seconds: int,
    max_sets: Decimal,
    access: Mapping[Platform, AccessStatus],
) -> None:
    market_a, market_b = candidate.market_a, candidate.market_b
    key_a = (market_a.platform, market_a.platform_market_id)
    key_b = (market_b.platform, market_b.platform_market_id)
    pair_label = f"{market_a.platform_market_id}|{market_b.platform_market_id}"

    score = score_equivalence(candidate)
    run.funnel.survived(Stage.RULES_COMPARED)

    if score.verdict is EquivalenceVerdict.REJECTED:
        run.funnel.dropped(Stage.EQUIVALENCE_VERIFIED, "verdict rejected")
        return
    if score.verdict not in (
        EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT,
        EquivalenceVerdict.VERIFIED_EQUIVALENT_STANDARD,
    ):
        run.funnel.dropped(
            Stage.EQUIVALENCE_VERIFIED, f"verdict {score.verdict.value}"
        )
        return
    run.funnel.survived(Stage.EQUIVALENCE_VERIFIED)

    book_a = book_by_key.get(key_a)
    book_b = book_by_key.get(key_b)
    if book_a is None or book_b is None:
        run.funnel.dropped(Stage.BOOKS_FRESH, "no book fetched for one side")
        return

    best: tuple[PairCandidate, Decimal] | None = None
    for side_a, side_b in _orientations(score):
        built = build_pair_candidate(
            pair_id=f"{pair_label}:{side_a.value}-{side_b.value}",
            market_a=market_by_key[key_a],
            book_a=book_a,
            side_a=side_a,
            market_b=market_by_key[key_b],
            book_b=book_b,
            side_b=side_b,
            score=score,
            attestations=attestations,
            max_quote_age_seconds=max_quote_age_seconds,
            access_status=access.get(market_a.platform, AccessStatus.UNVERIFIED)
            if access.get(market_a.platform) == access.get(market_b.platform)
            else AccessStatus.UNVERIFIED,
            max_sets=max_sets,
        )
        if isinstance(built, CandidateRejection):
            run.rejections.append((f"{pair_label}:{side_a.value}-{side_b.value}",
                                   built.stage.value, built.reason))
            run.funnel.dropped(built.stage, built.reason)
            continue
        gross = (
            built.leg_a.limit_price + built.leg_b.limit_price
        ) * built.target_quantity
        edge = built.target_quantity - gross
        if best is None or edge > best[1]:
            best = (built, edge)

    if best is None:
        return
    pair, edge = best
    run.funnel.survived(Stage.BOOKS_FRESH)
    if edge <= ZERO:
        run.funnel.dropped(
            Stage.GROSS_SPREAD, "both orientations cost at least $1 per share"
        )
        return
    run.funnel.survived(Stage.GROSS_SPREAD)

    clients = {
        pair.leg_a.contract.venue: BookBackedVenueClient(
            pair.leg_a.contract.venue,
            {pair.leg_a.contract.contract_key: ladder_from_book(book_a, _side_of(pair.leg_a))},
        ),
        pair.leg_b.contract.venue: BookBackedVenueClient(
            pair.leg_b.contract.venue,
            {pair.leg_b.contract.contract_key: ladder_from_book(book_b, _side_of(pair.leg_b))},
        ),
    }
    executor = TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=funding, clients=clients
    )
    journal = executor.run(pair)
    run.journals.append(journal)

    if journal.state is PairState.ABANDONED:
        reasons = journal.assessment.blocking_reasons() or [journal.abandon_reason]
        for reason in reasons:
            stage = (
                Stage.SETTLEMENT_PROVEN
                if reason.startswith("structural_status")
                else Stage.NET_POSITIVE
            )
            run.funnel.dropped(stage, reason)
        return

    run.funnel.survived(Stage.NET_POSITIVE)
    run.funnel.survived(Stage.SETTLEMENT_PROVEN)
    if pair.linkages and pair.linkages[0].covers_void:
        run.funnel.survived(Stage.FULLY_ATTESTED)
    else:
        run.funnel.dropped(Stage.FULLY_ATTESTED, "cancellation terms not attested")
    if journal.arbitrage_completed:
        run.funnel.survived(Stage.SHADOW_EXECUTED)
    else:
        run.funnel.dropped(Stage.SHADOW_EXECUTED, f"ended {journal.state.value}")


def _side_of(spec) -> Side:
    return spec.contract.outcome.to_side()
