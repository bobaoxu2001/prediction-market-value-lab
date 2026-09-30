"""Every recording and admission constant of the LIP study, in one frozen place.

Each value is fixed by docs/lip-preregistration.md (sections 2 and 8). A change after
day 1 is a new recorder version, not an edit here. Simulation constants live in
``sim_params`` and are pinned separately, because a scoring fix is not a change to
what was recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from decimal import Decimal

__all__ = ["FAST_INFO_PATTERNS", "RECORDING", "RecordingParams"]


@dataclass(frozen=True)
class RecordingParams:
    # --- schedule (UTC) ---------------------------------------------------------
    #: The recorder refuses to run inside [refuse_from, refuse_until): that is the maker
    #: study's window, and two recorders would share one read budget.
    refuse_from: time = time(2, 0)
    refuse_until: time = time(12, 25)
    #: Admission opens here; the quoting window proper starts at window_start.
    admission_from: time = time(12, 25)
    window_start: time = time(12, 30)
    #: The quoting window ends here on the following calendar day; no admission after.
    window_end: time = time(0, 45)
    #: Recording continues for the one-hour marks of the last fills, then drains.
    drain_at: time = time(1, 50)
    hard_stop: time = time(2, 0)
    #: Waiting for the maker study to finish stops here and the day is logged skipped.
    give_up_waiting: time = time(13, 30)
    maker_lock: str = "/tmp/pmvl-maker-day.lock.d"
    maker_process: str = "pmvl record sessions"
    own_lock: str = "/tmp/pmvl-lip-day.lock.d"

    # --- admission (section 2) --------------------------------------------------
    min_program_seconds: int = 21_600
    min_rate_per_hour: Decimal = Decimal("2.00")
    min_seconds_to_close: int = 3_600
    family_cap: int = 5
    total_cap: int = 200
    programs_active_every_s: float = 300.0
    programs_upcoming_every_s: float = 900.0

    # --- lanes (section 8) ------------------------------------------------------
    #: "global" reads the whole trade feed; "per_ticker" (TAPE_PT, the pre-flight fallback)
    #: reads each admitted market's tape every tape_per_ticker_every_s, with one book lane
    #: and a total cap of 100
    tape_mode: str = "global"
    tape_per_ticker_every_s: float = 60.0
    #: False when the pre-flight found min_ts ignored: pages are then read until one
    #: reaches back past the watermark
    min_ts_honoured: bool = True
    book_lanes: int = 2
    book_cadence_s: float = 1.0
    book_jitter_s: float = 0.25
    book_lane_rate: float = 1.25
    book_lane_size: int = 100
    #: levels per side requested; a linear-cent side has at most 99, so this is all of it
    book_depth: int = 100
    #: keep levels until cumulative size reaches this multiple of the target
    truncate_multiple: int = 2
    keyframe_every_s: float = 60.0
    tape_every_s: float = 2.0
    tape_rate: float = 1.5
    tape_limit: int = 200
    tape_overlap_s: float = 5.0
    tape_page_cap: int = 50
    status_every_s: float = 30.0
    status_rate: float = 0.3
    control_rate: float = 0.3
    audit_every_s: float = 300.0
    audit_tickers: int = 2
    clock_every_s: float = 600.0
    #: record each admitted market this long past its participation end, for 1 h marks
    mark_tail_s: float = 3_900.0
    #: 429 policy: halve a lane's rate for this long after a 429
    backoff_s: float = 600.0
    #: a counted day needs successful book responses on this share of the window's ticks
    counted_day_share: float = 0.80
    #: how often the recorder writes every stream's end to ends.json (Amendment 3)
    checkpoint_every_s: float = 60.0
    window_ticks: int = 44_100


RECORDING = RecordingParams()

#: Fast-information families, excluded from the universe outright (section 2.1, X1-X4).
#: Each entry: (class, pattern, exception). ``exception`` is a substring that exempts a
#: ticker from that one pattern - only the game/match rule has one, for season markets.
_INPLAY = (
    r"^KX(TT[A-Z]*MATCH|MIXEDDOUBLESMATCH|SQUASHMATCH|DARTSMATCH|CHESSMATCH|CHESSGAME|"
    r"WRESTLINGMATCH|CRICKET[A-Z0-9]*MATCH|VALORANTGAME|LOLGAMES|CBAGAME|NCAA[A-Z]*GAME|"
    r"MLBFTGAME|CLUBFGAME|EFLCUPGAME|BALLERLEAGUEGAME|PICKLEBALLGAMES|PPLGAMES)$"
)
FAST_INFO_PATTERNS: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    ("X1", re.compile(r"15M$"), None),
    ("X1", re.compile(r"^KXTEMP[A-Z]*H$"), None),
    ("X2", re.compile(_INPLAY), None),
    ("X2", re.compile(r"MENTION"), None),
    ("X2", re.compile(r"SAY$"), None),
    ("X2", re.compile(r"^KXTRUTHSOCIAL"), None),
    ("X2", re.compile(r"(GAME|GAMES|MATCH|SPREAD|TOTAL)$"), "SEASON"),
    ("X3", re.compile(r"^KX(HIGH|LOW|TEMP|RAIN|SNOW|WIND)"), None),
    ("X3", re.compile(r"ERCOT"), None),
    ("X3", re.compile(r"PJM"), None),
    ("X4", re.compile(
        r"^KX(BTC|ETH|SOL|XRP|DOGE|CRYPTO|EURUSD|USDJPY|GBPUSD|INX|NASDAQ|WTI|BRENT|GOLD|"
        r"SILVER|NATGAS|COPPER|PLAT|PALL)"), None),
)
