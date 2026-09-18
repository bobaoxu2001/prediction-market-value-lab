"""Load a recording back, with the version and the refusals attached.

Analysis always runs against the file, never against a live fetch. That is not a
performance choice: a result that can only be produced by re-querying the venue cannot
be re-checked later, and "it reproduced when I ran it again" is not reproduction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pmvl_shared.schemas import NormalizedMarket

from .schema import EventSnapshot, TradeRecord
from .writer import read_log

__all__ = ["Recording", "load_recording"]


@dataclass
class Recording:
    snapshots: list[EventSnapshot] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    #: ticker -> the market as it was when the recording started. Replay uses this
    #: rather than re-fetching, so a relisted or corrected rung cannot silently
    #: change what a past observation is taken to have meant.
    markets: dict[str, NormalizedMarket] = field(default_factory=dict)
    strategy_versions: set[str] = field(default_factory=set)

    @property
    def single_version(self) -> str | None:
        """The one version this recording was made under, or ``None`` if it spans several.

        Callers must refuse to pool a multi-version recording rather than picking one.
        A rule change between cycles means the later cycles answer a different
        question, and averaging the two answers produces a number that is not an
        estimate of either.
        """
        return next(iter(self.strategy_versions)) if len(self.strategy_versions) == 1 else None

    def as_dict(self) -> dict[str, Any]:
        synchronized = sum(1 for s in self.snapshots if s.is_synchronized)
        return {
            "strategy_versions": sorted(self.strategy_versions),
            "snapshots": len(self.snapshots),
            "synchronized_snapshots": synchronized,
            "degraded_snapshots": len(self.snapshots) - synchronized,
            "rejections": len(self.rejections),
            "trades": len(self.trades),
            "markets_described": len(self.markets),
            "events": sorted({s.event_ticker for s in self.snapshots}),
        }


def load_recording(directory: Path) -> Recording:
    """Read ``snapshots.jsonl`` and ``trades.jsonl``, verifying both chains."""
    directory = Path(directory)
    out = Recording()

    snapshots = directory / "snapshots.jsonl"
    if snapshots.exists():
        for entry in read_log(snapshots):
            body = entry.body
            out.strategy_versions.add(str(body.get("strategy_version")))
            if body.get("kind") == "snapshot":
                out.snapshots.append(EventSnapshot.from_dict(body))
            else:
                out.rejections.append(body)

    markets = directory / "markets.jsonl"
    if markets.exists():
        for entry in read_log(markets):
            body = entry.body
            out.strategy_versions.add(str(body.get("strategy_version")))
            if body.get("kind") == "market":
                market = NormalizedMarket.model_validate(body["market"])
                out.markets[market.platform_market_id] = market

    trades = directory / "trades.jsonl"
    if trades.exists():
        for entry in read_log(trades):
            body = entry.body
            out.strategy_versions.add(str(body.get("strategy_version")))
            if body.get("kind") == "trade":
                out.trades.append(TradeRecord.from_dict(body))

    return out
