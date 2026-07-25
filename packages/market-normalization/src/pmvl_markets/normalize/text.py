"""Title, entity, date and threshold normalization.

Cross-platform matching lives or dies here. The same underlying question is phrased
very differently by the two venues:

    Kalshi      "Will the high temp in NYC be >87° on Jul 25, 2026?"
    Polymarket  "Highest temperature in New York City on July 25?"

The job of this module is to strip venue-specific packaging down to a comparable
core, and - critically - to *extract* the numeric thresholds and comparators so the
verification step can reject pairs that merely look similar.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pmvl_shared.money import d_or_none

# Venue boilerplate that carries no semantic content.
_STOP_PHRASES = [
    "will there be", "will the", "will a", "will", "does the", "do the", "is the",
    "are the", "how many", "what will", "what is the", "which", "who will", "who",
    "by the end of", "at the end of", "as of", "according to",
    "this market will resolve to", "this market resolves",
]

_PUNCT_RE = re.compile(r"[^\w\s\.\-\+°$%/:]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_MD_RE = re.compile(r"\*+|_+|`+")

#: Common aliases collapsed to a canonical entity token so "NYC" == "New York City".
ENTITY_ALIASES: dict[str, str] = {
    "nyc": "new york city",
    "new york": "new york city",
    "ny": "new york city",
    "la": "los angeles",
    "sf": "san francisco",
    "dc": "washington dc",
    "washington d.c.": "washington dc",
    "uk": "united kingdom",
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "ripple",
    "doge": "dogecoin",
    "fed": "federal reserve",
    "fomc": "federal reserve",
    "gop": "republican",
    "dems": "democrat",
    "democrats": "democrat",
    "republicans": "republican",
    "potus": "president",
    "s&p": "sp500",
    "s&p 500": "sp500",
    "spx": "sp500",
    "nasdaq 100": "nasdaq",
    "ndx": "nasdaq",
    "cpi": "consumer price index",
    "gdp": "gross domestic product",
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

#: Comparators, longest-first so ">=" wins over ">".
_COMPARATORS: list[tuple[str, str]] = [
    ("greater than or equal to", "gte"), ("at or above", "gte"), ("or above", "gte"),
    ("or higher", "gte"), ("or more", "gte"), ("at least", "gte"), (">=", "gte"),
    ("less than or equal to", "lte"), ("at or below", "lte"), ("or below", "lte"),
    ("or lower", "lte"), ("or less", "lte"), ("at most", "lte"), ("<=", "lte"),
    ("greater than", "gt"), ("higher than", "gt"), ("more than", "gt"),
    ("above", "gt"), ("exceed", "gt"), ("over", "gt"), (">", "gt"),
    ("less than", "lt"), ("lower than", "lt"), ("fewer than", "lt"),
    ("below", "lt"), ("under", "lt"), ("<", "lt"),
    ("between", "between"), ("equal to", "eq"), ("exactly", "eq"),
]

#: Measurement basis. Two markets on the same asset and threshold still differ if
#: one settles on a closing print and the other on an intraday touch.
_SEMANTIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bintraday\b|\btouch(es|ed)?\b|\bat any (time|point)\b|\breach(es|ed)?\b"),
     "touch_intraday"),
    (re.compile(r"\bclos(e|es|ing)\b|\bat the close\b|\bsettlement price\b|\bfinal price\b"),
     "close_at_cutoff"),
    (re.compile(r"\bhigh(est)?\b.*\btemp"), "daily_high"),
    (re.compile(r"\blow(est)?\b.*\btemp"), "daily_low"),
    (re.compile(r"\baverage\b|\bmean\b"), "average"),
    (re.compile(r"\bcumulative\b|\btotal\b"), "cumulative"),
]

_OVERTIME_RE = re.compile(
    r"\bovertime\b|\bextra time\b|\bpenalt(y|ies)\b|\bshootout\b|\bOT\b|\bincluding OT\b",
    re.IGNORECASE,
)
_REVISION_RE = re.compile(
    r"\brevis(ed|ion)s?\b|\bfinal(ized)? (estimate|reading)\b|\bsecond estimate\b",
    re.IGNORECASE,
)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_title(title: str, *, subtitle: str = "") -> str:
    """Collapse a market title to a comparable canonical form.

    Lowercases, strips markdown/punctuation, expands entity aliases, and removes
    interrogative boilerplate. The result is *not* meant to be human-facing - it is a
    matching key.
    """
    text = f"{title} {subtitle}".strip()
    text = _MD_RE.sub(" ", text)
    text = strip_accents(text).lower()
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()

    for phrase in _STOP_PHRASES:
        if text.startswith(phrase + " "):
            text = text[len(phrase) + 1 :]
    # Alias expansion is word-boundary anchored so "us" inside "used" survives.
    for alias, canonical in ENTITY_ALIASES.items():
        text = re.sub(rf"(?<![\w]){re.escape(alias)}(?![\w])", canonical, text)
    return _WS_RE.sub(" ", text).strip()


def tokenize(text: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", text.lower()) if len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_NUMBER_RE = re.compile(
    r"(?<![\w.])(\$)?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?"
    r"(k|m|bn|b|thousand|million|billion)?(?![\w])",
    re.IGNORECASE,
)

_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "bn": 1_000_000_000, "billion": 1_000_000_000,
}


def extract_numbers(text: str) -> list[Decimal]:
    """Pull numeric thresholds, handling $, commas and k/m/bn suffixes.

    A single-letter magnitude suffix is only honoured when the number is clearly a
    quantity - i.e. it carries a ``$`` prefix or the suffix is spelled out. Without
    that guard, the tournament name in "3M Open: Will Chandler Phillips finish top
    20?" parses as a $3,000,000 threshold, which then poisons the resolution hash and
    can make two unrelated markets look like a rule-compatible pair.
    """
    out: list[Decimal] = []
    for match in _NUMBER_RE.finditer(text):
        has_dollar = bool(match.group(1))
        raw = match.group(2).replace(",", "")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        suffix = (match.group(3) or "").lower()
        if suffix:
            is_spelled_out = len(suffix) > 2
            if has_dollar or is_spelled_out:
                value *= _MULTIPLIERS[suffix]
            else:
                # Ambiguous single-letter suffix with no currency marker ("3M Open",
                # "Game 7B"). Treat it as part of a name, not a magnitude.
                continue
        out.append(value)
    return out


def numbers_after_comparator(text: str) -> list[Decimal]:
    """Numbers appearing *after* a comparator phrase.

    A threshold is only meaningful relative to a comparator. Restricting extraction
    to the text following one removes leading identifiers (tournament names, event
    numbers, years in a prefix) that are not thresholds at all.
    """
    lowered = text.lower()
    best_index: int | None = None
    for phrase, _code in _COMPARATORS:
        idx = lowered.find(phrase)
        if idx >= 0 and (best_index is None or idx < best_index):
            best_index = idx + len(phrase)
    if best_index is None:
        return []
    return extract_numbers(text[best_index:])


def extract_comparator(text: str) -> str:
    lowered = text.lower()
    for phrase, code in _COMPARATORS:
        if phrase in lowered:
            return code
    return ""


def extract_threshold_semantics(text: str) -> str:
    lowered = text.lower()
    for pattern, label in _SEMANTIC_PATTERNS:
        if pattern.search(lowered):
            return label
    return ""


def extract_dates(text: str) -> list[date]:
    """Find calendar dates in the formats both venues use in titles."""
    found: list[date] = []
    lowered = text.lower()

    # "Jul 25, 2026" / "July 25 2026" / "25 July 2026"
    for m in re.finditer(
        r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?\b", lowered
    ):
        month = _MONTHS.get(m.group(1)[:4].rstrip(".")) or _MONTHS.get(m.group(1)[:3])
        if not month:
            continue
        year = int(m.group(3)) if m.group(3) else datetime.utcnow().year
        try:
            found.append(date(year, month, int(m.group(2))))
        except ValueError:
            continue

    for m in re.finditer(r"\b(\d{1,2})\s+([a-z]{3,9})\.?,?\s*(\d{4})?\b", lowered):
        month = _MONTHS.get(m.group(2)[:4].rstrip(".")) or _MONTHS.get(m.group(2)[:3])
        if not month:
            continue
        year = int(m.group(3)) if m.group(3) else datetime.utcnow().year
        try:
            found.append(date(year, month, int(m.group(1))))
        except ValueError:
            continue

    # ISO 2026-07-25
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", lowered):
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue

    return sorted(set(found))


#: Domain nouns kept as matching anchors even though they are short/common.
_ENTITY_HINTS = {
    "bitcoin", "ethereum", "solana", "ripple", "dogecoin", "sp500", "nasdaq",
    "federal", "reserve", "president", "republican", "democrat", "senate", "house",
    "consumer", "price", "index", "gross", "domestic", "product", "unemployment",
    "temperature", "temp", "hurricane", "snow", "rain", "election", "nominee",
}

_GENERIC_TOKENS = {
    "the", "and", "for", "will", "with", "from", "that", "this", "than", "when",
    "what", "who", "any", "all", "not", "yes", "no", "market", "resolve", "resolved",
    "before", "after", "during", "between", "above", "below", "over", "under",
    "more", "less", "least", "most", "high", "low", "close", "closing", "date",
}


def extract_entities(text: str) -> list[str]:
    """Content-bearing tokens used as a coarse matching anchor."""
    tokens = [t for t in re.split(r"[^\w]+", text.lower()) if t]
    out = [
        t for t in tokens
        if (t in _ENTITY_HINTS) or (len(t) > 3 and not t.isdigit() and t not in _GENERIC_TOKENS)
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


@dataclass
class TitleFeatures:
    """Everything structured that could be recovered from a title + rules blob."""

    normalized: str
    tokens: set[str] = field(default_factory=set)
    entities: list[str] = field(default_factory=list)
    numbers: list[Decimal] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)
    comparator: str = ""
    threshold_semantics: str = ""
    includes_overtime: bool | None = None
    uses_revised_data: bool | None = None

    @property
    def primary_threshold(self) -> Decimal | None:
        return self.numbers[0] if self.numbers else None


def extract_features(
    title: str, *, subtitle: str = "", description: str = ""
) -> TitleFeatures:
    """Build the structured feature set used for candidate matching.

    Thresholds are read from title+subtitle only. Descriptions restate thresholds in
    worked examples ("if the price were 100,000..."), which would pollute the set.
    Semantics and qualifiers, by contrast, are usually *only* in the description.
    """
    headline = f"{title} {subtitle}".strip()
    normalized = normalize_title(title, subtitle=subtitle)
    full = f"{headline} {description}".strip()

    overtime = True if _OVERTIME_RE.search(full) else None
    if overtime and re.search(r"\bnot includ\w*\s+(overtime|extra time)", full, re.IGNORECASE):
        overtime = False

    # Prefer numbers that follow a comparator; fall back to any number in the
    # headline only when the phrasing has no comparator at all.
    numbers = numbers_after_comparator(headline) or extract_numbers(headline)

    return TitleFeatures(
        normalized=normalized,
        tokens=tokenize(normalized),
        entities=extract_entities(normalized),
        numbers=numbers,
        dates=extract_dates(full),
        comparator=extract_comparator(headline) or extract_comparator(description),
        threshold_semantics=(
            extract_threshold_semantics(headline) or extract_threshold_semantics(description)
        ),
        includes_overtime=overtime,
        uses_revised_data=True if _REVISION_RE.search(full) else None,
    )


def parse_decimal_field(value: object) -> Decimal | None:
    """Provider fields arrive as strings, floats or None. Never trust the type."""
    return d_or_none(value)  # type: ignore[arg-type]
