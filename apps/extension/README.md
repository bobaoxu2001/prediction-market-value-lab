# PMVL — true entry cost

A Chrome extension that shows what a Kalshi or Polymarket order **actually costs
per contract**, on the page where the order is placed.

```
Entry cost, per contract
SIZE   COSTS   OVER QUOTE   BREAK-EVEN
1      56.0¢      +3.7%        56.0%
50     55.7¢      +3.2%        55.7%
Cheapest placeable size: 50 at 55.7¢ each.
```

## Why an extension and not the website

The hosted site answers the same question, and answers it about a **frozen
snapshot** — the freshest quote in the published artefact was eleven days old
when this was written. "What will this cost me" is a question about the book
right now, and an eleven-day-old book cannot answer it.

So the extension reads the venue's own public, unauthenticated endpoints, the
same ones the research pipeline uses, and computes the cost locally. It needs no
account, no server of ours, and no pipeline to be running.

It also puts the number where the decision is made. The site requires a reader to
remember it exists and go there; the size field on the order ticket is where the
size is actually chosen.

## What it shows, and what it refuses to

It shows cost per contract at placeable sizes, the break-even probability that
follows from it, and which size is cheapest.

It shows **no probability estimate, no edge and no recommendation.** The
retrodiction harness in this repository scored the project's models against the
market's own price across 22 segments and found none that beat it; the one
segment that cleared significance did so in the wrong direction. So the overlay
says what the trade costs and has no opinion on whether to make it.

It never types into a field, changes an order size, or submits anything.

## The second implementation problem

The fee maths exists twice: in `pmvl_markets.pricing` (Python) and in `src/cost.ts`.
Two implementations are two numbers that can disagree, and here they would
disagree in front of somebody placing an order.

`tests/conformance.test.ts` replays ~1,000 cases generated from the **real**
Python functions and fails on any difference. Regenerate the fixtures with:

```bash
python scripts/generate_cost_conformance_fixtures.py
```

A diff in `fixtures/cost-conformance.json` is a change in what the product says an
order costs, and should be read as carefully as a change to the code.

`src/decimal.ts` is exact decimal arithmetic on BigInt rather than floats. This is
not fastidiousness: `0.07 * 3` in JavaScript is `0.21000000000000002`, which sits
on the wrong side of a cent boundary that `ceil_cent` rounds up, so a float port
would disagree with Python at exactly the boundaries the product is about.

## Build and load

```bash
npm install && npm run build
```

Then in Chrome: `chrome://extensions` → enable Developer mode → **Load unpacked**
→ select this directory.

```bash
npm test        # conformance + venue adapters + the render harness
npm run typecheck
```

`npm test` also writes `out/overlay-preview.html`, which renders the panel from
the recorded venue payloads so the styling can be inspected without visiting a
venue.

## Verified, and not verified

**Verified here:** the cost maths against the Python, case for case; the venue
adapters against payloads captured live from both venues; the panel's rendering
and legibility in light and dark schemes.

**Not verified here:** injection into the live venue pages. kalshi.com
rate-limits automated access (HTTP 429 from this environment), so the content
script has never been observed running on a real Kalshi page. Contract discovery
is therefore written to fail closed — a ticker that does not resolve against the
venue's own API renders nothing at all — but *whether the overlay appears, and
where it sits relative to the order ticket*, needs a human to load the unpacked
extension and look.

That is the first thing to check before this goes anywhere near a user.
