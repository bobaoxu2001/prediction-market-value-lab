# Kalshi execution harness (Phases 5A-5C)

Authenticated account access, a demo execution harness, order/fill reconciliation
and a hard risk kill switch — built **before** the research system is authorized
to trade, so that authorization later is a policy decision rather than an
engineering scramble.

```
LIVE_AUTONOMOUS_TRADING_AUTHORIZED = NO
LIVE_ORDER_SUBMISSION_AUTHORIZED   = NO
```

Neither line is a configuration setting. See "Why real money is unreachable".

## Modes

| Mode | Authenticates | Reads | Writes |
|---|---|---|---|
| `SHADOW` | no | no | no — constructs, risk-checks and persists the intent, then stops |
| `DEMO` | Kalshi sandbox | yes | yes, sandbox only |
| `PRODUCTION_READ_ONLY` | real account | yes | **never** |

There is no `PRODUCTION_LIVE`. Not disabled — absent.

## Why real money is unreachable

Six independent gates. Any one of them alone is sufficient; the lowest is the one
that carries the guarantee.

1. **Mode gate** — `ExecutionMode` has no live member. There is no value to pass
   and no flag to flip.
2. **Environment gate** — the base URL is *derived* from the mode
   (`base_url_for`), never supplied beside it, so a demo mode cannot hold a
   production endpoint.
3. **Credential gate** — credentials carry their environment. Crossing them
   raises `CredentialsUnavailable` instead of sending a request somewhere
   unintended.
4. **Client write guard** — `KalshiExecutionClient` refuses to *construct* in a
   non-writing mode, and re-asserts on every mutating call.
   `KalshiAccountClient` has no mutating method at all.
5. **Risk gate** — refuses any intent whose mode touches a real account,
   independently of what the client would have done.
6. **Transport guard** — `assert_transport_write_allowed(method, url)` refuses
   every non-GET verb aimed at a production host, unconditionally, with no
   parameter that disables it. The URL it inspects is the one `httpx` resolves
   (`build_request(...).url`), not a string rebuilt from the profile, and the
   request that is guarded is the request that is sent.

`is_production_url` fails **closed** — an empty, malformed or unrecognised host
counts as production.

### What an adversarial review broke, and what changed

The first cut of gate 6 validated a URL it rebuilt by hand from
`profile.base_url` while issuing the call through `self._client`, whose
`base_url` nothing checked. A review reproduced two working bypasses: a
caller-supplied `httpx.AsyncClient` pointed at production, and an absolute URL
passed as `path`. Both delivered a DEMO-labelled order `POST` to
`api.elections.kalshi.com` with all six gates reporting success.

Three changes closed it:

- the transport refuses a caller-supplied client whose `base_url` disagrees with
  the profile, or that has redirects enabled;
- the guard now inspects the resolved request URL, which also closes the
  absolute-`path` escape;
- "read" means `GET` and nothing else — the old test was membership in a
  four-verb write set, which silently exempted every verb nobody had listed.

`tests/test_phase5a_adversarial_regressions.py` keeps both attacks as tests.

## Layout

```
pmvl_markets/execution/
  modes.py         modes, environments, the six gates
  credentials.py   loading + RSA-PSS signing (verified against Kalshi docs, 2026-09)
  transport.py     signed HTTP; no cache, no redirects, gate 6
  client.py        KalshiAccountClient (read) / KalshiExecutionClient (write)
  models.py        typed models + order state machine
  idempotency.py   deterministic client_order_id
  ledger.py        durable execution ledger (its own SQLite file)
  risk.py          risk gate + kill switch
  reconcile.py     order/fill/position reconciliation
  resolve.py       market identity resolution
  harness.py       intent -> risk -> venue
```

## Authentication

Verified against Kalshi's published API-key documentation rather than from memory:

- headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- signed string is `timestamp_ms + HTTP_METHOD + path`
- the path is signed **without** its query string
- RSA-PSS over SHA-256, MGF1(SHA-256), salt length = digest length

Configuration (environment variables; production and demo are separate fields so
a demo run cannot authenticate with production keys by configuration alone):

```
KALSHI_API_KEY_ID              KALSHI_DEMO_API_KEY_ID
KALSHI_PRIVATE_KEY_PEM         KALSHI_DEMO_PRIVATE_KEY_PEM
KALSHI_PRIVATE_KEY_PATH        KALSHI_DEMO_PRIVATE_KEY_PATH
```

Prefer the `_PATH` form, pointing outside the repository. The key is parsed in
memory only. It is never logged, never persisted, and `KalshiCredentials` defines
`__repr__` by hand so the dataclass default cannot print the PEM.

Signing requires `cryptography`, declared in
`packages/market-normalization/pyproject.toml`. If it is missing, signing raises
`SigningUnavailable` rather than falling back to an unsigned request.

## Idempotency

`client_order_id` is **derived**, not generated:

```
sha256(version, mode, strategy, strategy_version, decision_epoch,
       signal_id, ticker, outcome, action, price, quantity, revision)
```

The same logical decision yields the same id on any machine, before and after a
restart, so retrying is safe by construction. Wall-clock time and attempt
counters are deliberately excluded. Changing your mind is a `revision` bump — a
different id — which keeps "replacing my order" and "retrying my order" distinct.

`new_attempt_id()` exists for tracing and is never sent as the idempotency key.

## Execution state storage

`data/execution.db` — a **separate SQLite file**, not `pmvl.db`.

1. `pmvl.db` is under an open contention investigation: measured write
   transactions held 40–55 s after the Family C repair and up to 348 s before it,
   with organic `database is locked` victims around 0.68/hour. A write that can
   be starved for a minute is the wrong home for "did I just send an order".
2. A lost ledger row is worse here than anywhere else in the repo. Phase 4C
   already found an `OperationalError` deleting a `job_runs` row; the same
   failure against an order record means the system does not know it holds a
   position.
3. Nothing spans both stores. An order's durability has no invariant in common
   with a research snapshot's.

Precedent: `research-archive.db` is already a second store with its own lifecycle.

## Risk limits

All eleven caps are live. Two of them — the daily loss breaker and the daily
spend cap — were initially inert: nothing populated them and their fields
defaulted to `Decimal("0")`, so unknown state read as "nothing lost yet" and the
check always passed. They are now `None` by default, the risk gate blocks on
unmeasured cumulative state, realized loss is measured from the venue's own
positions, and daily spend is measured from the ledger.

Defaults are deliberately tiny (`CONSERVATIVE_LIMITS`) and are absolute, not
fractions of equity — a cap expressed as "2% of balance" grows with the account,
which is the opposite of what a safety limit is for. **The account balance is
never treated as available risk capital.**

Enforced identically in DEMO: risk logic that has only ever run against real
money has never been tested.

Every check fails closed. Unknown exposure blocks, a missing market-data
timestamp blocks, an unknown edge blocks. A rejection reports *all* failing
reasons, not the first.

Production-live limits are not derivable from these and require explicit
ratification in a later phase.

## Kill switch

Three arms, checked as a disjunction:

- **configuration** — `EXECUTION_ENABLED` (default **false**: a fresh install
  does not trade, even in demo)
- **runtime** — in-process, trippable by an operator or a circuit breaker
- **persistent** — `data/EXECUTION_DISABLED`; its existence disables execution,
  so a crash-restart loop cannot clear it

Paths are anchored at the repo root. A cwd-relative kill switch fails **open**
for any process started from another directory — precisely the crash-restart case
the file arm exists for — and a cwd-relative ledger silently becomes an empty
database reporting zero unresolved orders. `RiskGate()` defaults to
`KillSwitch.from_settings()`, so the obvious line of code gets all three arms;
the bare `KillSwitch()` that consults nothing remains available for tests.

`release()` clears only the runtime arm. Reads and reconciliation stay available
while killed — an operator whose kill switch also blinded them would be worse off.

## Reconciliation

A discrepancy is **surfaced, never resolved by trading**. No path in
`reconcile.py` places, cancels or amends an order, and a test asserts the module
does not even mention the write API.

Absence is not a fill. An order the venue no longer lists has not necessarily
filled; without fill evidence it becomes `UNKNOWN_RECONCILIATION_REQUIRED`, which
the risk gate treats as a hard block on new mutations.

Positions are compared by `(ticker, outcome)`. Collapsing to ticker meant the
exact inverse position — long 5 YES against a real long 5 NO — reconciled clean,
which defeated the one check that compares local belief against venue truth.

A blocking discrepancy with no local order row (`REMOTE_ONLY`,
`POSITION_MISMATCH` — the two kinds that mean "the venue holds exposure this
system does not know about") is persisted to `blocking_discrepancies` and counted
by the risk gate. It used to live only on an in-memory report the CLI printed and
discarded, so a ghost order at the venue was fully compatible with the next order
sailing through.

Classifications: `RECONCILED`, `LOCAL_ONLY`, `REMOTE_ONLY`, `STATUS_MISMATCH`,
`FILL_MISMATCH`, `POSITION_MISMATCH`.

`recover_after_restart` reloads the ledger and reconciles against the venue. It
re-submits nothing; mid-flight orders are resolved by *looking them up* via the
deterministic `client_order_id`.

## Operator commands

```bash
pmvl exec status
```

```bash
pmvl exec shadow-order KXHIGHNY-26SEP05-B70 --price 40 --qty 2 --json
```

```bash
pmvl exec kill --reason "operator stop"
```

`account-status`, `positions`, `open-orders`, `fills` and `reconcile` accept
`--mode production` for **reads**; a write in that mode fails locally before any
request is built. There is deliberately no `live-order` command — not even a stub
that raises, because a stub would still appear in `--help`.

## What this does not do

- It does not place real orders, and cannot be configured to.
- It does not wire research `suggested_action` to any venue.
- It says nothing about whether a strategy is profitable. Execution readiness is
  plumbing, not edge.


---

# Phase 5B — authorization contract and the shadow pipeline

## The risk gate is now a type, not a convention

Phase 5A's risk gate returned a *verdict* and the write client took an
`OrderIntent`. Those two facts never touched: `create_order` was public, took raw
caller values, and consulted nothing. The documentation called the risk gate "the
only path from an OrderIntent to a venue mutation"; it was a convention, and
conventions are what the next caller forgets.

```
OrderIntent -> RiskGate.authorize(...) -> AuthorizedExecutionPlan
                                       -> ExecutionGateway.submit(plan)
```

`create_order(plan)` and `submit(plan)` accept **only** a sealed plan. There is no
overload taking a ticker, a price, or an intent, so skipping the gate is not a
shortcut the signature can express. `evaluate()` still exists for reports and dry
runs and deliberately cannot authorize anything.

### Why the plan is trustworthy

**It binds semantics, not permission.** The plan carries the exact order that was
approved. Submitting is not "you may trade", it is "you may place *this*".

**It is issued, not merely sealed.** The first cut sealed plans with an HMAC and
exposed a public `seal()`. A review pointed out that this is the same defect as a
public `create_order`, moved up a layer: the HMAC proves *built in this process*,
not *approved*, so a forged plan for a million contracts at 99c verified
perfectly. Sealing is now private, and `issue_authorization()` — called only by
the risk gate after a passing evaluation — records a per-issuance nonce in a
process-local registry. Membership in that registry is what distinguishes an
approval from a construction, and spending it makes an authorization single-use,
so a plan cannot be replayed into two orders.

Every field the venue receives is inside the seal, and so is the provenance
(`signal_timestamp`, `market_data_timestamp`, `account_state_at`) — those cannot
change what is placed, but a rewritable audit trail is not an audit trail.

**It expires, on the gate's terms.** Default TTL is 90 seconds, hard-capped at 5
minutes. The cap exists because the caller used to choose the lifetime: a
strategy passing `ttl=timedelta(days=3650)` held a permanent licence to place one
specific order against a decade-old exposure snapshot.

The seal key lives only in memory and is regenerated on import, so a plan cannot
survive a restart. That is deliberate: an authorization that outlives the account
state it was computed against is the thing this design exists to prevent.

## Point-of-use re-checks

Every gateway re-verifies at submit: seal, expiry, mode, kill switch, and whether
this mode has unresolved orders or open discrepancies. Defense in depth is
preserved rather than replaced — the write client still refuses to construct
outside a writing mode, and the transport still refuses any non-GET verb aimed at
a production host, independently of anything the authorization layer decided.

## Residual findings from Phase 5A, closed

| | |
|---|---|
| **R1** credentials | `load_demo_credentials()` / `load_production_credentials()` read disjoint config namespaces. The environment is derived from the mode, never supplied beside it. This is `CONFIG_STRUCTURALLY_BOUND` — Kalshi exposes no way to prove a key belongs to a sandbox account, so the stronger claim is not made. |
| **R2** risk bypass | Closed by the plan-only gateway API above. |
| **R3** transport | Both clients assert `transport.profile.environment` matches their own. The *read* path crossed environments silently before this — a demo-labelled client could print real balances under a demo label. |
| **R4** idempotency | `time_in_force` joined the hash; version bumped `v1`→`v2` so no old id is reinterpreted. Every field `create_order` sends is now bound. |
| **R5** mode scoping | Orders, discrepancies, spend and reconciliation are scoped by mode. `recover_after_restart(mode=...)` reads only that mode's rows and stamps `reconciliation_runs.mode` with the real venue. A pre-5B `execution.db` is migrated in place; legacy rows with no mode block **every** mode rather than none. |
| **R6** UNKNOWN recovery | `resolve_unknown_order` transitions only on venue evidence. There is no `force_clear`, `mark_filled`, `mark_cancelled` or `ignore`. Every attempt is audited, including the ones that changed nothing — a failed resolution is the more interesting record. An operator note is recorded and authorizes nothing. |

## Shadow

`ResearchDecision → SignalExecutionAdapter → OrderIntent → RiskGate →
AuthorizedExecutionPlan → ShadowExecutionGateway → ledger`, then stop.

Shadow uses the **same** resolver, risk policy, limits, freshness checks,
idempotency and authorization contract that demo uses. There is no
`ShadowRiskGate`: a shadow verdict from weaker rules would answer a question
nobody asked. `ShadowExecutionGateway` holds no transport and no credentials, so
"shadow accidentally traded" is not a failure it can have.

`SHADOW_AUTHORIZED` is terminal and cannot reach `FILLED`, `PARTIALLY_FILLED` or
`CANCELLED`. Claiming a venue outcome for an order no venue received is the same
fabrication the reconciler refuses one layer down.

Shadow risk is sized against a fixed virtual balance, never the operator's real
Kalshi balance. Authorized shadow plans count as committed exposure, so one run
cannot approve the same market repeatedly and call the total flat.

```bash
pmvl exec shadow-replay --limit 500 --json
```

## The signal adapter

The canonical research decision is the `recommendations` table. The adapter
changes representation and nothing else: it never recomputes a fair value,
invents an edge, a ticker or a size, inverts YES/NO, or substitutes a market. A
recommendation whose `state` is not `STILL_ACTIONABLE` is not actionable —
turning `SETTLED` or `AVOID` into an order would be the execution layer overruling
the model it serves.

Price policy is the decision's own: `entry_price` is the price the EV was
computed at, so it is both the limit and the ceiling. No execution optimizer is
introduced here.

Every decision produces either an `OrderIntent` or an `IneligibleSignal` naming
exactly why not. Nothing is silently dropped, because the denominator is the
point: "how many signals did the robot want to trade" is unanswerable if the
refusals are not recorded.

**The current research record contains no executable Kalshi signal.** All 462
rows are `provenance='demo'` seed fixtures; of the 12 still-actionable ones, 7 are
Polymarket and 5 are closed markets. The adapter refuses `provenance='demo'` by
default so seed rows cannot be mistaken for model output.

## Still open

Not on the signal→risk→execution safety path, documented rather than fixed:

- `ExecutionLedger.force_status_from_evidence` is a public ledger method. It
  requires the row to be UNKNOWN and is called only by `resolve_unknown_order`,
  but a determined caller could invoke it directly.
- Kalshi supports order fields this code never sends (post-only, reduce-only,
  non-limit types). They are absent from the idempotency hash because hashing a
  value that never varies is theatre; adding one later requires a version bump.


---

# Phase 5C0 — why there are no real signals, and what a quantity means

## Zero real signals is the model working, not a bug

The live weather pipeline runs. `model_predictions` holds 64,469 rows under
`ensemble-v1.0.0`, 5,747 of them on real `KXHIGH*` markets, updated continuously.
52 real weather markets reach the scorer on a current pass, and the latest `score`
job built **600 candidates and wrote 0 recommendations**.

Reconstructing that pass read-only: 594 candidates built, **0 passed**
`passes_gates`. For real weather specifically — 104 candidate-sides — every one
either could not be built or had a **negative** conservative net EV. The model is
saying there is no edge after costs, which on a book whose best ask is 1c is
exactly what it should say.

So the root cause is `D — WEATHER MARKETS ARE SCORED BUT FAIL THE ACTIONABILITY
GATE`. It is **not** a persistence bug and **not** Phase 4C1: nothing in
`publish_recommendations` or `rank_candidates` consults CLEAN or the 4C gate. A
recommendation would be written the moment one cleared `min_conservative_net_ev`.

The 462 rows that do exist are all `provenance='demo'` seed fixtures from June–July.

## Three quantities, never collapsed

```
approved_quantity <= requested_quantity <= capacity_ceiling
```

| | what it is | who owns it |
|---|---|---|
| `capacity_ceiling` | what the book absorbs before the marginal contract stops being +EV (`max_profitable_size`, bounded at 5000) | research |
| `requested_quantity` | what a named, versioned sizing policy asked for | **no component yet** |
| `approved_quantity` | what the risk gate permitted — it may reduce, never raise | risk |

`POSITION_SIZING_OWNER = NO COMPONENT YET`. The closest thing is
`fractional_kelly`, and it is a fraction of a bankroll the research layer does not
model; nothing converts it to contracts. So `STRATEGY_POSITION_SIZE_DEFINED = NO`
and `StrategySizingPolicy` refuses rather than guessing.

`DEMO_EXECUTION_PROBE` sizes **one contract**, DEMO-only, bound in three places.
It is a venue-mechanics test and expresses no view; no result obtained with it
says anything about sizing quality or profitability.

## What the adversarial review broke this time

Eight reproduced findings, all fixed:

- **The risk gate sealed a quantity it re-read after the checks.** `evaluate()`
  calls into the caller-supplied exposure object, and `authorize()` then re-read
  the intent to build the plan. An exposure that mutated the intent in that
  window produced a sealed, gateway-admissible plan for **5000 contracts against
  a 5-contract cap**. Every venue-affecting field is now snapshotted once, before
  anything that can run caller code, and the snapshot is what gets sealed. The
  ordering guard is a `raise`, not an `assert` that `python -O` strips.
- **Sizing was bound to a quantity but not to a market.** A decision sized against
  a deep book was spendable on a thin one. Ticker and ceiling are now cross-checked.
- **`STRATEGY_SIZED` was guarded by `is`**, so a look-alike `StrEnum` with the same
  value was issued, sealed, and reached the ledger's audit column — the one label
  reserved so "the strategy sized this" cannot be declared. Compared by value now,
  and the policy must be a genuine member.
- **The fixture gate was a denylist of one string.** `fixture` — a replayed
  recorded response — passed, as did any casing and any unenumerated value. It is
  an allowlist of `live` now, matching how every other classifier here fails closed.
- **A BUY limit rounded half-even**, so 0.5966 became 60c: a third of a cent above
  the price the edge was measured at, on edges of 1–5c. Four of the twelve live
  actionable rows rounded up, one by 14.5% of its own edge. And the intent's price
  ceiling was set to the same rounded number, so `PRICE_ABOVE_MAX` compared a value
  against itself and could never fire. Rounds down now, ceiling from the unrounded
  research price.
- **`market_data_timestamp` was the signal timestamp**, making `MARKET_DATA_STALE`
  a duplicate of `SIGNAL_STALE` — the same inert-limit shape as the daily loss and
  spend breakers. It reads `quote_observed_at` now.
- **Sizing decisions were never spent**, so one could underwrite many orders.
- **The ledger recorded whatever label the caller asserted**, unverified.

## Operator commands

```bash
pmvl exec shadow-replay --limit 500 --json
```

```bash
pmvl exec discrepancies --mode demo --clear
```


---

# Phase 5C — demo venue mechanics

## What was validated, and against what

There are no demo credentials configured, so **nothing was sent to Kalshi**. The
order lifecycle is instead driven through the real stack against a stateful fake
venue (`tests/fake_kalshi_demo.py`): the transport, client, harness, risk gate,
authorization contract, sizing contract, ledger and reconciler are all production
code, and only the socket is replaced.

That proves our half of the conversation — how this system behaves given a venue
response. It cannot prove the venue responds as modelled. Every claim below is
about the former.

```
DEMO_CREDENTIALS_PRESENT = NO
DEMO_LIVE_SMOKE          = BLOCKED_MISSING_DEMO_CREDENTIALS
```

To unblock, an operator sets, in the environment or a gitignored `.env`:

```
KALSHI_DEMO_API_KEY_ID=<demo key id>
KALSHI_DEMO_PRIVATE_KEY_PATH=/path/outside/this/repo/demo-key.pem
```

then arms demo writes for a bounded window:

```bash
pmvl exec demo-arm --minutes 30 --reason "5C first probe" --confirm
```

## Time-bounded demo arming

`EXECUTION_ENABLED` is false by default, which engages the kill switch, which is
correct. Turning it on permanently is a bad way to run a first probe, so the arm
exists: a file carrying an expiry that satisfies the **configuration** arm of the
kill switch, for **DEMO only**, for at most four hours.

- shadow and production are unaffected — verified, not asserted
- an expired, unreadable, or over-long arm means *not armed*
- `pmvl exec kill` overrides it absolutely
- `pmvl exec demo-disarm` revokes it immediately
- the arm file is gitignored; committing one would arm every checkout

It cannot enable a production write, because no mode can perform one.

## What the adversarial review broke

Seven reproduced defects, all fixed. Three attackers found the first one
independently.

**A cancel racing a partial fill was recorded as terminal CANCELLED.** The guard
consulted `record.filled_quantity` — local state that reconciliation had not yet
populated, so it read 0 in exactly the case the race creates. The venue's own
DELETE response carried `taker_fill_count` and was discarded. Because CANCELLED
is terminal and outside `OPEN_STATUSES`, nothing looked at the row again: the
ledger permanently asserted flat while the account held contracts. The guard now
reads the venue's answer. Note `initial - remaining` is *not* a fill count on a
cancel response — cancelling legitimately drives remaining to zero — so an absent
`taker_fill_count` is treated as unknown rather than as zero.

**`resolve_unknown_order` wrote terminal FILLED from a status string with no fill
rows.** Exactly what `_apply_submit_response` refuses one layer down. A venue
status is evidence of an outcome, never of a quantity; the ledger now refuses it
and the resolution stays UNKNOWN until the fills arrive.

**Gate 6 validated the pre-redirect URL.** `follow_redirects` was checked at
construction, but httpx reads it at send time, so flipping the attribute on a
supplied client let a 307 from the demo host re-send an order body to production.
Now pinned per send.

**The demo arm was inert.** No shipped caller passed `mode=` to
`KillSwitch.from_settings`, so the command reported success and unblocked
nothing — the five tests establishing it exercised a path no caller took. Wired
through `RiskGate(mode=)` and the gateways.

**`issue_sizing` would mint a `demo_execution_probe` label for any quantity.** The
size was bound only inside the policy object, so bypassing the policy bypassed the
size and a 5-contract order read in the audit trail as a 1-contract mechanics
test. Bound at issuance now, the same guard shape that reserves `STRATEGY_SIZED`.

**Account reads were a single 200-row page with the cursor dropped.** A ghost
order past row 200 reconciled clean and exposure under-counted — the one unknown
in this subsystem that reported success instead of blocking. Reads paginate; an
endless cursor raises rather than returning a partial account.

**`harness.cancel` raised `InvalidTransition` on an UNKNOWN order** — the same
crash-instead-of-explain defect fixed on the submit path, left on cancel, landing
at the moment an operator is most likely to reach for it.

## Still not validated

No order has touched a venue. Fills, partial fills, cancellation, restart
recovery and idempotency are proven against a fake that this repository wrote.
`NATURAL_DEMO_PARTIAL_FILL_OBSERVED = NO`, and no execution statistics, latencies,
slippage or fee data exist, because no venue produced any.
