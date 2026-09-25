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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .fills import FillScenario
from .model import MarketDay
from .policy import POLICIES
from .reader import load_day
from .scoring import daily_payout
from .sim_params import SIM, SimParams
from .simulator import REG_HIGH, REG_LOW, score_path, simulate
from .study import Cell
from .universe import AdmissionState
from .valuation import day_exit, mark_1h
from .version import LIP_SIM_VERSION

WRITES_PER_S = SIM.writes_per_s_low

__all__ = ["MarketDayResult", "build_cells", "replay_day", "replay_market"]

REGIMES = (REG_LOW, REG_HIGH)


@dataclass
class MarketDayResult:
    day: str
    market: str
    program_id: str
    family: str
    hours: float
    #: the program's per-account cap, applied across days when results are pooled
    cap: float | None = None
    #: "policy|scenario|regime|envelope" -> {"accrued", "pay", "MARK_1H", "DAY_EXIT", "fills", ...}
    paths: dict[str, dict[str, float]] = field(default_factory=dict)
    sim_version: str = LIP_SIM_VERSION


def replay_market(md: MarketDay, day: str, program_id: str, *, writes_per_s: float,
                  window: tuple[float, float], sync_delay_s: float = 0.0,
                  params: SimParams = SIM) -> MarketDayResult:
    hours = max(0.0, md.admitted_until - md.admitted_from) / 3600
    out = MarketDayResult(day, md.market, program_id, md.family, hours,
                          cap=None if md.program.cap is None else float(md.program.cap))
    for policy in POLICIES:
        for scenario in FillScenario:
            for regime in REGIMES:
                path = simulate(md, policy, scenario, regime, writes_per_s=writes_per_s,
                                sync_delay_s=sync_delay_s, window_anchor=window[0], window_end=window[1],
                                params=params)
                for env, low in (("LOW", True), ("HIGH", False)):
                    credit = score_path(path, md, low=low, params=params)
                    out.paths[f"{policy.name}|{scenario.value}|{regime.name}|{env}"] = {
                        "accrued": credit["accrued"],
                        # uncapped here: the cap spans days and is applied when pooling
                        "pay": daily_payout(credit["accrued"], None),
                        "MARK_1H": mark_1h(path, md, low=low, params=params),
                        "DAY_EXIT": day_exit(path, md, low=low, params=params),
                        "fills": float(sum(f.qty for f in path.fills)),
                        "orders": float(len(path.orders)),
                        "seconds": credit["seconds"], "observed": credit["observed"],
                        "credited": credit["credited"],
                    }
    return out


def _params_for(study: dict[str, Any]) -> SimParams:
    """The span limit follows the cadence the day was actually recorded at (2 s fallback)."""
    cadence = float(study.get("book_cadence_s") or SIM.cadence_s)
    return replace(SIM, cadence_s=cadence)


def _one(args: tuple) -> dict[str, Any]:
    day_dir, settlements, state, program_id, writes, window, sync = args
    data = load_day(Path(day_dir), settlements_path=settlements, state_path=state)
    pair = next(p for p in data.pairs if p.program_id == program_id)
    md = data.market_day(pair)
    return asdict(replay_market(md, data.study["day"], program_id, writes_per_s=writes, window=window,
                                sync_delay_s=sync, params=_params_for(data.study)))


def skipped_day_results(study: dict[str, Any], state_path: Path | None) -> list[dict[str, Any]]:
    """A day the recorder did not record: every pair live in its window adds its hours at $0."""
    if not state_path or not Path(state_path).exists():
        return []
    lo = datetime.fromisoformat(study["window"]["window_start"]).timestamp()
    hi = datetime.fromisoformat(study["window"]["window_end"]).timestamp()
    state = AdmissionState.from_json(json.loads(Path(state_path).read_text()))
    out = []
    for pair in state.admitted.values():
        a, b = max(pair.start, pair.admitted_at, lo), min(pair.end, hi)
        if b <= a:
            continue
        r = MarketDayResult(study["day"], pair.market, pair.program_id, pair.family, (b - a) / 3600,
                            cap=None if pair.cap in (None, "") else float(pair.cap))
        zero = {"accrued": 0.0, "pay": 0.0, "MARK_1H": 0.0, "DAY_EXIT": 0.0, "fills": 0.0, "orders": 0.0,
                "seconds": 0, "observed": 0, "credited": 0}
        for policy in POLICIES:
            for scenario in FillScenario:
                for regime in REGIMES:
                    for env in ("LOW", "HIGH"):
                        r.paths[f"{policy.name}|{scenario.value}|{regime.name}|{env}"] = dict(zero)
        out.append(asdict(r))
    return out


def replay_day(day_dir: Path, *, settlements_path: Path | None = None, state_path: Path | None = None,
               workers: int = 4) -> list[dict[str, Any]]:
    """Every admitted market-day of one day - a skipped day contributes zeros.

    REG_LOW splits the documented 10 writes/s evenly across the day's peak number of
    admitted markets, and at the instants every market acts together (a segment start,
    a gate opening) each market waits its place in the shared queue: index / 10 s.
    """
    study = json.loads((Path(day_dir) / "study.json").read_text())
    if study.get("skipped"):
        return skipped_day_results(study, state_path)
    data = load_day(Path(day_dir), settlements_path=settlements_path, state_path=state_path)
    if not data.pairs:
        return []
    events = sorted([(p.admitted_at, 1) for p in data.pairs] + [(p.end, -1) for p in data.pairs])
    live, peak = 0, 1
    for _, d in events:
        live += d
        peak = max(peak, live)
    writes = WRITES_PER_S / peak
    ordered = sorted(data.pairs, key=lambda p: (p.admitted_at, p.program_id))
    jobs = [(str(day_dir), settlements_path, state_path, p.program_id, writes, data.window, i / WRITES_PER_S)
            for i, p in enumerate(ordered)]
    if workers <= 1:
        return [_one(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, jobs))


def build_cells(results: list[dict[str, Any]], *, verified: set[str] | None, withdrawn: set[str]) -> list[Cell]:
    """Pool market-day results into (study day, family) cells.

    LOW counts a payout only from a verified program (``verified`` None means the check
    has not been run, and LOW pays nothing); a withdrawn program pays nothing in either.
    A program's per-account cap spans its days: each day pays at most the cap minus what
    its earlier days paid (5.8 step 2). Every admitted hour stays in the denominator.
    """
    capped: dict[tuple[str, str], float] = {}
    for r in sorted(results, key=lambda r: r["day"]):
        for key, v in r["paths"].items():
            paid = capped.get((r["program_id"], key), 0.0)
            cap_left = None if r.get("cap") is None else float(r["cap"]) - paid
            v["pay"] = daily_payout(v["accrued"], cap_left)
            capped[(r["program_id"], key)] = paid + v["pay"]
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
