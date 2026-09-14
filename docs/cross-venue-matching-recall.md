# Cross-venue matching: finding a Kalshi/Polymarket pair, and pricing it

**Measured 12 September 2026** against live ingests of ~8,200 Kalshi and 1,500
Polymarket markets.

Two questions look identical from outside an empty arbitrage list: *the venues do not
list the same contracts*, and *we cannot see that they do*. This document separates
them with numbers, because the first is a finding and the second is a bug. It turned
out to be the second — and then, once the pairs were visible, the first as well.

## Where this started

The guaranteed-arbitrage label was unreachable by construction, for two independent
reasons:

1. `_cancellation_component` in `matching/equivalence.py` returned `UNKNOWN`
   unconditionally, so `VERIFIED_EQUIVALENT_STRICT` — the only verdict mapped to
   `ArbitrageLabel.EXECUTABLE` — could never be derived.
2. `arbitrage/pipeline.py` passed `scan_cross_platform` a bare `MatchVerdict`, which
   `_as_equivalence` deliberately caps at `STANDARD`.

Both are fixed; `tests/test_guaranteed_arbitrage.py` pins the end-to-end property.
And it did not matter yet, because the matcher was finding **zero** cross-venue pairs
to label at all.

| Scan | Candidate pairs | Verified matches |
|---|---:|---:|
| Before this work | 478 | **0** |
| After | 3,002 | **12** |

The diagnosis string flipped with it, from *"on this sample the two venues do not list
contracts with identical settlement terms"* to *"12 of 3002 were blocked ONLY by
fields the parser could not establish"*.

## Gap 1 — ticker versus name (fixed)

`unmatched_proper_nouns` compared raw text, so Kalshi's `SOL` and Polymarket's
`Solana` read as two different subjects. Both sides are now alias-expanded through
the existing `ENTITY_ALIASES` map. On the live scan `entity_mismatch` fell from 478 to
314 and stopped being the largest demotion reason.

## Gap 2 — event linkage (not needed for matching; see below for the scanner it did starve)

84% of open Kalshi markets (26,965) carry an event identifier resolving to no event
row, because event fetching is capped at `min(600, market_limit)` while market
fetching is not. This was assumed to be the blocker for sports matching.

It is not. **Kalshi restates the entire fixture in its own rules prose**, so nothing
about the matchup requires an event row:

> If San Diego wins the **San Diego vs San Francisco professional baseball game
> originally scheduled for Sep 11, 2026 at 10:15 PM EDT**, then the market resolves
> to Yes.

The gap is still real and still starved the multi-outcome scanner, which needs
`Event.outcome_count` to establish that a basket is complete — that is dealt with
under *Gap 2, revisited* below. It was never on the path to cross-venue arbitrage.

## Gap 3 — the venues title sports contracts differently (closed)

| Venue | Title | Outcomes |
|---|---|---|
| Kalshi | `San Diego wins` | `["Yes", "No"]` |
| Polymarket | `San Diego Padres vs. San Francisco Giants` | `["San Diego Padres", "San Francisco Giants"]` |

Kalshi names the **outcome** and leaves the matchup to its rules; Polymarket names
the **matchup** and leaves the outcome to an array. Measured token similarity for
that real pair is **0.286** — under the verifier's 0.35 floor, and over the 0.28
blocking floor only by luck. No threshold tuning finds these.

`matching/fixtures.py` identifies the contest structurally instead. Four things must
agree, each load-bearing — the first was added after an adversarial pass showed its
absence producing false pairs (see below):

* **Competition.** Kalshi's league comes from its event ticker (`KXMLBGAME`),
  Polymarket's from its prose ("the upcoming WNBA game"). Both sides must resolve one
  and they must agree.
* **Competitors**, by prefix-token containment rather than a league registry: every
  token of the shorter name must prefix a *distinct* token of the longer one, and the
  leftover tokens must extend the identity rather than replace it. This accepts
  `chicago c` / `Chicago Cubs` and `new york y` / `New York Yankees`, and rejects
  `chicago c` / `Chicago White Sox` and `washington` / `washington state`.
* **Date and stated start time.** The date alone is not enough twice over: a series is
  several games on consecutive days, and a *doubleheader* is two games between the
  same teams on one date. Doubleheaders are what postponements create — the exact
  situation where these venues' rules diverge most — so the stated first-pitch time
  is required to agree within three hours when both venues give one.
* **Proposition.** Only a head-to-head moneyline. The same fixture also carries
  totals, spreads and "team to advance"; a Polymarket title with a `:` suffix, or
  whose outcomes are not the competitors named in its title, is rejected.

A fixture match establishes *subject identity* only. It relaxes the checks that are
**inapplicable** to a moneyline — there is no strike, no comparator and no measurement
basis, and reporting those as "unverified" demoted every sports pair for failing to
state terms that do not exist. It does not license a risk-free claim: a fixture pair
is capped below the strict verdict by construction.

Effect on one real pair (Kalshi `San Diego wins` × Polymarket `San Diego Padres vs.
San Francisco Giants`):

| | Before | After |
|---|---|---|
| Proposed as a candidate | no | yes |
| Demotion codes | 7 | 1 (`source_unknown`) |
| `rule_compatibility` | `incompatible` | `equivalent` |
| Verdict | `REJECTED` | `PROBABLE_MATCH` |

Polarity is derived from the fixture rather than from a comparator, and was verified
against two complementary markets of one live contest: Kalshi `Colorado wins` against
the Polymarket Rockies/Tigers market is **not** inverted, Kalshi `Detroit wins`
against the same market **is**.

## What the prices actually say

Book coverage is the other half. Cross-venue arbitrage is the only product needing a
book on two venues at once, and the other reserves rank each venue independently by
liquidity, so both halves of a pair rarely won their own venue's draw. A match
reserve now claims a share of the orderbook budget, and matched legs are hydrated
from the database so a pair is not starved merely because one of its markets missed
this cycle's listing.

With both legs priced, on four fully-quoted MLB moneylines:

| Contest | Cheapest cross-venue pair |
|---|---:|
| Philadelphia / Atlanta | **$1.0000** |
| Kansas City / Boston | **$1.0000** |
| LA Dodgers / Miami | $1.0100 |
| (opposite orientations) | $1.0200 |

**Every pair sums to at least $1.00 before a single fee.** There is no arbitrage here;
the two venues are cross-arbitraged to within a cent by people faster than this
scanner. That is the honest answer to "is there risk-free arbitrage between Kalshi and
Polymarket", and it is now an answer backed by prices rather than by an empty list.

## Why these pairs could not be riskless even at a discount

Worth stating separately, because it does not depend on today's prices. The two
venues' sports rules genuinely differ:

> **Kalshi** — "If this game is postponed or delayed, the market will remain open and
> close after the rescheduled game has finished (**within two days**). If the game is
> cancelled or rescheduled to over two days away, the market will resolve to **a fair
> price in accordance with the rules**."

> **Polymarket** — "If the game is postponed, this market will remain open **until the
> game has been completed**. If the game is canceled entirely, with no make-up game,
> this market will resolve **50-50**."

A game postponed more than two days has Kalshi settling at a "fair price" while
Polymarket is still waiting. The legs diverge, and the position is not a hedge. The
cancellation component reads this correctly: Kalshi's "fair price in accordance with
the rules" classifies as `UNSTATED` rather than being forced into a category, so the
pair caps at `PROBABLE_MATCH` and is labelled Rule Mismatch Risk. Closing Gap 3 was
never going to turn sports into risk-free arbitrage, and it did not.

## What an adversarial pass found, and what it changed

The first working version of fixture matching was attacked before it was trusted, on
the live 53,336-row database. It produced false pairs. The gates below exist because
each one was demonstrated, not anticipated:

| Defect | How it failed | Fix |
|---|---|---|
| **The sport guard was inert** | Polymarket writes the *league* ("the upcoming WNBA game"), never the sport, so 79 of 99 real Polymarket fixtures carried `sport=""` and the check never ran. A Kalshi MLB *Seattle vs Toronto* game certified as a Polymarket WNBA *Toronto Tempo vs Seattle Storm* game — inverted, tradeable, and priced. Kalshi names competitors by city alone, so nothing else separated them. | The **competition** is now required on both sides and must agree. Kalshi's league comes from its event ticker (`KXMLBGAME`), Polymarket's from its prose; silence on either side refuses the fixture. |
| **Prefix containment conflated programs** | `washington` matched `washington state`, `kansas`/`kansas state`, `michigan`/`central michigan`. Thirteen real collisions existed between the Kalshi NFL and Polymarket college-football catalogs, where the sport guard *does* run. | Leftover tokens of the longer name are checked: a nickname extends an identity, a qualifier (`state`, `tech`, `southern`) replaces one. |
| **Doubleheader guard was skippable** | It only ran when both venues stated a clock, and 122 of 421 real fixtures state none. It was also inclusive at exactly 180 minutes — a common split-doubleheader gap. Kalshi states "game 1 of the double header" in the prose being parsed, and the extractor discarded it. | The marker is captured and must match. For baseball, both sides must state a first pitch. The boundary is now exclusive. |
| **The fixture branch overrode published strike metadata** | A market carrying `floor_strike=1.5` still printed "not applicable: a moneyline has no strike" and paired with a full-game moneyline. | A market that publishes a strike is not a moneyline and yields no fixture. |
| **A trailing qualifier was skipped** | The Kalshi regex stopped at the sport word, so "…baseball game **after five complete innings** originally scheduled for…" still matched as a full-game moneyline. | The span is anchored to the end of the YES condition; only a doubleheader marker may appear. |
| **The proper-noun whitelist was self-referential** | It tested nouns against a pooled blob of *both* sides' competitor names, so across 64 live pairs 0 of 128 raised nouns ever survived. | Now whole-token rather than substring. Its real limit is documented in the code: it cannot separate two leagues, because "Storm" beside "Seattle" is structurally identical to "Padres" beside "San Diego". The competition check is what does that. |
| **Nothing structurally capped the tier** | What actually kept sports pairs below the strict verdict was the resolution digest happening to differ — an incidental hash comparison. Both named gates (settlement source, cancellation) fall to ordinary venue prose. | A fixture pair is capped at `VERIFIED_EQUIVALENT_STANDARD` by construction, so no coincidence elsewhere can print `executable` for one. |

After these fixes the live scan still verifies the same 12 genuine matches, and the
reproduced attacks are blocked.

## The three the review left open, and how they were closed

* **The instruction did not say which team to buy.** `ArbLeg` now carries
  `outcome_label` — the venue's own name for the outcome that leg buys, empty when
  the outcomes really are Yes/No. The table renders it beside the side and the
  headline names it, so an inverted pair reads `kalshi YES / polymarket YES
  Philadelphia Phillies: Atlanta wins` instead of the previous `polymarket YES`
  with nothing to say which team that was.
* **The contest date was a function of the wall clock.** A yearless Polymarket date
  is now anchored to the market's own timing fields, considering neighbouring years
  so a contest on 31 December resolved by a market closing on 1 January lands in the
  right one.
* **The fixture proposer had no timing corroboration.** It still bypasses the token
  proposer's 3-day resolution-gap gate — it has to, since every genuine pair sits
  6d23h apart while Kalshi holds its market open past the game — but a parsed contest
  date more than 30 days from the venue's own timing is now refused. That catches a
  date wrong by a year without second-guessing a venue by a day.

## Gap 2, revisited — event linkage (closed for the scanner that needed it)

The backlog is real: ~28,000 open Kalshi markets referenced an event ticker resolving
to no row, because event discovery is paged and capped while market ingestion is not.
The scanner it actually starved was the multi-outcome basket, which refuses to price
an event whose venue-reported outcome count is unknown.

`GET /events/{ticker}?with_nested_markets=true` resolves a named event directly, so
the gaps that are *in scope* can be filled without paging further through events
nobody is pricing. Each cycle fills the most-traded 150 per venue, ranked by volume,
and already-filled ones drop out of the query.

Effect on the live scan, measured before and after:

| Multi-outcome skip reason | Before | After |
|---|---:|---:|
| Venue outcome count unknown (no event row) | dozens | **0** |
| Partial basket (priced N of M outcomes) | 46 | 46 |

Every in-scope event now has an authoritative outcome count. What remains is a book
budget limit rather than a metadata gap — pricing 8 of an event's 54 outcomes is not
a complete set, and the scanner is right to say so.

## Verification of the labelling, dating and backfill changes

Checked against every real row they apply to rather than on constructed examples:

| Check | Result |
|---|---|
| `outcomes[0]` ↔ `clobTokenIds[0]` ↔ stored `yes_token_id` | 3,761 open Polymarket rows, **0 misaligned** — so `outcome_label` cannot name the wrong team |
| Polymarket `outcome_label` agrees with the fixture's own side | 79 of 79 fixture-eligible markets |
| Kalshi `outcome_label` empty (its outcomes really are Yes/No) | 322 of 322 |
| Contest-date drift from the venue's own timing | 401 fixtures, min 0 / median 1 / p95 7 / **max 8 days** against a 30-day gate — the gate rejects **0** real fixtures while still catching a year error |
| Fixtures with no reference time to anchor against | 0 |
| Nested market list truncated for large events | No — `/events/{ticker}?with_nested_markets=true` matched `/markets?event_ticker=…` exactly on an 82-outcome event, so `outcome_count` cannot come back too low |
| Fixture tails containing more than one date | 0 of 401 (the date nearest the market's timing is chosen anyway) |

A wrong `outcome_count` is the one failure here that could manufacture a false
risk-free claim, since the basket scanner treats it as authoritative — hence checking
truncation directly against the API rather than trusting the code comment. The
failure direction is also safe: a count that included settled markets would make the
scanner demand more priced outcomes than exist and skip, not certify.

## The side nobody was checking (14 September 2026)

Re-running two days later on fresh data surfaced a structure the scanner was blind
to. An event with N mutually exclusive outcomes trades as a basket in **two**
directions:

* **Long** — buy YES on every outcome. Exactly one resolves YES, so the payout is $1.
* **Short** — buy NO on every outcome. Every leg but the winner pays, so the payout
  is $(N−1).

`scan_multi_outcome` priced only the long side. On live data that is the side where
edge does *not* appear:

| Complete, fully-priced, exclusive baskets | 12 |
|---|---:|
| Gross-profitable **long** (Σ YES asks < $1) | **0** |
| Gross-profitable **short** (Σ NO asks < $N−1) | **1** |

The one was a real Polymarket negative-risk event, `Fed Decision in October?`, with
a **+$0.006 per-set** gross short edge against a −$0.031 long edge. That is the
strongest guarantee structure anywhere in this system — a venue-enforced exhaustive
partition, one venue, so no cross-venue rule divergence, no transfer cost and no
settlement timing risk — and nothing looked at it.

Both directions are now priced and the better is returned.

### The guarantees are not symmetric

Getting this backwards is how the change would go wrong, so it is enforced per
direction rather than shared:

* **Mutual exclusivity** is load-bearing **both** ways. Two outcomes both resolving
  YES pays the long basket $2 on a $1 premise, and costs the short basket a paying
  leg ($(N−2), not $(N−1)).
* **Exhaustiveness** is load-bearing for the **long** basket only. An unlisted
  outcome winning makes every listed leg resolve NO: the long basket pays $0, a total
  loss. The short basket collects on *every* leg and takes $N instead of $(N−1) — the
  missing outcome helps it. Requiring exhaustiveness to short would reject sound
  positions for a risk that runs the other way, so a non-exhaustive short basket is
  flagged as being in the holder's favour rather than downgraded.

### The short basket has an asymmetry of its own, running the other way

The first version of this got the risk profile half right and shipped a false
guarantee for one state, which is worth recording because the reasoning error is easy
to repeat.

"Mutually exclusive and exhaustive" describes the outcome **space**. It does not
promise the event *resolves*. If an event voids and every leg settles at $0.50 rather
than $0/$1 — which is exactly what these venues do on an unresolvable market — the
short basket collects N/2 against a cost near N−1:

| N | long cost | long void payout | long P/L | short cost | short void payout | short P/L |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | $1.00 | $1.00 | $0.00 | $1.00 | $1.00 | $0.00 |
| 3 | $1.00 | $1.50 | **+$0.50** | $2.00 | $1.50 | **−$0.50** |
| 5 | $1.00 | $2.50 | **+$1.50** | $4.00 | $2.50 | **−$1.50** |
| 8 | $1.00 | $4.00 | **+$3.00** | $7.00 | $4.00 | **−$3.00** |

A void *gains* for the long basket and *loses* $(N−2)/2 per set for the short one.
So the short direction may only be called `EXECUTABLE` when the venue enforces the
partition — and negative-risk events do, through their own rules rather than through
a flag:

> **Fed Decision in October** — "If no statement is released by the end date of the
> next scheduled meeting, this market will resolve to the **No change** bracket."

> **Emmys 2026** — "If for any reason no winner is declared… or in case of **overall
> cancellation** or a tie for winner, this market will resolve in favor of the
> nominee which comes **first in alphabetical order**."

Both guarantee a winner is always declared, which is what rules the void state out.
Without that construction the short basket is a hedge with a real void exposure and
is labelled `NOT_GUARANTEED` with the dollar figure stated. A negative-risk basket
still reports what a void would have cost, so the reader can see what the label rests
on. At N=2 the exposure is zero and no flag is raised.

Note what is *not* the reason: a missing outcome. That genuinely helps a short
basket, so citing exhaustiveness as its risk would be wrong in substance even where
the label happened to be right in effect.

### What an adversarial pass found in the short direction

The first version got the duality half right and shipped three defects. All were
reproduced against the live database before being fixed.

**The asymmetry was stated backwards for exclusivity.** The claim was that mutual
exclusivity binds both directions, because two winners "pay the long basket $2 on a
$1 premise". Receiving $2 for $1 is a *windfall*. Hand-settling every assignment of
winners shows the real structure is a clean duality:

| Property | Binds long | Binds short |
|---|:--:|:--:|
| Exhaustiveness (nothing wins) | **yes** — pays $0, total loss | no — pays $N, better |
| Exclusivity (two win) | no — pays $2 for $1 | **yes** — pays $(N−2), short |
| Event-level void (50-50) | no — pays N/2 > $1 | **yes** at N>2 — loses (N−2)/2 |

The code applied exclusivity to both and printed the exhaustiveness *consequence* as
the reason, so a sound long basket was demoted for a risk running in its favour, with
an explanation about a different property. Each trigger now carries its own
consequence, and the test that was meant to catch this — which only ever exercised
the short direction despite its name — now covers both.

**`negative_risk` bypassed the exclusivity gate.** The structure was
`if negative_risk: <flag> else: <every gate>`, so the one property that genuinely
binds the short side was never checked on exactly the events most likely to reach
`EXECUTABLE`. With `negRisk=True` and exclusivity denied, a 5-leg basket returned
`EXECUTABLE` at $3.90/set against an asserted $4.00 payout — two winners pay $3, a
$0.90/set loss on the only label that claims a locked-in result. It was unreachable
today only because the Polymarket provider derives both flags from one boolean, a
coupling no test protected. Negative risk now satisfies exhaustiveness and
void-freedom only; exclusivity is checked unconditionally.

**A failed guarantee was overwritten by fill-quality labels.** `label` was a scalar
that the staleness, liquidity and accepting-orders gates reassigned unconditionally,
so `NOT_GUARANTEED` could be replaced by `stale_quote` — which the API explains as
"a race, not an edge" and the taxonomy classes as "genuinely riskless, but … cannot
be captured". This fired on live data: `US x Iran Effective Ceasefire begins by…?`,
a five-leg *nested deadline ladder* where an early ceasefire resolves every leg Yes,
every NO leg pays $0 and the basket loses its full $14,927 — reported as
`stale_quote` while its own flags said "NOT a guaranteed profit". The guarantee
verdict is now tracked separately and applied last, so it outranks every fill-quality
label. That basket now reads `not_guaranteed`, with the void exposure and the missing
exclusivity stated first and the staleness kept as a secondary flag.

Three further findings were real and fixed alongside: the completeness gate was
one-sided (`<` rather than `!=`), so more legs than the venue reports overstated the
payout; sizing maximised *gross* rather than net, discarding a genuinely executable
$45.50 basket and misreporting the size on those it kept — the same defect already
corrected in the complete-set and cross-venue scanners; and the liquidity floor was
denominated per basket rather than per dollar of payout, binding roughly (N−1)×
looser on the short side. Sizes are now floored to whole contracts and checked
against each leg's minimum order size, and the two directions are compared on return
per dollar of capital rather than raw profit per set.

### It still does not clear its costs

The measured edge is real and too thin, and the honest arithmetic is worth keeping:

```
payout (N−1 = 4 × 100 sets)   $400.00
gross cost                    $399.40
gross edge                    +$0.60
Polymarket fees               −$2.48
slippage pad (1 tick × 5)     −$2.30
NET                           −$4.18
```

The fees concentrate on the two mid-priced legs (NO at $0.39 and $0.63): Polymarket's
fee peaks near $0.50, so a basket on an event with a genuine favourite pays most of
its fee on the favourite's NO leg. That is a structural reason short baskets are hard
here, not bad luck on one event.

The scanner reports nothing for it, which is correct — but it now reports nothing
*having checked*, rather than by construction.

## Known and unfixed

* **Fixture extraction covers head-to-head moneylines only.** Totals and spreads are
  deliberately excluded; matching them needs the line itself compared, not just the
  contest.
* **`source_unknown` is the last demotion standing on the live sports pairs.**
  Polymarket's rules for a game do not name a settlement authority the parser
  recognises. Resolving it would move these to a verified tier — still not to
  `EXECUTABLE`, which a fixture pair is capped below by construction.
* **The event backfill works down a backlog rather than clearing it.** 150 tickers
  per venue per cycle against tens of thousands, most of them crypto strike ladders
  that the monotonicity scanner handles and the basket scanner does not want.
