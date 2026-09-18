"""An append-only log that notices when it has been edited.

Forward evidence is only worth anything if nobody - including me, later, with a good
reason - can quietly remove the observations that went the wrong way. Each line
carries the digest of the line before it, so deleting or altering any earlier line
breaks the chain at that point and the reader says where.

This is not a security measure. Anyone who can write the file can rewrite the whole
chain. It is an *accident* measure: it makes "the losers are missing" a loud failure
instead of a silent one, which is the failure mode this project has already had once.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

__all__ = ["AppendOnlyLog", "LogIntegrityError", "read_log", "verify_log"]

GENESIS = "0" * 64


class LogIntegrityError(RuntimeError):
    """The chain does not add up, so the file is no longer a faithful record."""


def _digest(prev: str, body: str) -> str:
    return hashlib.sha256(f"{prev}\n{body}".encode()).hexdigest()


class AppendOnlyLog:
    """Writes JSON lines, each chained to the last.

    Opened in append mode and flushed per line: a recorder killed mid-run leaves a
    valid prefix rather than a truncated final line, and the next run continues the
    chain from whatever was already there.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev, self._count = self._tail()

    def _tail(self) -> tuple[str, int]:
        if not self.path.exists():
            return GENESIS, 0
        prev, count = GENESIS, 0
        with self.path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A half-written final line from a killed process. Everything
                    # before it stands; we chain from the last line that parsed.
                    break
                prev = row.get("digest") or prev
                count += 1
        return prev, count

    @property
    def count(self) -> int:
        return self._count

    def append(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = _digest(self._prev, body)
        row = f'{{"seq":{self._count},"prev":"{self._prev}","digest":"{digest}","body":{body}}}'
        with self.path.open("a") as fh:
            fh.write(row + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prev = digest
        self._count += 1
        return digest


@dataclass(frozen=True)
class LogEntry:
    seq: int
    digest: str
    body: dict[str, Any]


def read_log(path: Path, *, verify: bool = True) -> Iterator[LogEntry]:
    """Yield every entry in order, refusing to continue past a broken link.

    ``verify=False`` exists only for inspecting a file that is already known to be
    broken. Analysis code never passes it.
    """
    prev = GENESIS
    with Path(path).open() as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            body = json.dumps(row["body"], sort_keys=True, separators=(",", ":"))
            if verify:
                if row["prev"] != prev:
                    raise LogIntegrityError(
                        f"{path}:{lineno}: chain broken - entry {row['seq']} follows "
                        f"{row['prev'][:12]} but the previous line ended {prev[:12]}. "
                        "Lines have been removed or reordered."
                    )
                expected = _digest(prev, body)
                if row["digest"] != expected:
                    raise LogIntegrityError(
                        f"{path}:{lineno}: entry {row['seq']} body does not match its "
                        "digest. The line was edited after it was written."
                    )
            prev = row["digest"]
            yield LogEntry(seq=int(row["seq"]), digest=row["digest"], body=row["body"])


def verify_log(path: Path) -> tuple[bool, str]:
    """(ok, message). Used by the CLI so a run can state its evidence is intact."""
    try:
        n = sum(1 for _ in read_log(path))
    except LogIntegrityError as exc:
        return False, str(exc)
    except FileNotFoundError:
        return False, f"{path}: no such log"
    return True, f"{n} entries, chain intact"
