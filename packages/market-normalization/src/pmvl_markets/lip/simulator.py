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
    apply_cross,
    apply_orphan,
    apply_print,
    apply_shrink,
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
    """A token bucket; each action costs one token. ``rate=None`` is unlimited."""

    def __init__(self, rate: float | None) -> None:
        self.rate = rate
        self.capacity = max(1.0, rate) if rate else math.inf
        self.tokens = self.capacity
        self.at = -math.inf
        self.next_free = -math.inf

    def send_time(self, t: float) -> float:
        if self.rate is None:
            return t
        start = max(t, self.next_free)
        if self.at == -math.inf:
            self.tokens = self.capacity
        else:
            self.tokens = min(self.capacity, self.tokens + (start - self.at) * self.rate)
        self.at = start
        if self.tokens >= 1:
            self.tokens -= 1
            return start
        wait = (1 - self.tokens) / self.rate
        self.at = start + wait
        self.tokens = 0.0
        self.next_free = start + wait
        return start + wait


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


def _levels(side: BookSide) -> tuple[tuple[int, float], ...]:
    return tuple(zip((int(p) for p in side.px), (float(q) for q in side.sz)))


def _is_open(md: MarketDay, t: float, low: bool) -> bool:
    before, after = md.status.bracketing(t)
    pts = [p for p in (before, after) if p is not None] if low else [p for p in (before,) if p is not None]
    if not pts:
        return not low
    for p in pts:
        if p.market_status.lower() not in OPEN_STATUSES:
            return False
        if p.exchange_active is False or (low and p.exchange_active is None):
            return False
    return t < md.close_time


def simulate(md: MarketDay, policy: LipPolicy, scenario: FillScenario, regime: Regime, *,
             writes_per_s: float | None = None, window_anchor: float | None = None,
             window_end: float | None = None, params: SimParams = SIM) -> SimPath:
    """Event-driven replay of one policy on one admitted market-day."""
    target = float(md.program.target)
    size = policy.size(target)
    start = md.admitted_from
    end = min(md.admitted_until, md.close_time - params.close_buffer_s, md.program.end_ts,
              window_end if window_end is not None else math.inf)
    path = SimPath(md.market, policy, scenario, regime, target, participation=(start, max(start, end)))
    if end <= start:
        return path

    queue = WriteQueue((writes_per_s or params.writes_per_s_low) if regime.low else None)
    books = [b for b in md.books.snapshots if b.received_at >= start - 60 and b.received_at <= end + 120]
    trades = md.trades
    idx = trades.between(start, end + 120)
    events: list[tuple[float, int, int, Any]] = []
    for i, b in enumerate(books):
        events.append((b.received_at, 2, i, ("book", b)))
    for j in idx:
        events.append((float(trades.ts[j]), 1, int(j), ("print", int(j))))
    for p in md.status.points:
        if start <= p.at <= end:
            events.append((p.at, 0, 0, ("status", p)))
    anchor = window_anchor if window_anchor is not None else start
    for s in segment_starts(start, end, anchor):
        events.append((s, 3, 0, ("segment", s)))
    events.append((end, 4, 0, ("end", end)))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    internal: list[tuple[float, int, int, Any]] = []  # landings
    seq = 0
    resting: dict[LipSide, list[OrderState]] = {LipSide.YES: [], LipSide.NO: []}
    pending_first_book: set[int] = set()
    held = {LipSide.YES: 0.0, LipSide.NO: 0.0}
    last_book: BookSnapshot | None = None
    last_book_at = -math.inf
    at_level_volume: dict[tuple[LipSide, int], float] = {}
    filled_since: dict[LipSide, float] = {LipSide.YES: 0.0, LipSide.NO: 0.0}
    last_fill_known: dict[LipSide, float] = {LipSide.YES: math.inf, LipSide.NO: math.inf}
    last_replenish: dict[LipSide, float] = {LipSide.YES: -math.inf, LipSide.NO: -math.inf}
    gate = {"state": "closed", "count": 0}
    stopped_until_segment = False
    oid = 0

    def record_queue(o: OrderState, t: float) -> None:
        path.queue_history.setdefault(o.oid, []).append((t, o.queue))

    #: a queued placement per side that has not landed yet - coalesced, never duplicated
    pending_place: dict[LipSide, dict | None] = {LipSide.YES: None, LipSide.NO: None}
    pending_child: dict[LipSide, dict | None] = {LipSide.YES: None, LipSide.NO: None}
    decreasing: set[int] = set()

    def schedule(t_decide: float, kind: str, payload: dict, rtt: float) -> None:
        nonlocal seq
        sent = queue.send_time(t_decide)
        landed = sent + regime.latency(rtt, params)
        payload["sent"] = sent
        path.writes.append((t_decide, sent, landed, kind, False))
        seq += 1
        heapq.heappush(internal, (landed, 0, seq, (kind, payload, t_decide)))

    def cancel_side(t: float, side: LipSide, rtt: float, why: str) -> None:
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
        if resting[LipSide.YES] or resting[LipSide.NO]:
            path.stops.append((t, why))
        for side in (LipSide.YES, LipSide.NO):
            cancel_side(t, side, rtt, why)

    def place(t: float, side: LipSide, price: int, qty: float, book: BookSnapshot, parent: int | None,
              improved: bool) -> None:
        if qty <= 0:
            return
        slot = pending_child if parent is not None else pending_place
        item = slot[side]
        if item is not None and t < item["sent"]:
            # coalesce: the queued action takes the new desired state in place
            item.update(price=price, qty=qty, queue=book.side(side).size_at(price), improved=improved, decided=t)
            path.writes.append((t, t, t, "coalesce", True))
            return
        if item is not None:
            return  # sent and in flight: the next decision after it lands handles the rest
        payload = {"side": side, "price": price, "qty": qty, "queue": book.side(side).size_at(price),
                   "parent": parent, "improved": improved, "decided": t}
        slot[side] = payload
        schedule(t, "place", payload, book.rtt)

    def inventory() -> float:
        return held[LipSide.YES] - held[LipSide.NO]

    def on_fill(o: OrderState, qty: float, t: float) -> None:
        if qty <= 0:
            return
        held[o.side] += qty
        filled_since[o.side] += qty
        known = t + regime.latency(last_book.rtt if last_book else 0.0, params)
        last_fill_known[o.side] = min(last_fill_known[o.side], known)

    def live_orders(t: float) -> list[OrderState]:
        return [o for o in path.orders if o.live(t)]

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
            if "cancel_on_land" in payload:
                o.credit_end_low = min(o.credit_end_low, payload["cancel_on_land"])
                schedule(t, "cancel", {"order": o}, last_book.rtt if last_book else 0.0)
                return
            resting[side].append(o)
            if scenario is not FillScenario.THROUGH:
                pending_first_book.add(o.oid)
        elif kind == "cancel":
            o = payload["order"]
            o.fillable_until = min(o.fillable_until, t)
            o.credit_end_high = min(o.credit_end_high, t)
            if o in resting[o.side]:
                resting[o.side].remove(o)
        elif kind == "decrease":
            o = payload["order"]
            decreasing.discard(o.oid)
            o.remaining = min(o.remaining, payload["to"])

    def drain_internal(upto: float) -> None:
        while internal and internal[0][0] <= upto:
            t, _, _, item = heapq.heappop(internal)
            land(t, item)

    def decide(book: BookSnapshot, t: float) -> None:
        if gate["state"] != "open" or stopped_until_segment:
            return
        if not (md.program.start_ts <= t < md.program.end_ts) or t >= end:
            return
        rest_px = {s: (resting[s][0].price if resting[s] else None) for s in (LipSide.YES, LipSide.NO)}
        tgt = targets(book, policy, rest_px, params)
        allowed = allowed_sizes(inventory(), size)
        for side in (LipSide.YES, LipSide.NO):
            want = tgt[side]
            if want is None:
                if resting[side]:
                    cancel_side(t, side, book.rtt, "unquotable")
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
                    cancel_side(t, side, book.rtt, "reprice")
                place(t, side, want, allowed[side], book, None, improved)
                filled_since[side] = 0.0
                last_fill_known[side] = math.inf
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
            if (filled_since[side] > 0 and t >= last_fill_known[side]
                    and t - last_replenish[side] >= params.replenish_every_s):
                room = allowed[side] - total
                qty = min(filled_since[side], room)
                if qty > 0 and pending_child[side] is None:
                    # the unfilled remainder waits for inventory room; it is not forgotten
                    place(t, side, want, qty, book, on_book[0].oid, improved)
                    last_replenish[side] = t
                    filled_since[side] -= qty
                    if filled_since[side] <= 1e-9:
                        filled_since[side] = 0.0
                        last_fill_known[side] = math.inf

    for t, _, _, (kind, obj) in events:
        drain_internal(t)
        if last_book is not None and t - last_book_at > params.dead_man_s and (resting[LipSide.YES] or resting[LipSide.NO]):
            cancel_all(last_book_at + params.dead_man_s, last_book.rtt, "dead_man")
            gate["state"], gate["count"] = "evaluating", 0
            drain_internal(t)
        if kind == "print":
            j = obj
            yes_px, sz, taker_yes = int(trades.yes_px[j]), float(trades.size[j]), bool(trades.taker_yes[j])
            if bool(trades.block[j]):
                continue
            for o in live_orders(t):
                rel = print_relation(o.side, o.price, yes_px, taker_yes)
                if rel is None:
                    continue
                if rel == "at":
                    key = (o.side, o.price)
                    at_level_volume[key] = at_level_volume.get(key, 0.0) + sz
                before = o.queue
                on_fill(o, apply_print(o, scenario, rel, sz, t), t)
                if o.queue != before:
                    record_queue(o, t)
        elif kind == "book":
            book: BookSnapshot = obj
            prev = last_book
            for o in live_orders(t):
                side_now = book.side(o.side)
                level_now = side_now.size_at(o.price)
                if o.oid in pending_first_book:
                    pending_first_book.discard(o.oid)
                    o.queue = max(o.queue, level_now) if scenario is FillScenario.FIFO else min(o.queue, level_now)
                    record_queue(o, t)
                    continue
                if prev is None:
                    continue
                prev_t = prev.received_at
                shrink = prev.side(o.side).size_at(o.price) - level_now
                explained = at_level_volume.pop((o.side, o.price), 0.0)
                unexplained = shrink - explained
                covered = md.coverage.covered(prev_t, t)
                before = o.queue
                on_fill(o, apply_shrink(o, scenario, level_now, unexplained, covered, t), t)
                opposite = book.side(o.side.other)
                crossing = float(opposite.sz[opposite.px >= 100 - o.price].sum()) if not opposite.empty else 0.0
                on_fill(o, apply_cross(o, scenario, crossing, prev_t), prev_t)
                on_fill(o, apply_orphan(o, scenario, side_now.best, prev_t), prev_t)
                if o.queue != before:
                    record_queue(o, t)
            at_level_volume.clear()
            last_book, last_book_at = book, t
            if gate["state"] == "evaluating":
                ok = (gate_passes(book, target) and _is_open(md, t, True)
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
            if gate["state"] == "open":
                decide(book, t)
        elif kind == "status":
            p = obj
            if p.market_status.lower() not in OPEN_STATUSES or p.exchange_active is False:
                if last_book is not None:
                    cancel_all(t, last_book.rtt, "status")
                stopped_until_segment = True
        elif kind == "segment":
            stopped_until_segment = False
            gate["state"], gate["count"] = "evaluating", 0
        elif kind == "end":
            rtt = last_book.rtt if last_book is not None else 0.0
            cancel_all(t, rtt, "end")
            drain_internal(t + 30)
            _flatten(path, md, held, t, regime, queue, rtt, params)
            break
    drain_internal(math.inf)
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
    qty, proceeds, fee = abs(net), 0.0, 0.0
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
    """One order's fills as prefix sums, and its queue history, for O(log n) lookups."""

    order: OrderState
    fill_t: list[float]
    fill_cum: list[float]
    queue_t: list[float]
    queue_v: list[float]
    #: when the order was filled out, if it ever was: it earns nothing after that
    done_at: float = math.inf

    def remaining(self, t: float) -> float:
        i = bisect_left(self.fill_t, t)
        return self.order.size - (self.fill_cum[i - 1] if i > 0 else 0.0)

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
        ft, fc, run = [], [], 0.0
        for f in sorted(o.fills, key=lambda f: f.at):
            run += f.qty
            ft.append(f.at)
            fc.append(run)
        hist = path.queue_history.get(o.oid) or [(o.live_from, o.queue)]
        done = ft[-1] if fc and fc[-1] >= o.size - 1e-9 else math.inf
        ledgers.append(_Ledger(o, ft, fc, [h[0] for h in hist], [h[1] for h in hist], done))
    ledgers.sort(key=lambda lg: lg.order.live_from)
    absorbed = sorted((f for f in path.fills if f.kind in ("at_level", "through", "gap", "orphan")), key=lambda f: f.at)
    absorbed_t = [f.at for f in absorbed]
    crosses = sorted((f for f in path.fills if f.kind == "cross"), key=lambda f: f.at)
    crosses_t = [f.at for f in crosses]
    level_cache: dict[int, tuple] = {}
    target_cache: dict[tuple, int | None] = {}
    v_sum, seconds, counted, observed, credited = 0.0, 0, 0, 0, 0
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
                for px in [px for px in base if px >= 100 - f.price]:
                    base[px] = max(0.0, base[px] - f.qty)
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
                            if q <= 0:
                                continue
                            price = credited_price(before, side, o.price)
                            better = sum(qq for px, qq in cf if px > price)
                            if path.scenario is FillScenario.THROUGH:
                                ahead = next((qq for px, qq in cf if px == price), 0.0)
                            else:
                                ahead = lg.queue(k)
                            q_eff = min(max(target - better - ahead, 0.0), q)
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
        "accrued": accrual(reward, v_sum, n_total), "n_total": n_total,
    }
