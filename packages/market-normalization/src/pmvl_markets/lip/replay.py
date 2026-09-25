"""One recorded day through every declared path, and many days into cells.

Per admitted market-day: 4 policies x 3 fill scenarios x 2 latency regimes = 24 paths,
each credited in both envelopes and valued both ways - 96 numbers per market-day, and
its admitted hours. Markets are independent, so they run in parallel processes.

The payout's verification (a program seen paid, terms unchanged) is *not* applied here:
it depends on listings read after the day. ``build_cells`` applies it when pooling.
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .fills import FillScenario
from .model import MarketDay
from .policy import POLICIES
from .reader import load_day
from .scoring import daily_payout
from .simulator import REG_HIGH, REG_LOW, score_path, simulate
from .study import Cell
from .valuation import day_exit, mark_1h
from .version import LIP_SIM_VERSION

__all__ = ["MarketDayResult", "build_cells", "replay_day", "replay_market"]

REGIMES = (REG_LOW, REG_HIGH)


@dataclass
class MarketDayResult:
    day: str
    market: str
    program_id: str
    family: str
    hours: float
    #: "policy|scenario|regime|envelope" -> {"accrued", "pay", "MARK_1H", "DAY_EXIT", "fills", ...}
    paths: dict[str, dict[str, float]] = field(default_factory=dict)
    sim_version: str = LIP_SIM_VERSION


def replay_market(md: MarketDay, day: str, program_id: str, *, writes_per_s: float,
                  window: tuple[float, float]) -> MarketDayResult:
    hours = max(0.0, md.admitted_until - md.admitted_from) / 3600
    out = MarketDayResult(day, md.market, program_id, md.family, hours)
    for policy in POLICIES:
        for scenario in FillScenario:
            for regime in REGIMES:
                path = simulate(md, policy, scenario, regime, writes_per_s=writes_per_s,
                                window_anchor=window[0], window_end=window[1])
                for env, low in (("LOW", True), ("HIGH", False)):
                    credit = score_path(path, md, low=low)
                    out.paths[f"{policy.name}|{scenario.value}|{regime.name}|{env}"] = {
                        "accrued": credit["accrued"],
                        "pay": daily_payout(credit["accrued"], None if md.program.cap is None else float(md.program.cap)),
                        "MARK_1H": mark_1h(path, md, low=low),
                        "DAY_EXIT": day_exit(path, md, low=low),
                        "fills": float(sum(f.qty for f in path.fills)),
                        "orders": float(len(path.orders)),
                        "seconds": credit["seconds"], "observed": credit["observed"],
                        "credited": credit["credited"],
                    }
    return out


def _one(args: tuple) -> dict[str, Any]:
    day_dir, settlements, state, program_id, writes, window = args
    data = load_day(Path(day_dir), settlements_path=settlements, state_path=state)
    pair = next(p for p in data.pairs if p.program_id == program_id)
    md = data.market_day(pair)
    return asdict(replay_market(md, data.study["day"], program_id, writes_per_s=writes, window=window))


def replay_day(day_dir: Path, *, settlements_path: Path | None = None, state_path: Path | None = None,
               workers: int = 4) -> list[dict[str, Any]]:
    """Every admitted market-day of one recorded day. The write budget is split evenly
    across the day's largest number of concurrently admitted markets (the conservative
    static share - see simulator)."""
    data = load_day(Path(day_dir), settlements_path=settlements_path, state_path=state_path)
    if not data.pairs:
        return []
    events = sorted([(p.admitted_at, 1) for p in data.pairs] + [(p.end, -1) for p in data.pairs])
    live, peak = 0, 1
    for _, d in events:
        live += d
        peak = max(peak, live)
    writes = 10.0 / peak
    jobs = [(str(day_dir), settlements_path, state_path, p.program_id, writes, data.window) for p in data.pairs]
    if workers <= 1:
        return [_one(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, jobs))


def build_cells(results: list[dict[str, Any]], *, verified: set[str] | None, withdrawn: set[str]) -> list[Cell]:
    """Pool market-day results into (study day, family) cells.

    LOW counts a payout only from a verified program (``verified`` None means the check
    has not been run, and LOW pays nothing); a withdrawn program pays nothing in either.
    Every admitted hour stays in the denominator.
    """
    cells: dict[tuple[str, str], Cell] = {}
    for r in results:
        c = cells.setdefault((r["day"], r["family"]), Cell(r["day"], r["family"]))
        c.hours += r["hours"]
        for key, v in r["paths"].items():
            policy, scenario, regime, env = key.split("|")
            pay = v["pay"]
            if r["program_id"] in withdrawn:
                pay = 0.0
            elif env == "LOW" and (verified is None or r["program_id"] not in verified):
                pay = 0.0
            for valuation in ("MARK_1H", "DAY_EXIT"):
                val = v[valuation]
                if not math.isfinite(val):
                    val = 0.0
                k = (policy, (scenario, regime, valuation), env)
                c.value[k] = c.value.get(k, 0.0) + pay + val
    return list(cells.values())


def write_results(results: list[dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps({"sim_version": LIP_SIM_VERSION, "results": results}) + "\n")
