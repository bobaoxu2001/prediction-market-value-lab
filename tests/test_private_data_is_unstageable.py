"""Customer and prospect research must not be one `git add -A` from public.

`founding-trader/` held a prospect ledger - names, contact details, outreach
history - at the repository root. It was untracked, which is not a protection:
untracked is the state immediately before tracked, and an untracked directory
shows up in every `git status` as a `??` entry that `git add -A` will stage
without comment. This repository is public, and a commit containing personal
data cannot be undone by deleting the file.

The folder now lives under `private/`, which has been the protected area since
the pilot ledger needed one. These tests assert the protection holds rather than
that a particular folder was moved, so the next place somebody puts a customer
file is covered too.

`git check-ignore` is the oracle on purpose. Reading `.gitignore` and
re-implementing its precedence rules would test our understanding of gitignore,
which is exactly the thing that was wrong.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def is_ignored(relative: str) -> bool:
    """Whether git would ignore this path, asked of git itself."""
    return (
        subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", relative],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


def tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return {line for line in result.stdout.splitlines() if line}


@pytest.mark.parametrize(
    "relative",
    [
        "private/anything.csv",
        "private/founding-trader/customer_ledger.csv",
        "private/founding-trader/research_archive.md",
        "private/nested/deeper/notes.md",
    ],
)
def test_the_private_area_is_ignored(relative: str) -> None:
    assert is_ignored(relative), f"{relative} would be stageable"


@pytest.mark.parametrize(
    "relative",
    [
        # The exact path the folder used to sit at.
        "founding-trader/customer_ledger.csv",
        "founding-trader/batch1_outreach_log.md",
        # And the shapes, wherever they are recreated. A tool that writes a
        # ledger back to the old location, or to a new one, stays ignored.
        "customer_ledger.csv",
        "docs/customer_ledger.csv",
        "notes/batch2_outreach_log.md",
    ],
)
def test_prospect_research_stays_ignored_wherever_it_reappears(relative: str) -> None:
    assert is_ignored(relative), (
        f"{relative} is not ignored; a single `git add -A` would publish it"
    )


def test_no_customer_or_prospect_file_is_currently_tracked() -> None:
    """The state assertion, independent of the ignore rules.

    An ignore rule does nothing for a file that is *already* tracked - git
    ignores nothing it is already following - so the rules above and this check
    are answering different questions.
    """
    suspicious = sorted(
        path
        for path in tracked_paths()
        if path.startswith(("private/", "founding-trader/"))
        or Path(path).name in {"customer_ledger.csv", "founding-trader-ledger.csv"}
        or "outreach_log" in Path(path).name
    )
    assert suspicious == [], (
        "these customer/prospect files are tracked and therefore public: "
        f"{suspicious}. Removing them from the working tree does not remove them "
        "from history."
    )


def test_the_committed_template_is_not_mistaken_for_the_real_ledger() -> None:
    """The template is meant to be public; the ledger it describes is not.

    Kept as an explicit assertion so a future rule that ignores everything
    matching `*customers*` does not silently drop the template a reader needs.
    """
    template = "docs/founding-pilot-customers.template.md"
    assert template in tracked_paths()
    assert not is_ignored(template)
