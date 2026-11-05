"""No file git tracks may contain a private postal address.

An exact residential address was committed to `apps/web/lib/seller.ts` to resolve
the notices clauses on `/terms` and `/privacy`. This repository is public: the
string was served, indexed, and is now permanent in history. Removing it from the
working tree is the part that can still be done, and this test is what stops it
coming back through a different file - a doc, a fixture, an owner checklist - the
next time somebody needs to fill in an address field.

The check is deliberately structural. Matching the one string that caused the
incident would pass the moment a different residence, or the same one written
with "Blvd." instead of "Boulevard", was added. Matching the *shape* of a US
mailing address catches those, and the two-signal rule below keeps it from firing
on ordinary prose.

Scope is `git ls-files`: what a clone receives. Untracked working files are the
subject of `private/` and the ignore rules, not of this test.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: A house number followed by a street type. The leading number is required, so
#: "Washington" as a jurisdiction and "Park Avenue Partners" as a name do not
#: match - only something shaped like a place mail is delivered to.
#: This file's own resolved path; see the exemption in the scan below.
SELF = Path(__file__).resolve()

STREET_LINE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,4}\s+"
    r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct"
    r"|Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|Way|Circle|Cir)\.?\b"
)

#: A dwelling subdivision. On its own this is already a residential tell.
UNIT_LINE = re.compile(
    r"\b(?:Apt|Apartment|Unit|Suite|Ste|Flat)\.?\s*#?\s*\d+[A-Za-z]?\b"
)

#: `NJ 07310`. A bare state name is a jurisdiction and is required in the legal
#: documents; a state code sitting beside a ZIP is the last line of an address.
STATE_AND_ZIP = re.compile(
    r"\b(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]"
    r"|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s+\d{5}(?:-\d{4})?\b"
)

#: Suffixes that are text a human wrote or reviewed. Binary artefacts, lockfiles
#: and captured venue fixtures are excluded: a market title legitimately contains
#: street names, and a lockfile integrity hash is not prose anybody chose.
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", ".cjs",
    ".md", ".mdx", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".sql", ".sh", ".example",
}

#: Paths whose content is captured from a venue or generated, not authored.
EXCLUDED_PREFIXES = (
    "tests/fixtures/",
    "apps/extension/fixtures/",
    "docs/samples/",
)

EXCLUDED_NAMES = {"package-lock.json", "requirements.lock.txt"}


def tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    out: list[Path] = []
    for name in result.stdout.split("\0"):
        if not name:
            continue
        if name.startswith(EXCLUDED_PREFIXES) or Path(name).name in EXCLUDED_NAMES:
            continue
        path = ROOT / name
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        out.append(path)
    return out


def address_signals(text: str) -> list[str]:
    """Which address-shaped signals a document contains, by name.

    Reported by name rather than by matched text so a failure message can point
    at the file without reprinting the address it is complaining about.
    """
    found = []
    if UNIT_LINE.search(text):
        found.append("dwelling-unit")
    if STREET_LINE.search(text):
        found.append("street-line")
    if STATE_AND_ZIP.search(text):
        found.append("state-and-ZIP")
    return found


def test_exactly_one_file_is_exempt_from_the_scan() -> None:
    """The exemption is a single file, and it is this one.

    An exemption that can grow is not an exemption, it is a bypass.
    """
    exempt = [p for p in tracked_text_files() if p.resolve() == SELF]
    assert len(exempt) == 1
    assert exempt[0].name == "test_no_private_postal_address.py"


def test_the_detector_still_fires_on_a_real_looking_address() -> None:
    """Guard the exemption: the scanner must not have been declawed."""
    sample = "12 Example Ave\nApt 4B\nSomewhere, NJ 07030\n"
    assert len(address_signals(sample)) >= 2


def test_git_tracks_at_least_one_text_file() -> None:
    """Guard the guard. An empty file list would make every assertion vacuous."""
    assert len(tracked_text_files()) > 100


def test_no_tracked_file_contains_a_mailing_address() -> None:
    """Two independent signals in one file, which prose does not produce.

    One signal alone is common and innocent: a methodology page can say
    "Suite 300" in an example, and a fixture-free doc can name a highway. Two of
    "street line", "dwelling unit" and "state + ZIP" in the same document is a
    mailing address, and there is no legitimate reason for this repository to
    contain one.
    """
    offenders: list[str] = []
    for path in tracked_text_files():
        if path.resolve() == SELF:
            # A detector has to contain samples of what it detects. This file
            # carries an illustrative "Suite <n>" in a docstring and a
            # "<state> <ZIP>" in a comment explaining why a bare state name is
            # not enough - so it matches itself, and only itself, for reasons
            # that are the point rather than a leak.
            #
            # Exempted by exact resolved path, never by pattern, and pinned
            # below to exactly one file so this cannot quietly become a
            # directory-wide hole.
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        signals = address_signals(text)
        if len(signals) >= 2:
            offenders.append(f"{path.relative_to(ROOT)} ({', '.join(signals)})")

    assert not offenders, (
        "these tracked files look like they contain a postal address: "
        + "; ".join(offenders)
        + ". This repository is public. If an address is genuinely required, it "
        "must be a business or registered-agent address, never a residence."
    )


@pytest.mark.parametrize(
    "relative",
    ["apps/web/lib/seller.ts", "apps/web/app/(site)/terms/page.tsx",
     "apps/web/app/(site)/privacy/page.tsx"],
)
def test_the_documents_that_carried_it_carry_no_signal_at_all(relative: str) -> None:
    """The three files the incident touched are held to the stricter bar.

    Elsewhere two signals are needed to avoid false positives on prose. Here even
    one is wrong: these files exist to state who the seller is, and that is
    exactly the context in which a single street line is an address.
    """
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert address_signals(text) == [], (
        f"{relative} contains address-shaped text; this is where the private "
        "residential address was published from"
    )
