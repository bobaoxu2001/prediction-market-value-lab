"""Candidate generation for cross-platform market matching.

Comparing every Kalshi market against every Polymarket market is quadratic and
wasteful. This module cheaply proposes plausible pairs; :mod:`.verify` then does the
expensive, careful work of deciding whether a proposal is actually the same question.

Blocking strategy: a pair is only proposed if it shares at least one *discriminating*
token. Common words are excluded from the blocking index, because a token that
appears in a thousand markets creates a million useless pairs and buries the real
ones.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Iterable, Sequence

from pmvl_shared.enums import Category, Platform
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import ensure_utc

from ..normalize.text import TitleFeatures, extract_features, jaccard
from .fixtures import Fixture, extract_fixture, fixtures_agree

log = get_logger(__name__)

#: A token appearing in more than this fraction of markets is not discriminating.
MAX_TOKEN_DOCUMENT_FRACTION = 0.08
#: Absolute floor on that ceiling, so a small corpus stays matchable.
MIN_TOKEN_POSTING_CEILING = 20
#: Tokens shorter than this are ignored for blocking.
MIN_BLOCK_TOKEN_LENGTH = 4
#: Pairs whose resolution times differ by more than this are never the same question.
MAX_RESOLUTION_GAP = timedelta(days=3)


@dataclass
class MatchCandidate:
    """A proposed pair, with the cheap signals that justified proposing it."""

    market_a: NormalizedMarket
    market_b: NormalizedMarket
    features_a: TitleFeatures
    features_b: TitleFeatures
    token_similarity: float
    entity_overlap: float
    shared_tokens: tuple[str, ...]
    #: Set when the pair was proposed because both markets resolve on the *same
    #: head-to-head contest*, established structurally rather than by word overlap.
    #: The two venues title a game incompatibly - Kalshi names the outcome
    #: ("San Diego wins"), Polymarket the matchup ("San Diego Padres vs. San
    #: Francisco Giants") - so token similarity between a real pair is around 0.29
    #: and no amount of threshold tuning proposes them without this.
    fixture: tuple[Fixture, Fixture] | None = None
    #: Whether the two markets pay out on *opposite* competitors, which is the
    #: orientation a cross-venue hedge needs. Derived from the fixture, not from a
    #: comparator, because a moneyline has no comparator to invert.
    fixture_polarity_inverted: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (
            f"{self.market_a.platform.value}:{self.market_a.platform_market_id}",
            f"{self.market_b.platform.value}:{self.market_b.platform_market_id}",
        )

    @property
    def prescreen_score(self) -> float:
        """Cheap 0-1 plausibility, used only to order verification work."""
        return 0.6 * self.token_similarity + 0.4 * self.entity_overlap


def build_token_index(
    markets: Sequence[NormalizedMarket], features: dict[str, TitleFeatures]
) -> dict[str, set[str]]:
    """Inverted index of discriminating token -> market keys."""
    postings: dict[str, set[str]] = defaultdict(set)
    for market in markets:
        key = f"{market.platform.value}:{market.platform_market_id}"
        for token in features[key].tokens:
            if len(token) >= MIN_BLOCK_TOKEN_LENGTH and not token.isdigit():
                postings[token].add(key)

    if not markets:
        return {}
    # The fraction-based ceiling exists to drop tokens so common they generate
    # useless pairs. On a small corpus that same rule collapses to "a token may
    # appear in at most 2 markets", which discards almost every real signal, so an
    # absolute floor keeps small corpora matchable.
    ceiling = max(
        MIN_TOKEN_POSTING_CEILING, int(len(markets) * MAX_TOKEN_DOCUMENT_FRACTION)
    )
    # Only the UPPER bound filters. A token appearing in exactly one market is the
    # most discriminating signal there is - it points at a unique counterpart - so
    # excluding singletons would suppress precisely the matches worth finding.
    return {token: keys for token, keys in postings.items() if len(keys) <= ceiling}


def times_are_plausible(a: NormalizedMarket, b: NormalizedMarket) -> bool:
    """Reject pairs whose resolution windows cannot describe the same event."""
    ta = ensure_utc(a.expected_resolution_time)
    tb = ensure_utc(b.expected_resolution_time)
    if ta is None or tb is None:
        return True  # unknown times are checked properly during verification
    return abs(ta - tb) <= MAX_RESOLUTION_GAP


def categories_are_plausible(a: NormalizedMarket, b: NormalizedMarket) -> bool:
    """Allow a category mismatch only when one side is unclassified.

    The two venues taxonomise differently (a Fed market is 'economics' on one and
    'finance' on the other), so ``OTHER`` on either side is treated as unknown rather
    than as a contradiction.
    """
    if a.category == b.category:
        return True
    return Category.OTHER in (a.category, b.category)


def generate_candidates(
    markets_a: Sequence[NormalizedMarket],
    markets_b: Sequence[NormalizedMarket],
    *,
    min_token_similarity: float = 0.28,
    max_candidates_per_market: int = 6,
) -> list[MatchCandidate]:
    """Propose plausible cross-platform pairs between two market sets."""
    if not markets_a or not markets_b:
        return []

    combined = list(markets_a) + list(markets_b)
    features: dict[str, TitleFeatures] = {}
    for market in combined:
        key = f"{market.platform.value}:{market.platform_market_id}"
        features[key] = extract_features(
            market.title, subtitle=market.subtitle, description=market.description[:600]
        )

    index_b = build_token_index(list(markets_b), features)
    lookup_b = {f"{m.platform.value}:{m.platform_market_id}": m for m in markets_b}

    candidates: list[MatchCandidate] = []
    for market in markets_a:
        key_a = f"{market.platform.value}:{market.platform_market_id}"
        fa = features[key_a]

        # Gather the b-side markets sharing at least one discriminating token.
        hits: dict[str, int] = defaultdict(int)
        for token in fa.tokens:
            for key_b in index_b.get(token, ()):
                hits[key_b] += 1
        if not hits:
            continue

        scored: list[MatchCandidate] = []
        for key_b, _shared_count in sorted(hits.items(), key=lambda kv: -kv[1])[:40]:
            other = lookup_b[key_b]
            if not times_are_plausible(market, other):
                continue
            if not categories_are_plausible(market, other):
                continue
            fb = features[key_b]
            similarity = jaccard(fa.tokens, fb.tokens)
            if similarity < min_token_similarity:
                continue
            entity_overlap = jaccard(set(fa.entities[:8]), set(fb.entities[:8]))
            scored.append(
                MatchCandidate(
                    market_a=market,
                    market_b=other,
                    features_a=fa,
                    features_b=fb,
                    token_similarity=similarity,
                    entity_overlap=entity_overlap,
                    shared_tokens=tuple(sorted(fa.tokens & fb.tokens)),
                )
            )

        scored.sort(key=lambda c: c.prescreen_score, reverse=True)
        candidates.extend(scored[:max_candidates_per_market])

    # Merge in the structural proposer, preferring its annotation where both
    # proposers found the same pair. Dropping the fixture version as a duplicate
    # would silently discard the identity evidence and leave the verifier judging a
    # sports pair on word overlap - which is exactly what it cannot do.
    by_key = {c.key: c for c in generate_fixture_candidates(markets_a, markets_b, features=features)}
    total_fixture = len(by_key)
    merged = [by_key.pop(c.key, c) for c in candidates]
    merged.extend(by_key.values())

    log.info(
        "generated %d cross-platform candidate pairs from %d x %d markets "
        "(%d carry a fixture identity)",
        len(merged), len(markets_a), len(markets_b), total_fixture,
    )
    return merged


def generate_fixture_candidates(
    markets_a: Sequence[NormalizedMarket],
    markets_b: Sequence[NormalizedMarket],
    *,
    features: dict[str, TitleFeatures] | None = None,
) -> list[MatchCandidate]:
    """Propose pairs that resolve on the same head-to-head contest.

    A second, structural proposer alongside the token-similarity one. Word overlap
    cannot bridge the venues' sports naming: a real Kalshi/Polymarket moneyline pair
    scores 0.286, under the 0.28 blocking floor only by luck and under the 0.35 the
    verifier wants. Sports is the largest overlap between the two venues, so without
    this the scanner is blind to most of what the two venues actually both list.

    Bucketed by the contest date, which is required for a fixture match anyway: it
    keeps the comparison to the handful of games on one day, and it is the guard that
    stops the Monday and Tuesday games of the same series pairing with each other.
    """
    fixtures_b: dict[object, list[tuple[NormalizedMarket, Fixture]]] = defaultdict(list)
    for market in markets_b:
        fixture = extract_fixture(market)
        if fixture is not None:
            fixtures_b[fixture.scheduled].append((market, fixture))
    if not fixtures_b:
        return []

    features = features if features is not None else {}

    def feature_for(market: NormalizedMarket) -> TitleFeatures:
        key = f"{market.platform.value}:{market.platform_market_id}"
        if key not in features:
            features[key] = extract_features(
                market.title, subtitle=market.subtitle, description=market.description[:600]
            )
        return features[key]

    out: list[MatchCandidate] = []
    for market in markets_a:
        fixture_a = extract_fixture(market)
        if fixture_a is None:
            continue
        for other, fixture_b in fixtures_b.get(fixture_a.scheduled, ()):
            agree, inverted = fixtures_agree(fixture_a, fixture_b)
            if not agree:
                continue
            fa, fb = feature_for(market), feature_for(other)
            out.append(
                MatchCandidate(
                    market_a=market,
                    market_b=other,
                    features_a=fa,
                    features_b=fb,
                    token_similarity=jaccard(fa.tokens, fb.tokens),
                    entity_overlap=jaccard(set(fa.entities[:8]), set(fb.entities[:8])),
                    shared_tokens=tuple(sorted(fa.tokens & fb.tokens)),
                    fixture=(fixture_a, fixture_b),
                    fixture_polarity_inverted=inverted,
                )
            )
    return out


def split_by_platform(
    markets: Iterable[NormalizedMarket],
) -> dict[Platform, list[NormalizedMarket]]:
    out: dict[Platform, list[NormalizedMarket]] = defaultdict(list)
    for market in markets:
        out[market.platform].append(market)
    return dict(out)
