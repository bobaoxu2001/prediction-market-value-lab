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

from pmvl_shared.timeutil import ensure_utc

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
    )
    rules.resolution_hash = rules.compute_hash()
    rules.summary = summarize(rules)
    return rules


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
