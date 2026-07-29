"""Controlled vocabularies shared by the database, API and UI."""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    DEMO = "demo"


class DataProvenance(StrEnum):
    """Where a row's numbers came from.

    This is the single most important field in the schema. Every market, quote,
    recommendation and arbitrage row carries it, and production surfaces filter on
    ``LIVE``. A ``DEMO`` row can never be rendered as a real opportunity.
    """

    LIVE = "live"           # fetched from a venue's production API
    FIXTURE = "fixture"     # a recorded real response, replayed (tests)
    DEMO = "demo"           # synthetic, illustrative only - never a real opportunity


class MarketStatus(StrEnum):
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class Side(StrEnum):
    YES = "yes"
    NO = "no"


class Category(StrEnum):
    WEATHER = "weather"
    SPORTS = "sports"
    ECONOMICS = "economics"
    CRYPTO = "crypto"
    FINANCE = "finance"
    POLITICS = "politics"
    GEOPOLITICS = "geopolitics"
    CULTURE = "culture"
    TECH = "tech"
    MENTIONS = "mentions"
    OTHER = "other"


class RuleCompatibility(StrEnum):
    """How confidently two markets can be treated as the same underlying question.

    Only ``IDENTICAL`` may back an "executable arbitrage" claim. Everything else is
    downgraded to a labelled, risk-flagged theoretical opportunity.
    """

    IDENTICAL = "identical"       # same event, source, cutoff, and measurement basis
    EQUIVALENT = "equivalent"     # semantically same, immaterial wording differences
    SIMILAR = "similar"           # same subject, at least one material term differs
    INCOMPATIBLE = "incompatible"


class ArbitrageKind(StrEnum):
    COMPLETE_SET = "complete_set"
    CROSS_PLATFORM = "cross_platform"
    MULTI_OUTCOME = "multi_outcome"
    LOGICAL_CONSTRAINT = "logical_constraint"
    STALE_QUOTE = "stale_quote"


class ArbitrageLabel(StrEnum):
    """Honest classification. ``EXECUTABLE`` has a hard, auditable definition."""

    EXECUTABLE = "executable"
    THEORETICAL = "theoretical_arbitrage"
    RULE_MISMATCH_RISK = "rule_mismatch_risk"
    EXECUTION_RISK = "execution_risk"
    STALE_QUOTE = "stale_quote"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    NOT_GUARANTEED = "not_guaranteed"
    LOGICAL_MISPRICING = "logical_mispricing"


class RecommendationState(StrEnum):
    """Lifecycle of a published recommendation. Snapshots are never overwritten."""

    STILL_ACTIONABLE = "still_actionable"
    EDGE_REDUCED = "edge_reduced"
    NO_LONGER_ACTIONABLE = "no_longer_actionable"
    MARKET_CLOSED = "market_closed"
    SETTLED = "settled"


class SettlementResult(StrEnum):
    YES = "yes"
    NO = "no"
    VOID = "void"
    FIFTY_FIFTY = "fifty_fifty"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    """Lifecycle of one scheduled run.

    PARTIAL_SUCCESS exists because collapsing it into either neighbour loses the
    thing an operator needs. Called SUCCESS, a run that lost Polymarket looks
    healthy and the resulting shorter opportunity list reads as "no opportunities
    today". Called FAILED, a run that refreshed 90% of markets discards work that
    is good. STALE marks a run whose inputs were already past their hard freshness
    threshold, so its output should not gate recommendations.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    SKIPPED = "skipped"
    STALE = "stale"

    @property
    def produced_usable_output(self) -> bool:
        """Whether downstream jobs may consume this run's output."""
        return self in (JobStatus.SUCCESS, JobStatus.PARTIAL_SUCCESS)

    @property
    def is_terminal(self) -> bool:
        return self not in (JobStatus.PENDING, JobStatus.RUNNING)


class EvidenceStance(StrEnum):
    SUPPORTS_YES = "supports_yes"
    SUPPORTS_NO = "supports_no"
    NEUTRAL = "neutral"
    CONFLICTING = "conflicting"


class DataQuality(StrEnum):
    """Backtest honesty flag: what the simulated fill was actually derived from."""

    ORDERBOOK = "orderbook"        # real depth snapshot at decision time
    QUOTE = "quote"                # top-of-book only, depth unknown
    CANDLE = "candle"              # OHLC bar; NOT an executable price
    UNKNOWN = "unknown"


class VenueAvailability(StrEnum):
    """Where a contract has been *observed*, kept distinct from where it might trade.

    A market existing on Kalshi says nothing about whether any particular broker
    lists it. Brokers carry a subset of an exchange's contracts, change that subset
    without notice, and gate it by jurisdiction and account type. Inferring broker
    availability from exchange availability would produce a confident claim that a
    user cannot act on - the failure mode this enum exists to prevent.

    The platform only ever asserts what it has actually seen through an API it reads.
    Anything else is UNVERIFIED, and UNVERIFIED is the default.
    """

    #: Observed directly in the venue's own public API during ingest.
    OBSERVED_VIA_PUBLIC_API = "observed_via_public_api"
    #: A discovery source confirmed the contract is listed and tradeable there.
    CONFIRMED_AVAILABLE = "confirmed_available"
    #: A discovery source was consulted and the contract was NOT listed.
    CONFIRMED_UNAVAILABLE = "confirmed_unavailable"
    #: No reliable discovery source exists for this venue. The default.
    UNVERIFIED = "unverified"

    @property
    def is_actionable_claim(self) -> bool:
        """Whether this status may be presented as somewhere a user can trade."""
        return self in (
            VenueAvailability.OBSERVED_VIA_PUBLIC_API,
            VenueAvailability.CONFIRMED_AVAILABLE,
        )

    @property
    def display_label(self) -> str:
        return {
            VenueAvailability.OBSERVED_VIA_PUBLIC_API: "Observed via public API",
            VenueAvailability.CONFIRMED_AVAILABLE: "Confirmed available",
            VenueAvailability.CONFIRMED_UNAVAILABLE: "Confirmed unavailable",
            VenueAvailability.UNVERIFIED: "Unverified",
        }[self]


#: Venues the platform reads directly. Anything here can reach OBSERVED_VIA_PUBLIC_API.
DISCOVERABLE_VENUES: frozenset[str] = frozenset({"kalshi", "polymarket"})

#: Venues with no contract-discovery source wired up. These are pinned to UNVERIFIED
#: and MUST NOT be inferred from an exchange listing. Moomoo is the live example: it
#: brokers some Kalshi event contracts, but there is no public endpoint here that
#: enumerates which ones, so the honest answer is that we do not know.
UNDISCOVERABLE_VENUES: frozenset[str] = frozenset({"moomoo", "robinhood", "ibkr"})


def availability_for(venue: str, *, observed_platforms: frozenset[str] | set[str]) -> VenueAvailability:
    """Availability of a contract on ``venue``, given where it was actually observed.

    Deliberately has no path from "listed on Kalshi" to "available on Moomoo".
    """
    key = venue.strip().lower()
    if key in UNDISCOVERABLE_VENUES:
        return VenueAvailability.UNVERIFIED
    if key in {p.strip().lower() for p in observed_platforms}:
        return VenueAvailability.OBSERVED_VIA_PUBLIC_API
    if key in DISCOVERABLE_VENUES:
        # Discoverable and we did look, but this contract was not among the results.
        return VenueAvailability.CONFIRMED_UNAVAILABLE
    return VenueAvailability.UNVERIFIED


class OpportunityClass(StrEnum):
    """What kind of claim an opportunity actually is.

    The word "arbitrage" is reserved for a terminal payout that is guaranteed under
    every valid settlement state. Everything weaker gets a name that says how it is
    weaker, so a reader never has to infer the strength of a claim from its styling.
    """

    #: Payout guaranteed in every settlement state, after all costs, at a size the
    #: book can actually fill. The only class that may be called arbitrage.
    GUARANTEED_ARBITRAGE = "guaranteed_arbitrage"
    #: The price relationship is genuinely riskless, but depth, venue status or leg
    #: synchronisation means it cannot be captured at a meaningful size.
    EXECUTION_CONSTRAINED_ARBITRAGE = "execution_constrained_arbitrage"
    #: Positive expected value against an independent probability estimate, with real
    #: downside. Not guaranteed.
    RELATIVE_VALUE = "relative_value"
    #: The model and the market differ, but the difference did not survive costs or
    #: the conservative bound. Research signal only.
    MODEL_DISAGREEMENT = "model_disagreement"
    #: Worth monitoring; no actionable claim today.
    WATCHLIST = "watchlist"
    #: Evaluated and declined, with a recorded reason.
    REJECTED = "rejected"

    @property
    def is_guaranteed(self) -> bool:
        return self is OpportunityClass.GUARANTEED_ARBITRAGE

    @property
    def may_be_called_arbitrage(self) -> bool:
        """Only a guaranteed terminal payout earns the word."""
        return self is OpportunityClass.GUARANTEED_ARBITRAGE

    @property
    def is_actionable(self) -> bool:
        return self in (
            OpportunityClass.GUARANTEED_ARBITRAGE,
            OpportunityClass.RELATIVE_VALUE,
        )


#: How the arbitrage scanners' honesty labels map onto the public taxonomy.
#:
#: EXECUTABLE is the only label that survives as a guarantee. Everything else names
#: a specific unmet precondition and is demoted accordingly - a stale quote is a race,
#: a rule mismatch can lose on both legs, and a logical mispricing is information
#: rather than a harvestable trade unless a complete hedge exists.
ARBITRAGE_LABEL_TO_CLASS: dict[str, OpportunityClass] = {
    "executable": OpportunityClass.GUARANTEED_ARBITRAGE,
    "theoretical_arbitrage": OpportunityClass.EXECUTION_CONSTRAINED_ARBITRAGE,
    "insufficient_liquidity": OpportunityClass.EXECUTION_CONSTRAINED_ARBITRAGE,
    "execution_risk": OpportunityClass.EXECUTION_CONSTRAINED_ARBITRAGE,
    "stale_quote": OpportunityClass.EXECUTION_CONSTRAINED_ARBITRAGE,
    "rule_mismatch_risk": OpportunityClass.MODEL_DISAGREEMENT,
    "not_guaranteed": OpportunityClass.MODEL_DISAGREEMENT,
    "logical_mispricing": OpportunityClass.MODEL_DISAGREEMENT,
}


def classify_arbitrage_label(label: str) -> OpportunityClass:
    """Map an arbitrage honesty label onto the public taxonomy, defaulting to weak."""
    return ARBITRAGE_LABEL_TO_CLASS.get(label, OpportunityClass.WATCHLIST)


class ScanOutcome(StrEnum):
    """What happened to a candidate opportunity during a scan.

    Reporting only "no arbitrage found" tells a reader nothing about WHY. These codes
    let the pipeline answer the question the empty list raises: was there no price
    difference, did fees eat it, was the book too thin, or did the rules not match?
    """

    ACTIONABLE = "actionable"
    REJECTED_PRICE = "rejected_price"
    REJECTED_FEES = "rejected_fees"
    REJECTED_MARGIN = "rejected_margin"
    REJECTED_DEPTH = "rejected_depth"
    REJECTED_RULES = "rejected_rules"
    REJECTED_STALE = "rejected_stale"
    DIAGNOSTIC = "diagnostic"

    @property
    def is_actionable(self) -> bool:
        return self is ScanOutcome.ACTIONABLE

    @property
    def label(self) -> str:
        return {
            ScanOutcome.ACTIONABLE: "Cleared every gate",
            ScanOutcome.REJECTED_PRICE: "No price difference to capture",
            ScanOutcome.REJECTED_FEES: "Fees and slippage consumed the edge",
            ScanOutcome.REJECTED_MARGIN: "Edge below the required safety margin",
            ScanOutcome.REJECTED_DEPTH: "Order book too thin to fill",
            ScanOutcome.REJECTED_RULES: "Settlement rules are not equivalent",
            ScanOutcome.REJECTED_STALE: "Quotes too old to act on",
            ScanOutcome.DIAGNOSTIC: "Research signal, not actionable",
        }[self]


#: Labels a user would reasonably expect to see under the heading "Arbitrage
#: opportunities". Everything else is a research diagnostic: real output, but not a
#: trade. Mixing them lets a 25-hour-old quote with negative net profit sit in a list
#: a reader takes as actionable.
ACTIONABLE_ARBITRAGE_LABELS: frozenset[str] = frozenset(
    {ArbitrageLabel.EXECUTABLE.value}
)


def is_actionable_label(label: str) -> bool:
    return label in ACTIONABLE_ARBITRAGE_LABELS
