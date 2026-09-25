"""How a LIP day is written to disk, and read back with its chain checked.

**Books.** Bids only, per side, truncated once cumulative size reaches twice the
program's target (LIP scoring never reads past the target; the second target is room
for the counterfactual book of section 5.3). The count and size of what was dropped
are recorded, so the truncation is visible rather than assumed. A market's levels are
written only when they change, with a keyframe every 60 s; every request still lists
the markets it covered, so "unchanged" is a recorded observation and never an
inference from silence.

**Segments.** One directory per UTC hour. Each stream's hash chain continues from the
previous segment's last digest, so removing a whole hour breaks the chain exactly as
removing a line does. Closed segments are gzipped. ``recording.writer`` is reused, not
changed: a subclass seeds a new file's chain from the previous segment.
"""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pmvl_shared.schemas import OrderBook

from ..recording.writer import GENESIS, AppendOnlyLog, LogIntegrityError, _digest
from .grid import to_cents
from .version import LIP_RECORDER_VERSION

__all__ = [
    "BookEncoder",
    "ChainedLog",
    "SegmentWriter",
    "encode_side",
    "read_stream",
    "segment_name",
]


def segment_name(at: datetime) -> str:
    return "segment-" + at.astimezone(timezone.utc).strftime("%Y%m%dT%H")


def encode_side(levels: list[Any], target: float, multiple: int = 2) -> tuple[list[list[float]], list[float]]:
    """Bids best-first as [[cents, size], ...], cut once cumulative size reaches multiple*T.

    The level that crosses the cut is kept whole. Returns (kept, [dropped_count,
    dropped_size]).
    """
    ordered = sorted(((to_cents(lv.price), float(lv.size)) for lv in levels if lv.size > 0),
                     key=lambda x: -x[0])
    kept: list[list[float]] = []
    cum = 0.0
    cut = multiple * target
    for i, (px, sz) in enumerate(ordered):
        if cum >= cut:
            rest = ordered[i:]
            return kept, [float(len(rest)), float(sum(q for _, q in rest))]
        kept.append([px, sz])
        cum += sz
    return kept, [0.0, 0.0]


@dataclass
class BookEncoder:
    """Turns one batched response into a change-only line with periodic keyframes."""

    keyframe_every_s: float = 60.0
    multiple: int = 2
    _last: dict[str, tuple] = field(default_factory=dict)
    _last_key: dict[str, float] = field(default_factory=dict)

    def encode(
        self,
        *,
        lane: str,
        sent_at: float,
        received_at: float,
        ok: bool,
        detail: str,
        requested: list[str],
        books: dict[str, OrderBook],
        targets: dict[str, float],
    ) -> dict[str, Any]:
        markets: dict[str, Any] = {}
        unchanged: list[str] = []
        for ticker in requested:
            book = books.get(ticker)
            if book is None:
                continue
            target = targets.get(ticker, 1000.0)
            yes, yes_drop = encode_side(book.yes_bids, target, self.multiple)
            no, no_drop = encode_side(book.no_bids, target, self.multiple)
            state = (json.dumps(yes), json.dumps(no))
            key_due = received_at - self._last_key.get(ticker, float("-inf")) >= self.keyframe_every_s
            if not key_due and self._last.get(ticker) == state:
                unchanged.append(ticker)
                continue
            markets[ticker] = {"yes": yes, "no": no, "dropped": {"yes": yes_drop, "no": no_drop},
                               "key": key_due}
            self._last[ticker] = state
            if key_due:
                self._last_key[ticker] = received_at
        return {
            "kind": "books", "v": LIP_RECORDER_VERSION, "lane": lane,
            "sent_at": sent_at, "received_at": received_at, "rtt": received_at - sent_at,
            "ok": ok, "detail": detail, "requested": list(requested),
            "missing": [t for t in requested if t not in books] if ok else list(requested),
            "markets": markets, "unchanged": unchanged,
        }


class ChainedLog(AppendOnlyLog):
    """An AppendOnlyLog whose first line in a new file chains from a given digest."""

    def __init__(self, path: Path, *, prev: str = GENESIS, count: int = 0) -> None:
        self._seed = (prev, count)
        super().__init__(path)

    def _tail(self) -> tuple[str, int]:
        if not self.path.exists():
            return self._seed
        prev, count = self._seed
        with self.path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    break
                prev = row.get("digest") or prev
                count = int(row.get("seq", count)) + 1
        return prev, count


@dataclass
class SegmentWriter:
    """Hourly segments under one day directory; one chained stream per file name."""

    day_dir: Path
    gzip_closed: bool = True
    _segment: str | None = None
    _logs: dict[str, ChainedLog] = field(default_factory=dict)
    _tails: dict[str, tuple[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.day_dir = Path(self.day_dir)
        self.day_dir.mkdir(parents=True, exist_ok=True)
        # resume: carry every stream's chain from the newest existing segment
        for seg in sorted(p for p in self.day_dir.glob("segment-*") if p.is_dir()):
            for f in seg.iterdir():
                stream = f.name.split(".")[0]
                last = None
                for entry in _raw_rows(f):
                    last = entry
                if last is not None:
                    self._tails[stream] = (last["digest"], int(last["seq"]) + 1)

    def append(self, stream: str, body: dict[str, Any], *, at: datetime | None = None) -> str:
        now = at or datetime.now(timezone.utc)
        seg = segment_name(now)
        if seg != self._segment:
            self._rotate(seg)
        log = self._logs.get(stream)
        if log is None:
            prev, count = self._tails.get(stream, (GENESIS, 0))
            log = ChainedLog(self.day_dir / seg / f"{stream}.jsonl", prev=prev, count=count)
            self._logs[stream] = log
        digest = log.append(body)
        self._tails[stream] = (digest, log.count)
        return digest

    def _rotate(self, new_segment: str) -> None:
        old = self._segment
        self._logs = {}
        self._segment = new_segment
        if old and self.gzip_closed:
            for f in (self.day_dir / old).glob("*.jsonl"):
                gzip_file(f)

    def close(self) -> None:
        if self._segment and self.gzip_closed:
            for f in (self.day_dir / self._segment).glob("*.jsonl"):
                gzip_file(f)
        self._logs = {}
        self._segment = None


def gzip_file(path: Path) -> Path:
    target = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return target


def _open(path: Path):
    return gzip.open(path, "rt") if path.suffix == ".gz" else path.open()


def _raw_rows(path: Path) -> Iterator[dict[str, Any]]:
    with _open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                return


def read_stream(day_dir: Path, stream: str, *, verify: bool = True) -> Iterator[dict[str, Any]]:
    """Every body of one stream across all segments, with the chain verified end to end."""
    prev = GENESIS
    for seg in sorted(p for p in Path(day_dir).glob("segment-*") if p.is_dir()):
        files = [seg / f"{stream}.jsonl", seg / f"{stream}.jsonl.gz"]
        for f in files:
            if not f.exists():
                continue
            for lineno, row in enumerate(_raw_rows(f), start=1):
                body = json.dumps(row["body"], sort_keys=True, separators=(",", ":"))
                if verify:
                    if row["prev"] != prev:
                        raise LogIntegrityError(
                            f"{f}:{lineno}: chain broken - lines or a whole segment are missing")
                    if row["digest"] != _digest(prev, body):
                        raise LogIntegrityError(f"{f}:{lineno}: body edited after it was written")
                prev = row["digest"]
                yield row["body"]
