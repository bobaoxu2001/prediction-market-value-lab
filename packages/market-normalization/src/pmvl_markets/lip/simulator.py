"""Replay one policy on one market-day, then score it second by second (3, 4, 5).

**Decisions** are taken at each recorded book's ``received_at`` using only what had been
received by then. An action goes through the account's write queue, is sent when a
token is free, and lands one latency later: a new order is fillable and creditable
from its landing; an order being cancelled stays fillable until its cancel lands.

**Write budget.** The pre-registration asks for one token bucket across all markets.
Replaying all markets in one event loop is too slow, so REG_LOW gives each market an
equal static share of the documented budget instead. A static share can never burst
into another market's idle budget, so it is strictly *more* restrictive than the
shared queue - the conservative side, which is LOW's side. REG_HIGH is unlimited.

**Credit** (``score_path``) is computed afterwards from the path's order ledger: for
each admitted second, every book bracketing it, the minimum (LOW) or maximum (HIGH)
over them, with the target test on others' books only.
"""

from __future__ import annotations

import heapq
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any

from .fills import (
    Fill,
    FillScenario,
    OrderState,
    allocate_print,
    allocate_quantity,
    print_relation,
)
from .model import BookSide, BookSnapshot, LipSide, MarketDay
from .policy import LipPolicy, Placement, allowed_sizes, gate_passes, targets
from .scoring import OwnOrder, accrual, share_high, share_low, side_ok
from .sim_params import SIM, SimParams

__all__ = ["REG_HIGH", "REG_LOW", "Regime", "SimPath", "WriteQueue", "score_path", "simulate", "segment_starts"]

OPEN_STATUSES = frozenset({"open", "active"})


@dataclass(frozen=True)
class Regime:
    name: str
    low: bool

    def latency(self, rtt: float, params: SimParams = SIM) -> float:
        return max(params.latency_low_floor_s, rtt) if self.low else params.latency_high_s


REG_LOW = Regime("REG_LOW", True)
REG_HIGH = Regime("REG_HIGH", False)


class WriteQueue:
    """A per-market token bucket: one token per action, no burst beyond one token.

    ``rate=None`` is unlimited (REG_HIGH). ``sync_delay`` models the shared account-wide
    queue at the instants every market acts at once - the start of a segment, a gate
    opening, a lane-wide dead-man re-entry: the market's first action after such an
    instant waits ``sync_delay`` seconds, its place in that queue (index / W).
    """

    def __init__(self, rate: float | None, sync_delay: float = 0.0) -> None:
        self.rate = rate
        self.sync_delay = sync_delay
        self.next_free = -math.inf

    def mark_sync(self, t: float) -> None:
        if self.rate is not None:
            self.next_free = max(self.next_free, t + self.sync_delay)

    def send_time(self, t: float) -> float:
        if self.rate is None:
            return t
        sent = max(t, self.next_free)
        self.next_free = sent + 1.0 / self.rate
        return sent


@dataclass
class SimPath:
    market: str
    policy: LipPolicy
    scenario: FillScenario
    regime: Regime
    target: float
    orders: list[OrderState] = field(default_factory=list)
    #: (t, queue) history per order id, for the LOW credited size
    queue_history: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    writes: list[tuple[float, float, float, str, bool]] = field(default_factory=list)
    #: (t, side, qty, proceeds, fee) - the end-of-day flatten through others' bids
    exits: list[tuple[float, LipSide, float, float, float]] = field(default_factory=list)
    participation: tuple[float, float] = (0.0, 0.0)
    gate_log: list[tuple[float, bool]] = field(default_factory=list)
    stops: list[tuple[float, str]] = field(default_factory=list)

    @property
    def fills(self) -> list[Fill]:
        return sorted((f for o in self.orders for f in o.fills), key=lambda f: f.at)


def segment_starts(lo: float, hi: float, anchor: float) -> list[float]:
    """Segment boundaries: every hour from the window start, clipped to [lo, hi)."""
    out = [lo]
    t = anchor
    while t < hi:
        if t > lo:
            out.append(t)
        t += 3600.0
    return sorted(set(out))


def _is_open(md: MarketDay, t: float, low: bool) -> bool:
    """Credit's open test. LOW needs BOTH bracketing polls, and unknown is not open."""
    before, after = md.status.bracketing(t)
    if low:
        if before is None or after is None:
            return False
        pts = [before, after]
    else:
        pts = [p for p in (before,) if p is not None]
        if not pts:
            return t < md.close_time
    for p in pts:
        if p.market_status.lower() not in OPEN_STATUSES:
            return False
        if p.exchange_active is False or (low and p.exchange_active is None):
            return False
    return t < md.close_time


def _known_open(md: MarketDay, t: float) -> bool:
    """A decision's open test: the latest poll at or before t only - never a later one."""
    before, _ = md.status.bracketing(t)
    if before is None:
        return False
    return (before.market_status.lower() in OPEN_STATUSES and before.exchange_active is not False
            and t < md.close_time)


def simulate(md: MarketDay, policy: LipPolicy, scenario: FillScenario, regime: Regime, *,
             writes_per_s: float | None = None, sync_delay_s: float = 0.0,
             window_anchor: float | None = None, window_end: float | None = None,
             params: SimParams = SIM) -> SimPath:
    """Event-driven replay of one policy on one admitted market-day."""
    target = float(md.program.target)
    size = policy.size(target)
    start = md.admitted_from
    end = min(md.admitted_until, md.close_time - params.close_buffer_s, md.program.end_ts,
              window_end if window_end is not None else math.inf)
    path = SimPath(md.market, policy, scenario, regime, target, participation=(start, max(start, end)))
    if end <= start:
        return path

    #: HEAVY widens every fillable window by the clock uncertainty; THROUGH and FIFO narrow it
    live_eps = md.clock_eps if scenario is FillScenario.HEAVY else -md.clock_eps
    queue = WriteQueue((writes_per_s or params.writes_per_s_low) if regime.low else None, sync_delay_s)
    horizon = end + 900.0
    books = [b for b in md.books.snapshots if start - 60 <= b.received_at <= horizon]
    trades = md.trades
    idx = trades.between(start + md.clock_offset - 1, horizon + md.clock_offset)
    events: list[tuple[float, int, int, Any]] = []
    for i, b in enumerate(books):
        events.append((b.received_at, 2, i, ("book", b)))
    for j in idx:
        events.append((float(trades.ts[j]) - md.clock_offset, 1, int(j), ("print", int(j))))
    for p in md.status.points:
        if start <= p.at <= end:
            events.append((p.at, 0, 0, ("status", p)))
    anchor = window_anchor if window_anchor is not None else start
    for s in segment_starts(start, end, anchor):
        events.append((s, 3, 0, ("segment", s)))
    events.append((end, 4, 0, ("end", end)))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    internal: list[tuple[float, int, int, Any]] = []  # landings
    known: list[tuple[float, int, LipSide, float]] = []  # fills becoming known
    seq = 0
    resting: dict[LipSide, list[OrderState]] = {LipSide.YES: [], LipSide.NO: []}
    pending_first_book: set[int] = set()
    held = {LipSide.YES: 0.0, LipSide.NO: 0.0}
    known_held = {LipSide.YES: 0.0, LipSide.NO: 0.0}
    last_book: BookSnapshot | None = None
    last_book_at = -math.inf
    at_level_volume: dict[tuple[LipSide, int], float] = {}
    filled_since: dict[LipSide, float] = {LipSide.YES: 0.0, LipSide.NO: 0.0}
    last_replenish: dict[LipSide, float] = {LipSide.YES: -math.inf, LipSide.NO: -math.inf}
    gate = {"state": "closed", "count": 0}
    stopped_until_segment = False
    ended = False
    oid = 0
    #: a queued placement per side that has not landed yet - coalesced, never duplicated
    pending_place: dict[LipSide, dict | None] = {LipSide.YES: None, LipSide.NO: None}
    pending_child: dict[LipSide, dict | None] = {LipSide.YES: None, LipSide.NO: None}
    decreasing: set[int] = set()

    def record_queue(o: OrderState, t: float) -> None:
        path.queue_history.setdefault(o.oid, []).append((t, o.queue))

    def schedule(t_decide: float, kind: str, payload: dict, rtt: float) -> float:
        nonlocal seq
        sent = queue.send_time(t_decide)
        landed = sent + regime.latency(rtt, params)
        payload["sent"] = sent
        path.writes.append((t_decide, sent, landed, kind, False))
        seq += 1
        heapq.heappush(internal, (landed, 0, seq, (kind, payload, t_decide)))
        return landed

    def cancel_side(t: float, side: LipSide, rtt: float) -> None:
        for o in list(resting[side]):
            o.credit_end_low = min(o.credit_end_low, t)
            schedule(t, "cancel", {"order": o}, rtt)
        resting[side] = []
        for pending in (pending_place, pending_child):
            item = pending[side]
            if item is None:
                continue
            if t < item["sent"]:
                item["void"] = True  # still queued: coalesced into nothing
                path.writes.append((t, t, t, "coalesce_void", True))
            else:
                item["cancel_on_land"] = t  # already on the wire: cancel it when it lands
            pending[side] = None

    def cancel_all(t: float, rtt: float, why: str) -> None:
        if resting[LipSide.YES] or resting[LipSide.NO] or pending_place[LipSide.YES] or pending_place[LipSide.NO]:
            path.stops.append((t, why))
        for side in (LipSide.YES, LipSide.NO):
            cancel_side(t, side, rtt)

    def own_ahead(side: LipSide, price: int, before_oid: int | None = None) -> float:
        """Our own resting size at a price, ahead of an order (all of it if None)."""
        return sum(o.remaining for o in resting[side]
                   if o.price == price and (before_oid is None or o.oid < before_oid))

    def place(t: float, side: LipSide, price: int, qty: float, book: BookSnapshot, parent: int | None,
              improved: bool) -> None:
        if qty <= 0:
            return
        slot = pending_child if parent is not None else pending_place
        item = slot[side]
        # everything ahead of the new order: others at the price, and our own orders there
        ahead = book.side(side).size_at(price) + own_ahead(side, price)
        if item is not None and t < item["sent"]:
            item.update(price=price, qty=qty, queue=ahead, improved=improved, decided=t)
            path.writes.append((t, t, t, "coalesce", True))
            return
        if item is not None:
            return  # sent and in flight: the next decision after it lands handles the rest
        payload = {"side": side, "price": price, "qty": qty, "queue": ahead,
                   "parent": parent, "improved": improved, "decided": t}
        slot[side] = payload
        schedule(t, "place", payload, book.rtt)

    def on_fill(o: OrderState, qty: float, t: float) -> None:
        if qty <= 0:
            return
        held[o.side] += qty
        # known to the maker only a latency later (3.5): inventory and replenishment read
        # the known position, never the true one
        heapq.heappush(known, (t + regime.latency(last_book.rtt if last_book else 0.0, params), o.oid, o.side, qty))

    def drain_known(t: float) -> None:
        while known and known[0][0] <= t:
            _, _, side, qty = heapq.heappop(known)
            known_held[side] += qty
            filled_since[side] += qty

    def live_orders(t: float) -> list[OrderState]:
        return sorted((o for o in path.orders if o.live(t, live_eps)), key=lambda o: o.oid)

    def land(t: float, item: tuple) -> None:
        nonlocal oid
        kind, payload, decided = item
        if kind == "place":
            side = payload["side"]
            for slot in (pending_place, pending_child):
                if slot[side] is payload:
                    slot[side] = None
            if payload.get("void"):
                return
            oid += 1
            o = OrderState(oid=oid, side=side, price=payload["price"], size=payload["qty"],
                           remaining=payload["qty"], queue=payload["queue"], decided_at=payload["decided"],
                           live_from=t, parent=payload["parent"], improved=payload["improved"])
            path.orders.append(o)
            record_queue(o, t)
            if scenario is not FillScenario.THROUGH:
                pending_first_book.add(o.oid)
            else:
                pending_first_book.add(-o.oid)  # THROUGH: still gets its first-book checks
            if "cancel_on_land" in payload:
                o.credit_end_low = min(o.credit_end_low, payload["cancel_on_land"])
                schedule(t, "cancel", {"order": o}, last_book.rtt if last_book else 0.0)
                return
            resting[side].append(o)
        elif kind == "cancel":
            o = payload["order"]
            o.fillable_until = min(o.fillable_until, t)
            o.credit_end_high = min(o.credit_end_high, t)
            if o in resting[o.side]:
                resting[o.side].remove(o)
        elif kind == "decrease":
            o = payload["order"]
            decreasing.discard(o.oid)
            cut = max(0.0, o.remaining - payload["to"])
            if cut > 0:
                o.remaining -= cut
                o.decreases.append((decided, t, cut))

    def drain_internal(upto: float) -> None:
        while internal and internal[0][0] <= upto:
            t, _, _, item = heapq.heappop(internal)
            land(t, item)

    def decide(book: BookSnapshot, t: float) -> None:
        if gate["state"] != "open" or stopped_until_segment or ended:
            return
        if not (md.program.start_ts <= t < md.program.end_ts) or t >= end:
            return
        drain_known(t)
        rest_px = {s: (resting[s][0].price if resting[s] else None) for s in (LipSide.YES, LipSide.NO)}
        tgt = targets(book, policy, rest_px, params)
        allowed = allowed_sizes(known_held[LipSide.YES] - known_held[LipSide.NO], size)
        for side in (LipSide.YES, LipSide.NO):
            want = tgt[side]
            if want is None:
                if resting[side] or pending_place[side]:
                    cancel_side(t, side, book.rtt)
                continue
            improved = policy.placement is Placement.IMPROVE and want > book.side(side).best
            on_book = resting[side]
            queued = pending_place[side]
            if not on_book and queued is not None:
                if queued["price"] != want:
                    place(t, side, want, allowed[side], book, None, improved)  # coalesces
                continue
            if not on_book or any(o.price != want for o in on_book):
                if on_book:
                    cancel_side(t, side, book.rtt)
                place(t, side, want, allowed[side], book, None, improved)
                filled_since[side] = 0.0
                continue
            total = sum(o.remaining for o in on_book)
            if total > allowed[side] + 1e-9:
                excess = total - allowed[side]
                for o in reversed(on_book):
                    cut = min(o.remaining, excess)
                    if cut > 0 and o.oid not in decreasing:
                        decreasing.add(o.oid)
                        schedule(t, "decrease", {"order": o, "to": o.remaining - cut}, book.rtt)
                        excess -= cut
                continue
            if filled_since[side] > 0 and t - last_replenish[side] >= params.replenish_every_s:
                room = allowed[side] - total
                qty = min(filled_since[side], room)
                if qty > 0 and pending_child[side] is None:
                    # the unfilled remainder waits for inventory room; it is not forgotten
                    place(t, side, want, qty, book, on_book[0].oid, improved)
                    last_replenish[side] = t
                    filled_since[side] -= qty
                    if filled_since[side] <= 1e-9:
                        filled_since[side] = 0.0

    def on_print(t: float, j: int) -> None:
        if bool(trades.block[j]):
            return
        yes_px, sz, taker_yes = int(trades.yes_px[j]), float(trades.size[j]), bool(trades.taker_yes[j])
        live = live_orders(t)
        for side in (LipSide.YES, LipSide.NO):
            through, at_level = [], []
            for o in live:
                if o.side is not side:
                    continue
                rel = print_relation(side, o.price, yes_px, taker_yes)
                if rel == "through":
                    through.append(o)
                elif rel == "at":
                    at_level.append(o)
            if not through and not at_level:
                continue
            left = sz
            # better-priced (through) orders first, oldest first within a price
            through.sort(key=lambda o: (-o.price, o.oid))
            for o, got in allocate_print(through, scenario, "through", left, t):
                left -= got
                on_fill(o, got, t)
            if at_level:
                at_level_volume[(side, at_level[0].price)] = at_level_volume.get((side, at_level[0].price), 0.0) + sz
                at_level.sort(key=lambda o: o.oid)
                before = [o.queue for o in at_level]
                for o, got in allocate_print(at_level, scenario, "at", left, t):
                    on_fill(o, got, t)
                for o, b in zip(at_level, before):
                    if o.queue != b:
                        record_queue(o, t)

    def on_book(book: BookSnapshot, t: float) -> None:
        prev = last_book
        levels: dict[tuple[LipSide, int], list[OrderState]] = {}
        for o in live_orders(t):
            levels.setdefault((o.side, o.price), []).append(o)
        for (side, price), orders in levels.items():
            side_now = book.side(side)
            level_now = side_now.size_at(price)
            prev_t = prev.received_at if prev is not None else t
            explained = at_level_volume.pop((side, price), 0.0)
            shrink = (prev.side(side).size_at(price) - level_now) if prev is not None else 0.0
            unexplained = shrink - explained
            covered = md.coverage.covered(prev_t, t)
            fresh = [o for o in orders if o.oid in pending_first_book or -o.oid in pending_first_book]
            stale = [o for o in orders if o not in fresh]
            before = {o.oid: o.queue for o in orders}
            if scenario is FillScenario.HEAVY and unexplained > 0 and stale:
                if covered:
                    for o in stale:  # cancels ahead of us
                        o.queue = max(0.0, o.queue - unexplained)
                else:
                    # shrinkage the tape cannot explain, read as prints through our queue
                    for o, got in allocate_print(stale, scenario, "at", unexplained, t, kind="gap"):
                        on_fill(o, got, t)
            for o in orders:
                cap = level_now + sum(x.remaining for x in orders if x.oid < o.oid)
                if o in fresh:
                    pending_first_book.discard(o.oid)
                    pending_first_book.discard(-o.oid)
                    if scenario is FillScenario.FIFO:
                        o.queue = max(o.queue, cap)
                    elif scenario is FillScenario.HEAVY:
                        o.queue = min(o.queue, cap)
                if scenario is not FillScenario.THROUGH:
                    o.queue = min(o.queue, cap)  # never more ahead of us than there is
                if o.queue != before[o.oid]:
                    record_queue(o, t)
            if scenario is FillScenario.HEAVY:
                opposite = book.side(side.other)
                crossing = float(opposite.sz[opposite.px >= 100 - price].sum()) if not opposite.empty else 0.0
                if crossing > 0:
                    when = max(prev_t, min(o.live_from for o in orders))
                    for o, got in allocate_quantity(orders, crossing, when, "cross"):
                        on_fill(o, got, when)
                offset = 1 if any(o.improved for o in orders) else 0
                best = side_now.best
                if best is None or best < price - offset:
                    for o in orders:
                        when = max(prev_t, o.live_from)
                        on_fill(o, o.take(o.remaining, when, "orphan"), when)
        at_level_volume.clear()

    stop_after = math.inf
    for t, _, _, (kind, obj) in events:
        if ended and t > stop_after:
            break
        drain_internal(t)
        if (not ended and last_book is not None and t - last_book_at > params.dead_man_s
                and (resting[LipSide.YES] or resting[LipSide.NO])):
            cancel_all(last_book_at + params.dead_man_s, last_book.rtt, "dead_man")
            gate["state"], gate["count"] = "evaluating", 0
            drain_internal(t)
        if kind == "print":
            on_print(t, obj)
        elif kind == "book":
            book: BookSnapshot = obj
            on_book(book, t)
            last_book, last_book_at = book, t
            if ended:
                continue
            if gate["state"] == "evaluating":
                ok = (gate_passes(book, target) and _known_open(md, t)
                      and md.program.start_ts <= t < md.program.end_ts and t < end)
                if not ok:
                    gate["state"] = "closed"
                    path.gate_log.append((t, False))
                    cancel_all(t, book.rtt, "gate")
                else:
                    gate["count"] += 1
                    if gate["count"] >= params.gate_books:
                        gate["state"] = "open"
                        path.gate_log.append((t, True))
                        queue.mark_sync(t)  # every market's gate opens together
            if gate["state"] == "open":
                decide(book, t)
        elif kind == "status" and not ended:
            p = obj
            if p.market_status.lower() not in OPEN_STATUSES or p.exchange_active is False:
                if last_book is not None:
                    cancel_all(t, last_book.rtt, "status")
                stopped_until_segment = True
        elif kind == "segment" and not ended:
            stopped_until_segment = False
            gate["state"], gate["count"] = "evaluating", 0
        elif kind == "end":
            rtt = last_book.rtt if last_book is not None else 0.0
            cancel_all(t, rtt, "end")
            ended = True
            # orders stay fillable until their cancels land (3.7): keep applying fills
            stop_after = max([x[0] for x in internal] + [t]) + max(0.0, live_eps)
    drain_internal(math.inf)
    t_exit = stop_after if math.isfinite(stop_after) else end
    _flatten(path, md, held, t_exit, regime, queue, last_book.rtt if last_book else 0.0, params)
    return path


def _flatten(path: SimPath, md: MarketDay, held: dict[LipSide, float], t: float, regime: Regime,
             queue: WriteQueue, rtt: float, params: SimParams) -> None:
    """Sell the residual |N| through others' bids on the held side (3.8)."""
    net = held[LipSide.YES] - held[LipSide.NO]
    if abs(net) < 1e-9:
        return
    side = LipSide.YES if net > 0 else LipSide.NO
    at = queue.send_time(t) + regime.latency(rtt, params)
    book = next((b for b in md.books.snapshots if b.received_at >= at), None)
    qty = abs(net)
    if book is not None:
        from .valuation import walk_bids

        proceeds, filled, fee = walk_bids(book.side(side), qty, md.fee_multiplier)
        path.exits.append((at, side, filled, proceeds, fee))
        if qty - filled > 1e-9:
            path.exits.append((at, side, qty - filled, float("nan"), 0.0))
    else:
        path.exits.append((at, side, qty, float("nan"), 0.0))


# ---------------------------------------------------------------------- credit
class _BookIndex:
    """Bisect lookups over one market's books: the bracket of a second in O(log n)."""

    def __init__(self, md: MarketDay) -> None:
        snaps = md.books.snapshots
        self.snaps = snaps
        self.recv = [b.received_at for b in snaps]
        self.by_sent = sorted(snaps, key=lambda b: b.sent_at)
        self.sent = [b.sent_at for b in self.by_sent]
        self.max_rtt = max((b.rtt for b in snaps), default=0.0)

    def bracket(self, k: float, params: SimParams) -> list[BookSnapshot] | None:
        i = bisect_right(self.recv, k) - 1
        j = bisect_left(self.sent, k + 1)
        if i < 0 or j >= len(self.by_sent):
            return None
        first, last = self.snaps[i], self.by_sent[j]
        if last.sent_at - first.received_at > params.span_limit_s:
            return None
        out = [first]
        # books whose socket window [sent, received] intersects [k, k+1)
        lo = bisect_left(self.recv, k)
        hi = bisect_right(self.recv, k + 1 + self.max_rtt)
        for b in self.snaps[lo:hi]:
            if b.sent_at < k + 1 and b is not first:
                out.append(b)
        if last is not first and all(last is not b for b in out):
            out.append(last)
        return out

    def before(self, k: float) -> BookSnapshot | None:
        i = bisect_right(self.recv, k) - 1
        return self.snaps[i] if i >= 0 else None


@dataclass
class _Ledger:
    """One order's fills and decreases as prefix sums, and its queue history.

    A decrease stops the cut size resting when it lands; LOW stops crediting it from
    the decision, as it does a cancel. ``done_at`` is when fills and decreases together
    reach the order's size - it earns nothing after that.
    """

    order: OrderState
    #: every reduction in resting size: (time, cumulative contracts), sorted
    cut_t: list[float]
    cut_cum: list[float]
    queue_t: list[float]
    queue_v: list[float]
    done_at: float = math.inf

    def remaining(self, t: float) -> float:
        i = bisect_left(self.cut_t, t)
        return max(0.0, self.order.size - (self.cut_cum[i - 1] if i > 0 else 0.0))

    def queue(self, t: float) -> float:
        i = bisect_right(self.queue_t, t) - 1
        return self.queue_v[max(i, 0)] if self.queue_v else self.order.queue


def score_path(path: SimPath, md: MarketDay, *, low: bool, params: SimParams = SIM) -> dict[str, Any]:
    """Per-second credit v_k over the admitted seconds, and the accrued reward."""
    from .programs import n_total_seconds

    target = path.target
    discount = float(md.program.discount)
    reward = float(md.program.reward)
    n_total = n_total_seconds(md.program)
    k0, k1 = math.ceil(md.admitted_from), math.floor(md.admitted_until)
    index = _BookIndex(md)
    ledgers = []
    for o in path.orders:
        cuts = [(f.at, f.qty) for f in o.fills]
        cuts += [((decided if low else landed), qty) for decided, landed, qty in o.decreases]
        cuts.sort()
        ct, cc, run, done = [], [], 0.0, math.inf
        for at, qty in cuts:
            run += qty
            ct.append(at)
            cc.append(run)
            if run >= o.size - 1e-9 and done == math.inf:
                done = at
        hist = path.queue_history.get(o.oid) or [(o.live_from, o.queue)]
        ledgers.append(_Ledger(o, ct, cc, [h[0] for h in hist], [h[1] for h in hist], done))
    ledgers.sort(key=lambda lg: lg.order.live_from)
    absorbed = sorted((f for f in path.fills if f.kind in ("at_level", "through", "gap", "orphan")), key=lambda f: f.at)
    absorbed_t = [f.at for f in absorbed]
    crosses = sorted((f for f in path.fills if f.kind == "cross"), key=lambda f: f.at)
    crosses_t = [f.at for f in crosses]
    level_cache: dict[int, tuple] = {}
    target_cache: dict[tuple, int | None] = {}
    v_sum, seconds, counted, observed, credited, closed_high = 0.0, 0, 0, 0, 0, 0
    last_v = 0.0
    active: list[_Ledger] = []
    nxt = 0

    def levels(side: BookSide) -> tuple:
        key = id(side)
        hit = level_cache.get(key)
        if hit is None:
            hit = level_cache[key] = tuple(zip((int(p) for p in side.px), (float(q) for q in side.sz)))
        return hit

    def counterfactual(book: BookSnapshot, side: LipSide, k: float) -> tuple:
        base_levels = levels(book.side(side))
        a0, a1 = bisect_left(absorbed_t, k - 60.0), bisect_left(absorbed_t, k)
        c0, c1 = bisect_left(crosses_t, k - 60.0), bisect_left(crosses_t, k)
        if a0 == a1 and c0 == c1:
            return base_levels
        base = dict(base_levels)
        for f in absorbed[a0:a1]:
            if f.side is side:
                base[f.price] = base.get(f.price, 0.0) + f.qty
        for f in crosses[c0:c1]:
            if f.side is side.other:
                left = f.qty
                for px in sorted((px for px in base if px >= 100 - f.price), reverse=True):
                    took = min(base[px], left)
                    base[px] -= took
                    left -= took
                    if left <= 0:
                        break
        return tuple(sorted(((px, q) for px, q in base.items() if q > 0), key=lambda x: -x[0]))

    def credited_price(before: BookSnapshot | None, side: LipSide, price: int) -> int:
        if before is None:
            return price
        key = (id(before), side, price)
        if key not in target_cache:
            target_cache[key] = targets(before, path.policy, {side: price}, params).get(side)
        tgt = target_cache[key]
        return price if tgt is None else min(price, tgt)

    for k in range(k0, k1):
        seconds += 1
        while nxt < len(ledgers) and ledgers[nxt].order.live_from < k + 1:
            active.append(ledgers[nxt])
            nxt += 1
        if active:
            active = [lg for lg in active if lg.order.credit_end_high > k and lg.done_at > k]
        bracket = index.bracket(k, params)
        if bracket is None:
            if not low:
                v_sum += last_v
            continue
        observed += 1
        if not _is_open(md, k, low):
            if not low:
                closed_high += 1
            last_v = 0.0 if low else last_v
            continue
        before = index.before(k)
        best_v = None
        for b in bracket:
            if not (side_ok(levels(b.yes), target) and side_ok(levels(b.no), target)):
                v_b = 0.0
            else:
                total_low, total_high, cf_ok = 0.0, 0.0, True
                for side in (LipSide.YES, LipSide.NO):
                    others = levels(b.side(side))
                    high_orders, low_orders = [], []
                    cf = counterfactual(b, side, k) if low else others
                    candidates = []
                    for lg in active:
                        o = lg.order
                        if o.side is not side:
                            continue
                        frac = min(k + 1, o.credit_end_high) - max(k, o.live_from)
                        if frac > 0:
                            q = lg.remaining(k + 0.5) * min(1.0, frac)
                            if q > 0:
                                high_orders.append(OwnOrder(o.price, q))
                        if low and o.live_from <= k and o.credit_end_low >= k + 1:
                            q = lg.remaining(k + 1)
                            if q > 0:
                                candidates.append((credited_price(before, side, o.price), o.oid, q, lg))
                    # LOW: each order gets only the room under T left after everything ahead
                    # of it - others at better prices, our own orders at better prices, and
                    # its queue at its own price (which already counts our earlier orders there)
                    candidates.sort(key=lambda c: (-c[0], c[1]))
                    for i, (price, _, q, lg) in enumerate(candidates):
                        better = sum(qq for px, qq in cf if px > price)
                        own_better = sum(c[2] for c in candidates[:i] if c[0] > price)
                        if path.scenario is FillScenario.THROUGH:
                            ahead = (next((qq for px, qq in cf if px == price), 0.0)
                                     + sum(c[2] for c in candidates[:i] if c[0] == price))
                        else:
                            ahead = lg.queue(k)
                        q_eff = min(max(target - better - own_better - ahead, 0.0), q)
                        if q_eff > 0:
                            low_orders.append(OwnOrder(price, q_eff))
                    h = share_high(others, tuple(high_orders), target, discount) if high_orders else 0.0
                    total_high += h
                    if low:
                        if not side_ok(cf, target):
                            cf_ok = False
                        lo_share = share_low(cf, tuple(low_orders), target, discount) if low_orders else 0.0
                        total_low += min(lo_share, h)
                v_b = (total_low if cf_ok else 0.0) if low else total_high
            best_v = v_b if best_v is None else (min(best_v, v_b) if low else max(best_v, v_b))
        v = best_v or 0.0
        if v > 0:
            credited += 1
        counted += 1
        last_v = v
        v_sum += v
    return {
        "v_sum": v_sum, "seconds": seconds, "observed": observed, "counted": counted, "credited": credited,
        "accrued": accrual(reward, v_sum, n_total - closed_high), "n_total": n_total - closed_high,
    }
