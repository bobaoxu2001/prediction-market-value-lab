"""The ladder of claims, and the evidence each rung actually requires.

The failure this module exists to prevent is verbal, not numerical. "We found an
arbitrage" can mean the settlement mathematics is sound, or that two quotes summed to
less than a dollar, or that money was made. Those are three different claims with three
different evidence requirements, and a single word for all of them lets the weakest one
borrow the authority of the strongest.

So each rung is a separate enum member with its own gate, and :func:`classify` will not
return a rung whose evidence is absent. The rungs are ordered and **not** interchangeable:

``STRUCTURAL_PROOF_ONLY``
    The settlement mathematics holds. Says nothing whatever about prices.
``GROSS_ARBITRAGE_OBSERVED``
    Quotes existed that beat the payoff floor before fees.
``NET_ARBITRAGE_OBSERVED``
    ``payoff_floor - executable_cost - fees > 0``. Still not executable: says nothing
    about whether the legs coexisted or could be filled.
``EXECUTABLE_ARBITRAGE_OBSERVED``
    Adds simultaneity, real depth at the quoted levels, an open market, marginal
    sizing and a pessimistic fee. This is the first rung that describes a trade.
``REPLAY_VALIDATED``
    The verdict recomputes from stored inputs with no network.
``OUT_OF_SAMPLE_VALIDATED``
    Reproduced on data collected after the strategy version was frozen.
``LIVE_SHADOW_VALIDATED``
    Observed in a live shadow run rather than a stored scan.
``STABLE_PROFIT_EVIDENCE``
    Repeated across independent event-days, not driven by one outlier, with capacity
    and surviving stress. The only rung that is a claim about a *strategy*.
``INSUFFICIENT_EVIDENCE``
    The checks that would decide it were not run or not answerable.
``FALSIFIED``
    A check that the claim depends on came back negative.

``FALSIFIED`` is deliberately not the bottom of the ladder. A falsified claim is a
stronger result than an unevaluated one, and collapsing the two would let "we never
checked" and "we checked and it failed" print the same word.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.money import ZERO, quantize_usd

__all__ = [
    "ProofLevel",
    "GateResult",
    "Gate",
    "ProofBundle",
    "classify",
]


class ProofLevel(StrEnum):
    STRUCTURAL_PROOF_ONLY = "STRUCTURAL_PROOF_ONLY"
    GROSS_ARBITRAGE_OBSERVED = "GROSS_ARBITRAGE_OBSERVED"
    NET_ARBITRAGE_OBSERVED = "NET_ARBITRAGE_OBSERVED"
    EXECUTABLE_ARBITRAGE_OBSERVED = "EXECUTABLE_ARBITRAGE_OBSERVED"
    REPLAY_VALIDATED = "REPLAY_VALIDATED"
    OUT_OF_SAMPLE_VALIDATED = "OUT_OF_SAMPLE_VALIDATED"
    LIVE_SHADOW_VALIDATED = "LIVE_SHADOW_VALIDATED"
    STABLE_PROFIT_EVIDENCE = "STABLE_PROFIT_EVIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    FALSIFIED = "FALSIFIED"


#: Rungs in ascending order of what they claim. The terminal verdicts are absent
#: because they are not positions on the ladder - they are refusals to place one.
ASCENDING: tuple[ProofLevel, ...] = (
    ProofLevel.STRUCTURAL_PROOF_ONLY,
    ProofLevel.GROSS_ARBITRAGE_OBSERVED,
    ProofLevel.NET_ARBITRAGE_OBSERVED,
    ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED,
    ProofLevel.REPLAY_VALIDATED,
    ProofLevel.OUT_OF_SAMPLE_VALIDATED,
    ProofLevel.LIVE_SHADOW_VALIDATED,
    ProofLevel.STABLE_PROFIT_EVIDENCE,
)


class GateResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: The check could not be answered from the evidence available. Blocks promotion
    #: exactly as FAIL does, and is reported differently because the work it implies
    #: is different: collect the evidence, versus abandon the claim.
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


@dataclass(frozen=True)
class Gate:
    name: str
    result: GateResult
    detail: str = ""
    #: The rung this gate is a precondition for. A gate that fails caps the verdict
    #: at the rung *below* the one it guards.
    guards: ProofLevel = ProofLevel.STRUCTURAL_PROOF_ONLY

    def as_dict(self) -> dict[str, object]:
        return {
            "gate": self.name,
            "result": self.result.value,
            "detail": self.detail,
            "guards": self.guards.value,
        }


@dataclass
class ProofBundle:
    """A verdict, the gates that produced it, and the numbers behind them."""

    verdict: ProofLevel
    gates: list[Gate] = field(default_factory=list)
    reason: str = ""
    largest_uncertainty: str = ""
    metrics: dict[str, object] = field(default_factory=dict)

    @property
    def failed(self) -> list[Gate]:
        return [g for g in self.gates if g.result is GateResult.FAIL]

    @property
    def unestablished(self) -> list[Gate]:
        return [g for g in self.gates if g.result is GateResult.NOT_ESTABLISHED]

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "largest_uncertainty": self.largest_uncertainty,
            "gates": [g.as_dict() for g in self.gates],
            "metrics": self.metrics,
        }


def classify(gates: list[Gate]) -> ProofBundle:
    """Take the highest rung every gate permits, and never higher.

    A gate that FAILs caps the verdict below the rung it guards. A gate that is
    NOT_ESTABLISHED caps it the same way but is reported separately, because "we
    checked and it is false" and "we could not check" call for different work and
    only one of them is a finding about the market.

    Promotion is *conjunctive over the whole ladder below*, not just over the gates
    naming a given rung: a structure whose simultaneity gate failed cannot be
    REPLAY_VALIDATED, because replaying a combination that never coexisted reproduces
    the error faithfully rather than validating it.
    """
    if not gates:
        return ProofBundle(
            ProofLevel.INSUFFICIENT_EVIDENCE,
            reason="no gates were evaluated",
            largest_uncertainty="everything",
        )

    blocking = [g for g in gates if g.result is not GateResult.PASS]
    if not blocking:
        return ProofBundle(
            ProofLevel.STABLE_PROFIT_EVIDENCE,
            gates=gates,
            reason="every gate passed",
        )

    # The lowest rung any blocking gate guards decides the ceiling.
    lowest_blocked = min(blocking, key=lambda g: ASCENDING.index(g.guards))
    index = ASCENDING.index(lowest_blocked.guards)
    verdict = ASCENDING[index - 1] if index > 0 else ProofLevel.INSUFFICIENT_EVIDENCE

    hard_fail = [g for g in blocking if g.result is GateResult.FAIL]
    if hard_fail and index == 0:
        verdict = ProofLevel.FALSIFIED

    decisive = hard_fail[0] if hard_fail else blocking[0]
    others = [g for g in blocking if g is not decisive]
    return ProofBundle(
        verdict,
        gates=gates,
        reason=f"{decisive.name}: {decisive.detail}",
        largest_uncertainty=(
            f"{others[0].name}: {others[0].detail}" if others else "none beyond the above"
        ),
    )


def net_floor_gate(
    payoff_floor: Decimal, executable_cost: Decimal, fees: Decimal
) -> Gate:
    """The arithmetic rung: floor minus what it actually costs, fees included."""
    net = payoff_floor - executable_cost - fees
    return Gate(
        "net_floor_positive",
        GateResult.PASS if net > ZERO else GateResult.FAIL,
        f"floor ${quantize_usd(payoff_floor)} - cost ${quantize_usd(executable_cost)} "
        f"- fees ${quantize_usd(fees)} = ${quantize_usd(net)}",
        guards=ProofLevel.NET_ARBITRAGE_OBSERVED,
    )


def simultaneity_gate(skew_seconds: float | None, limit_seconds: float) -> Gate:
    """The rung that the stored-book results failed.

    A multi-leg floor asserts that a set of prices was purchasable together. Legs read
    minutes apart are separate market states, and combining them prices a basket that
    was never simultaneously on offer - which is how a 2.99-hour-old Solana quote
    produced a $33 "guaranteed" profit that never existed.
    """
    if skew_seconds is None:
        return Gate(
            "simultaneous_quotes",
            GateResult.NOT_ESTABLISHED,
            "quote timestamps were not recorded on every leg",
            guards=ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED,
        )
    ok = skew_seconds <= limit_seconds
    return Gate(
        "simultaneous_quotes",
        GateResult.PASS if ok else GateResult.FAIL,
        f"legs quoted {skew_seconds:.1f}s apart (limit {limit_seconds:.1f}s)",
        guards=ProofLevel.EXECUTABLE_ARBITRAGE_OBSERVED,
    )
