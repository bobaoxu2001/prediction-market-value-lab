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

## Gap 2 — event linkage (not needed after all)

84% of open Kalshi markets (26,965) carry an event identifier resolving to no event
row, because event fetching is capped at `min(600, market_limit)` while market
fetching is not. This was assumed to be the blocker for sports matching.

It is not. **Kalshi restates the entire fixture in its own rules prose**, so nothing
about the matchup requires an event row:

> If San Diego wins the **San Diego vs San Francisco professional baseball game
> originally scheduled for Sep 11, 2026 at 10:15 PM EDT**, then the market resolves
> to Yes.

The gap is still real and still starves the multi-outcome scanner, which needs
`Event.outcome_count` to establish that a basket is complete. It is no longer on the
path to cross-venue arbitrage.

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

## Known and unfixed

* **Yearless Polymarket dates.** 97 of 99 Polymarket fixtures state no year, and
  `extract_dates` falls back to the current year. Because Kalshi always states its
  year, a wrong inference produces *no* match rather than a wrong one — silent recall
  loss around the new year, and a stored match that is not reproducible from its row.
* **The arbitrage table renders `YES`/`NO` for Polymarket legs** whose outcomes are
  two team names. The engine's orientation is correct and `ArbLeg.token_id` carries
  the disambiguator, but the web table does not display it, so the same market reads
  `NO` in one row and `YES` in another with nothing on screen to say which team that
  is. Polarity is inverted on about half of fixture pairs, so this is not an edge
  case.
* **The fixture proposer bypasses the 3-day resolution-gap and category gates.**
  Necessarily so — every genuine live pair has a 6d23h gap, because Kalshi holds its
  market open past the game — but that leaves the contest date without independent
  corroboration.

## What remains

* Kalshi event linkage (Gap 2) for the multi-outcome scanner.
* Fixture extraction covers head-to-head moneylines only. Totals and spreads are
  deliberately excluded; matching them needs the line itself compared, not just the
  contest.
* `source_unknown` is the last demotion standing on the live sports pairs: Polymarket's
  rules for a game do not name a settlement authority the parser recognises. Resolving
  it would move these from `PROBABLE_MATCH` to a verified tier — still not to
  `EXECUTABLE`, because the cancellation divergence above is real.
