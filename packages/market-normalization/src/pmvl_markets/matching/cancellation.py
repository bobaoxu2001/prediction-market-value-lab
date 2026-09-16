"""Reading cancellation terms out of rule text, and the line a reading may not cross.

Neither venue publishes void handling as a structured field, so the only source is
the prose. That prose is readable - "if the game is not played by March 3, this market
resolves to No" is unambiguous - and refusing to read it at all is why
``VERIFIED_EQUIVALENT_STRICT`` was previously unreachable for every cross-venue pair.

But a regex over prose is keyword matching, and keyword matching does not prove a
settlement term. A false positive here does not produce a bad recommendation; it
produces a *certified* one, sized with real money against a state space that was
pruned on the strength of a pattern.

So this module splits the job in two and puts a person in the middle:

:func:`read_cancellation` extracts a **proposal** - a classification plus the exact
sentence it came from - and marks it as requiring review. Nothing downstream may
treat a proposal as proof.

:class:`AttestationStore` holds readings a human has confirmed, each bound to the
market's ``resolution_hash``. Only an attestation yields ``LinkageEvidence.PROVEN``.
Because the hash covers the terms that decide payout, a venue editing its rules
invalidates the attestation automatically - the reading lapses rather than silently
describing text that no longer exists.

The practical consequence is that a freshly scanned pair is *not* tradeable, and
becomes so only after someone reads two rule pages. That is the intended cost.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pmvl_shared.enums import Platform
from pmvl_shared.timeutil import iso, utcnow

__all__ = [
    "VoidHandling",
    "CancellationReading",
    "RuleAttestation",
    "AttestationStore",
    "read_cancellation",
]


class VoidHandling(StrEnum):
    """What the rules say happens when the event does not resolve normally."""

    #: The rules define an outcome for every state, including non-occurrence. There
    #: is no rule-level void path.
    RESOLVES_ANYWAY = "resolves_anyway"
    #: The rules explicitly provide for voiding, cancelling or refunding.
    VOIDS = "voids"
    #: The rules explicitly provide for a 50-50 split.
    SPLITS = "splits"
    #: The prose says nothing either way.
    UNSTATED = "unstated"


#: Patterns that indicate the rules cover non-occurrence with a definite outcome.
#: Each requires both a non-occurrence clause *and* a named resolution, because
#: "if the game is postponed" on its own says nothing about who gets paid.
_RESOLVES_ANYWAY = re.compile(
    r"(?:if|should|unless|in the event)\b[^.]{0,160}?"
    r"(?:not (?:be )?(?:play|held|occur|take place|complete)\w*"
    r"|postpon\w+|delay\w+|does not occur|fails to occur|is cancell?ed)"
    r"[^.]{0,160}?"
    r"(?:resolv\w+|settl\w+)\s+(?:to\s+)?(?:\"?(?:yes|no)\"?)\b",
    re.I,
)

_VOIDS = re.compile(
    r"(?:market|contract)s?\s+(?:will|shall|may)\s+be\s+(?:void\w*|cancell?ed)"
    r"|(?:will|shall)\s+(?:be\s+)?(?:void\w*|refund\w*)"
    r"|resolv\w+\s+(?:as\s+)?(?:void|invalid)",
    re.I,
)

_SPLITS = re.compile(
    r"50[\s\-/]*50|fifty[\s\-]?fifty|0\.5\s+for\s+(?:each|both)"
    r"|resolv\w+\s+(?:to\s+)?50%\s*[/-]\s*50%",
    re.I,
)


@dataclass(frozen=True)
class CancellationReading:
    """A proposed reading of one market's cancellation terms. Never proof by itself."""

    handling: VoidHandling
    #: The sentence the classification came from, so a reviewer checks the source
    #: rather than the classifier.
    evidence_quote: str = ""
    #: Always True. Present as a field so that code handling a reading has to
    #: acknowledge it, rather than the caveat living only in this docstring.
    requires_review: bool = True

    @property
    def proposes_no_void(self) -> bool:
        return self.handling is VoidHandling.RESOLVES_ANYWAY


def read_cancellation(rules_text: str) -> CancellationReading:
    """Propose a classification of one market's cancellation terms.

    Order matters. An explicit void or split clause beats a resolves-anyway clause,
    because rules commonly say both - "resolves No if not played; void if the season
    is abandoned" - and the *permissive* reading is the one that must lose a tie.
    """
    if not rules_text:
        return CancellationReading(VoidHandling.UNSTATED)

    text = " ".join(rules_text.split())

    for pattern, handling in (
        (_SPLITS, VoidHandling.SPLITS),
        (_VOIDS, VoidHandling.VOIDS),
        (_RESOLVES_ANYWAY, VoidHandling.RESOLVES_ANYWAY),
    ):
        match = pattern.search(text)
        if match:
            return CancellationReading(handling, _sentence_around(text, match.start()))
    return CancellationReading(VoidHandling.UNSTATED)


def _sentence_around(text: str, index: int, *, width: int = 240) -> str:
    start = text.rfind(".", 0, index) + 1
    end = text.find(".", index)
    if end == -1:
        end = min(len(text), index + width)
    return text[start : end + 1].strip()[:width]


@dataclass(frozen=True)
class RuleAttestation:
    """A human's confirmed reading of one market's cancellation terms.

    ``resolution_hash`` binds the attestation to the exact rule terms it was made
    against. A venue that edits its rules changes the hash, and
    :meth:`AttestationStore.get` then returns nothing - the attestation lapses rather
    than continuing to describe text that no longer exists.
    """

    venue: Platform
    market_ref: str
    resolution_hash: str
    handling: VoidHandling
    reviewed_by: str
    reviewed_at: str
    evidence_quote: str = ""
    source_url: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.reviewed_by.strip():
            raise ValueError("an attestation must name who made it")
        if not self.resolution_hash.strip():
            raise ValueError("an attestation must name the rule terms it covers")

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["venue"] = self.venue.value
        data["handling"] = self.handling.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "RuleAttestation":
        return cls(
            venue=Platform(data["venue"]),
            market_ref=str(data["market_ref"]),
            resolution_hash=str(data["resolution_hash"]),
            handling=VoidHandling(data["handling"]),
            reviewed_by=str(data["reviewed_by"]),
            reviewed_at=str(data["reviewed_at"]),
            evidence_quote=str(data.get("evidence_quote", "")),
            source_url=str(data.get("source_url", "")),
            note=str(data.get("note", "")),
        )


class AttestationStore:
    """Reviewed rule readings, on disk, keyed by venue + market + rule hash."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._entries: dict[tuple[str, str, str], RuleAttestation] = {}
        if self._path and self._path.exists():
            self._load()

    def _key(self, venue: Platform, market_ref: str, resolution_hash: str):
        return (venue.value, market_ref, resolution_hash)

    def _load(self) -> None:
        assert self._path is not None
        raw = json.loads(self._path.read_text() or "[]")
        for item in raw:
            attestation = RuleAttestation.from_dict(item)
            self._entries[
                self._key(
                    attestation.venue,
                    attestation.market_ref,
                    attestation.resolution_hash,
                )
            ] = attestation

    def save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                [a.as_dict() for a in self._entries.values()], indent=2, sort_keys=True
            )
        )

    def record(self, attestation: RuleAttestation) -> None:
        self._entries[
            self._key(
                attestation.venue, attestation.market_ref, attestation.resolution_hash
            )
        ] = attestation

    def get(
        self, venue: Platform, market_ref: str, resolution_hash: str
    ) -> RuleAttestation | None:
        """The attestation covering exactly these rule terms, or ``None``.

        A market that has been attested under a *different* hash returns ``None``
        rather than the stale reading. That is the lapse, and it is the point.
        """
        return self._entries.get(self._key(venue, market_ref, resolution_hash))

    def lapsed_for(self, venue: Platform, market_ref: str) -> bool:
        """True when this market was attested once, but not for its current terms.

        Distinguishing "never reviewed" from "reviewed, then the venue edited the
        rules" is worth the extra call: the second is a queue of work, the first is
        a backlog.
        """
        return any(
            key[0] == venue.value and key[1] == market_ref for key in self._entries
        )

    def __len__(self) -> int:
        return len(self._entries)


def draft_attestation(
    *,
    venue: Platform,
    market_ref: str,
    resolution_hash: str,
    rules_text: str,
    source_url: str = "",
) -> dict[str, object]:
    """Build the reviewer's worksheet for one market. Deliberately not an attestation.

    Returns a plain dict with ``reviewed_by`` left empty, because filling it in is the
    act this module exists to require. Constructing :class:`RuleAttestation` from it
    without a reviewer raises.
    """
    reading = read_cancellation(rules_text)
    return {
        "venue": venue.value,
        "market_ref": market_ref,
        "resolution_hash": resolution_hash,
        "handling": reading.handling.value,
        "reviewed_by": "",
        "reviewed_at": iso(utcnow()),
        "evidence_quote": reading.evidence_quote,
        "source_url": source_url,
        "note": "proposed by pattern match; confirm against the published rules",
    }
