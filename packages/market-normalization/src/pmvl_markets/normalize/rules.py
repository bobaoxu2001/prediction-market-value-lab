"""Settlement-rule normalization and the deterministic resolution hash.

Two markets can share a title and still settle differently. The ``resolution_hash``
digests only the terms that *change who gets paid*:

    entity set | comparator | threshold | measurement basis | cutoff (UTC hour) |
    settlement source family | overtime inclusion | revised-data inclusion

Equal hashes are a **necessary but not sufficient** condition for calling two markets
identical - :mod:`pmvl_markets.matching.verify` still checks each term individually
and records the specific mismatches. The hash exists to make the comparison cheap and
reproducible, not to be the final authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pmvl_shared.timeutil import ensure_utc, iso, parse_ts

from .text import TitleFeatures, extract_features

#: Settlement sources grouped into families. Two markets citing "CF Benchmarks BRTI"
#: and "CF Benchmarks Real-Time Index" are the same source; "Coinbase spot" is not.
_SOURCE_FAMILIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"cf benchmark|brti|brr\b", re.I), "cf_benchmarks"),
    (re.compile(r"\bcoinbase\b", re.I), "coinbase"),
    (re.compile(r"\bbinance\b", re.I), "binance"),
    (re.compile(r"national weather service|\bnws\b|noaa", re.I), "nws"),
    (re.compile(r"bureau of labor statistics|\bbls\b", re.I), "bls"),
    (re.compile(r"bureau of economic analysis|\bbea\b", re.I), "bea"),
    (re.compile(r"federal reserve|\bfomc\b", re.I), "federal_reserve"),
    (re.compile(r"associated press|\bap\b(?![a-z])", re.I), "associated_press"),
    (re.compile(r"\bespn\b|sports reference|official league", re.I), "sports_official"),
    (re.compile(r"\buma\b|optimistic oracle|consensus of credible sources", re.I), "uma_oracle"),
    (re.compile(r"s&p dow jones|\bcme\b|nasdaq inc", re.I), "exchange_official"),
]


def source_family(text: str) -> str:
    """Map a free-text settlement source onto a comparable family token.

    Returns ``""`` when no known family matches. There is deliberately no
    "first significant word" fallback: Polymarket rules almost all begin "This market
    will resolve...", so such a fallback would assign the family ``this`` to
    unrelated markets and manufacture *source compatibility* between them - exactly
    the false-arbitrage signal this module exists to prevent. An unknown source is
    recorded as unknown and downgrades the match.
    """
    if not text:
        return ""
    for pattern, family in _SOURCE_FAMILIES:
        if pattern.search(text):
            return family
    return ""


class VoidPolicy(StrEnum):
    """What a venue says happens when the question cannot be settled as written.

    This is the term that decides whether a matched cross-venue pair is a hedge or a
    coin flip. If one venue voids a cancelled event and refunds the stake while the
    other resolves it NO, a "riskless" pair loses the NO leg's premium and only gets
    the YES leg's stake back - a loss on a position that was sold as a guarantee.

    ``UNSTATED`` is a real answer, not a default to fall back on: a venue that says
    nothing about cancellation has committed to nothing, and an unverified term is
    not a verified one.
    """

    #: Stake returned - "void", "refunded", "annulled", or an explicit 50-50 split.
    VOID_REFUND = "void_refund"
    #: The contract still settles; non-occurrence decides the question outright.
    RESOLVE_OUTRIGHT = "resolve_outright"
    #: Settlement waits - the deadline moves to the rescheduled date.
    EXTENDED = "extended"
    #: Nothing the venue published addresses this, or what it published is ambiguous.
    UNSTATED = ""


#: Sentences that raise the question. Mirrors the cue lists the ingest store already
#: uses to preserve each venue's own cancellation and postponement wording, so the
#: classifier reads exactly the sentences a human auditor would be shown.
CANCELLATION_TRIGGERS = (
    "cancel", "void", "annul", "no contest", "not take place", "does not occur",
    "do not occur", "fails to occur", "never occurs", "abandon",
)
POSTPONEMENT_TRIGGERS = (
    "postpone", "delay", "reschedul", "suspend", "not completed", "moved to",
)

#: The consequence a sentence commits to. Ordered most specific first, so that
#: "resolve 50-50" is read as a refund rather than matching the bare "resolve" rule
#: below it.
_CONSEQUENCE_CUES: list[tuple[re.Pattern[str], "VoidPolicy"]] = [
    (re.compile(r"50\s*[-/:]\s*50|split\s+evenly|each\s+side\s+receives"), "VOID_REFUND"),
    (re.compile(r"\bvoid(ed|s)?\b|\brefund(ed|s)?\b|\bannul(led|s)?\b|\bno contest\b"),
     "VOID_REFUND"),
    (re.compile(r"\bresolv\w*\s+(to\s+|as\s+)?(no|yes)\b"), "RESOLVE_OUTRIGHT"),
    (re.compile(r"\bextend(ed|s)?\b|\bremain(s)?\s+open\b|\bpushed\s+back\b|"
                r"\bnew\s+(resolution\s+)?date\b|\brescheduled\s+date\b|"
                r"\buntil\s+(the\s+)?(event|game|match|contest)\b"),
     "EXTENDED"),
]


def _sentences(text: str) -> list[str]:
    """Split rule prose into sentences, tolerating the venues' loose punctuation."""
    if not text:
        return []
    flattened = re.sub(r"\s+", " ", text.replace("\n", " "))
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", flattened) if s.strip()]


def _plain(sentence: str) -> str:
    """Lowercase and strip the quoting the venues wrap outcome names in.

    Polymarket writes resolve to "No" with curly or straight quotes depending on the
    market; matching on the quoted form would make the classifier depend on which.
    """
    lowered = sentence.lower()
    for ch in ('"', "'", "“", "”", "‘", "’"):
        lowered = lowered.replace(ch, "")
    return lowered


def classify_void_language(text: str, triggers: tuple[str, ...]) -> VoidPolicy:
    """Read the venue's own words and name the consequence it committed to.

    Only sentences that actually raise the trigger are considered, and the
    consequence must appear in the *same* sentence. A rule that says "resolves NO if
    the price is below the strike" elsewhere in the text is not a statement about
    cancellation, and reading it as one would manufacture agreement between two
    venues that never addressed the question.

    Two different consequences across the matched sentences returns ``UNSTATED``
    rather than picking one. An ambiguous commitment is not a commitment, and this
    value gates a risk-free claim.
    """
    found: set[VoidPolicy] = set()
    for sentence in _sentences(text):
        plain = _plain(sentence)
        if not any(cue in plain for cue in triggers):
            continue
        for pattern, policy_name in _CONSEQUENCE_CUES:
            if pattern.search(plain):
                found.add(VoidPolicy[policy_name])
                break
    if len(found) == 1:
        return found.pop()
    return VoidPolicy.UNSTATED


@dataclass
class NormalizedRules:
    """Structured settlement terms, plus the digest computed from them."""

    entities: list[str] = field(default_factory=list)
    comparator: str = ""
    threshold: Decimal | None = None
    threshold_semantics: str = ""
    cutoff_utc: datetime | None = None
    settlement_source_family: str = ""
    settlement_source_raw: str = ""
    includes_overtime: bool | None = None
    uses_revised_data: bool | None = None
    #: How the venue says it handles a cancelled or a postponed event. Deliberately
    #: **outside** ``hash_payload`` - the digest stays a statement about the terms
    #: that decide a *normal* payout, and void handling is compared as its own
    #: equivalence component so a reader can see which of the two disagreed.
    cancellation_policy: str = ""
    postponement_policy: str = ""
    resolution_hash: str = ""
    summary: str = ""

    def hash_payload(self) -> dict[str, object]:
        """The exact terms that go into the digest, in a stable order."""
        return {
            # Top entities only: tail tokens are noise and differ between venues.
            "entities": sorted(self.entities[:6]),
            "comparator": self.comparator,
            "threshold": str(self.threshold) if self.threshold is not None else "",
            "semantics": self.threshold_semantics,
            # Hour resolution: minute-level differences in publication time do not
            # change the outcome, but a different hour usually does.
            "cutoff": self.cutoff_utc.strftime("%Y-%m-%dT%H") if self.cutoff_utc else "",
            "source": self.settlement_source_family,
            "overtime": self.includes_overtime,
            "revised": self.uses_revised_data,
        }

    def compute_hash(self) -> str:
        blob = json.dumps(self.hash_payload(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["threshold"] = str(self.threshold) if self.threshold is not None else None
        data["cutoff_utc"] = self.cutoff_utc.isoformat() if self.cutoff_utc else None
        return data


def normalize_rules(
    *,
    title: str,
    subtitle: str = "",
    description: str = "",
    settlement_source: str = "",
    cutoff_time: datetime | None = None,
    explicit_threshold: Decimal | None = None,
    explicit_comparator: str = "",
    has_structured_strike: bool = False,
    features: TitleFeatures | None = None,
) -> NormalizedRules:
    """Build :class:`NormalizedRules` from a market's text and structured fields.

    Structured venue fields (Kalshi's ``floor_strike``/``strike_type``) always win
    over text extraction - they are authoritative, whereas a regex over a title is a
    best effort.

    When the venue publishes structured strike metadata but *no* numeric strike, the
    market's condition is genuinely non-numeric ("finish top 20", "win the series").
    Text extraction is suppressed in that case: substituting a number scraped from
    the title would mix an authoritative comparator with a guessed threshold and
    produce a confident-looking but wrong rule.
    """
    features = features or extract_features(title, subtitle=subtitle, description=description)

    if explicit_threshold is not None:
        threshold = explicit_threshold
    elif has_structured_strike:
        threshold = None
    else:
        threshold = features.primary_threshold
    comparator = explicit_comparator or features.comparator

    rules = NormalizedRules(
        entities=features.entities,
        comparator=comparator,
        threshold=threshold,
        threshold_semantics=features.threshold_semantics,
        cutoff_utc=ensure_utc(cutoff_time),
        settlement_source_family=source_family(settlement_source or description),
        settlement_source_raw=(settlement_source or "").strip(),
        includes_overtime=features.includes_overtime,
        uses_revised_data=features.uses_revised_data,
        cancellation_policy=classify_void_language(
            description, CANCELLATION_TRIGGERS
        ).value,
        postponement_policy=classify_void_language(
            description, POSTPONEMENT_TRIGGERS
        ).value,
    )
    rules.resolution_hash = rules.compute_hash()
    rules.summary = summarize(rules)
    return rules


def market_rule_inputs(
    *,
    title: str,
    subtitle: str = "",
    description: str = "",
    settlement_source: str = "",
    cutoff_time: datetime | None = None,
    explicit_threshold: Decimal | None = None,
    explicit_comparator: str = "",
    has_structured_strike: bool = False,
) -> dict[str, Any]:
    """The exact inputs that produced a market's normalized rules, JSON-safe.

    Providers stamp this vector onto the market at ingest time and build the rules
    *through* it. The store then replays the same vector via
    :func:`rules_from_inputs` instead of re-deriving inputs from market columns, so
    the hash the provider stamped and the hash persisted on the rule row are one
    construction - a choice the provider made once (Polymarket's endDate cutoff, a
    source fallback that prefers the description) can never silently diverge when
    the row is written later.
    """
    return {
        "title": title,
        "subtitle": subtitle or "",
        "description": description or "",
        "settlement_source": settlement_source or "",
        "cutoff_time": iso(cutoff_time),
        "explicit_threshold": (
            str(explicit_threshold) if explicit_threshold is not None else None
        ),
        "explicit_comparator": explicit_comparator or "",
        "has_structured_strike": bool(has_structured_strike),
    }


def rules_from_inputs(inputs: dict[str, Any]) -> NormalizedRules:
    """Reconstruct :class:`NormalizedRules` from a stored input vector.

    The inverse of :func:`market_rule_inputs`: parsing the exact inputs back into
    the call :func:`normalize_rules` made at ingest time, so the digest is stable
    across the provider -> persistence -> reload round-trip by construction.
    """
    threshold_raw = inputs.get("explicit_threshold")
    explicit_threshold = (
        Decimal(str(threshold_raw)) if threshold_raw not in (None, "") else None
    )
    return normalize_rules(
        title=str(inputs.get("title") or ""),
        subtitle=str(inputs.get("subtitle") or ""),
        description=str(inputs.get("description") or ""),
        settlement_source=str(inputs.get("settlement_source") or ""),
        cutoff_time=parse_ts(inputs.get("cutoff_time")),
        explicit_threshold=explicit_threshold,
        explicit_comparator=str(inputs.get("explicit_comparator") or ""),
        has_structured_strike=bool(inputs.get("has_structured_strike")),
    )


def summarize(rules: NormalizedRules) -> str:
    """One-line human-readable restatement of the normalized terms."""
    parts: list[str] = []
    if rules.entities:
        parts.append(" ".join(rules.entities[:4]))
    if rules.comparator and rules.threshold is not None:
        symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=",
                  "between": "between"}.get(rules.comparator, rules.comparator)
        parts.append(f"{symbol} {rules.threshold}")
    if rules.threshold_semantics:
        parts.append(f"[{rules.threshold_semantics}]")
    if rules.cutoff_utc:
        parts.append(f"by {rules.cutoff_utc.strftime('%Y-%m-%d %H:%MZ')}")
    if rules.settlement_source_family:
        parts.append(f"src={rules.settlement_source_family}")
    return " ".join(parts) if parts else "unstructured"


#: Kalshi ``strike_type`` -> canonical comparator.
KALSHI_STRIKE_COMPARATORS = {
    "greater": "gt",
    "greater_or_equal": "gte",
    "less": "lt",
    "less_or_equal": "lte",
    "between": "between",
    "functional": "",
    "custom": "",
    "structured": "",
}
