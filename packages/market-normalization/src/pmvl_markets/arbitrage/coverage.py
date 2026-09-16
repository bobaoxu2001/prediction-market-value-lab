"""How many pairs survived each filter, and what killed the rest.

The number that gets quoted about a system like this - "we verified N cross-platform
matches" - is close to meaningless on its own, because it does not say how many pairs
were examined to produce it, nor which filter removed the others. Worse, "N matches,
no opportunities" reads as "the market is efficient" when it usually means "the
matcher only looked at N pairs out of thousands".

So every stage records both a survivor count and the reasons for the drops, and
:meth:`CoverageFunnel.render` prints them together. A funnel whose first stage is
small is a *recall* problem and says nothing about whether arbitrage exists.

The stages are ordered by what they prove, not by what is cheap to compute. In
particular ``SETTLEMENT_PROVEN`` sits after ``NET_POSITIVE`` deliberately: a pair that
makes money on paper but cannot be proven to settle together is a finding about the
matcher, and burying it inside an earlier filter would hide how often it happens.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Stage", "CoverageFunnel"]


class Stage(StrEnum):
    """One filter in the pipeline, in the order they are applied."""

    MARKETS_LOADED = "markets_loaded"
    PAIRS_PROPOSED = "pairs_proposed"
    RULES_COMPARED = "rules_compared"
    EQUIVALENCE_VERIFIED = "equivalence_verified"
    BOOKS_FRESH = "books_fresh"
    GROSS_SPREAD = "gross_spread"
    NET_POSITIVE = "net_positive"
    SETTLEMENT_PROVEN = "settlement_proven"
    FULLY_ATTESTED = "fully_attested"
    SHADOW_EXECUTED = "shadow_executed"


#: What each stage actually establishes. Printed alongside the counts so a reader
#: cannot mistake "survived stage 4" for "is tradeable".
STAGE_MEANING: dict[Stage, str] = {
    Stage.MARKETS_LOADED: "markets read from each venue",
    Stage.PAIRS_PROPOSED: "pairs a matcher thought worth comparing (recall ceiling)",
    Stage.RULES_COMPARED: "pairs whose settlement terms were both normalised",
    Stage.EQUIVALENCE_VERIFIED: "pairs whose payout terms agree, term by term",
    Stage.BOOKS_FRESH: "pairs with a recent, non-empty book on both venues",
    Stage.GROSS_SPREAD: "pairs where the two asks sum to under $1 before costs",
    Stage.NET_POSITIVE: "pairs still positive after fees, depth and execution costs",
    Stage.SETTLEMENT_PROVEN: "pairs the settlement evaluator proves cannot lose",
    Stage.FULLY_ATTESTED: "pairs whose cancellation terms a human confirmed on both venues",
    Stage.SHADOW_EXECUTED: "pairs both legs of which filled in simulation",
}


@dataclass
class CoverageFunnel:
    """Survivor counts and drop reasons, one entry per stage."""

    label: str = ""
    observation_window: str = ""
    survivors: dict[Stage, int] = field(
        default_factory=lambda: {stage: 0 for stage in Stage}
    )
    drops: dict[Stage, Counter] = field(
        default_factory=lambda: {stage: Counter() for stage in Stage}
    )

    def survived(self, stage: Stage, count: int = 1) -> None:
        self.survivors[stage] += count

    def dropped(self, stage: Stage, reason: str, count: int = 1) -> None:
        """Record that ``count`` pairs failed to get past ``stage``, and why.

        The reason should be a *category*, not a formatted message with ids in it -
        the point is to be able to say "412 pairs dropped because Polymarket had no
        book", which a per-pair message cannot summarise.
        """
        self.drops[stage][reason] += count

    def entered(self, stage: Stage) -> int:
        """How many pairs reached this stage: survivors plus everything it dropped."""
        return self.survivors[stage] + sum(self.drops[stage].values())

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "observation_window": self.observation_window,
            "stages": [
                {
                    "stage": stage.value,
                    "establishes": STAGE_MEANING[stage],
                    "entered": self.entered(stage),
                    "survived": self.survivors[stage],
                    "dropped": dict(self.drops[stage]),
                }
                for stage in Stage
            ],
        }

    def render(self) -> str:
        """A funnel a person can read without a spreadsheet."""
        lines: list[str] = []
        if self.label:
            lines.append(self.label)
        if self.observation_window:
            lines.append(f"observation window: {self.observation_window}")
        lines.append("")
        for stage in Stage:
            entered = self.entered(stage)
            survived = self.survivors[stage]
            if entered == 0 and survived == 0:
                lines.append(f"  {stage.value:22} {'-':>8}   (stage not reached)")
                continue
            lines.append(
                f"  {stage.value:22} {survived:>8} / {entered:<8} "
                f"{STAGE_MEANING[stage]}"
            )
            for reason, count in self.drops[stage].most_common():
                lines.append(f"      dropped {count:>6}  {reason}")
        return "\n".join(lines)
