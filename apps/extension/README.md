# PMVL — true entry cost

A Chrome extension that shows what a Kalshi or Polymarket order **actually costs
per contract**, on the page where the order is placed.

```
Entry cost · YES
SIZE          COSTS   OVER QUOTE   BREAK-EVEN
5             17.0¢      +143%        17.0%
10  [yours]   12.0¢       +72%        12.0%
1000           7.07¢      +1.0%        7.1%

At 10 you pay 12.0¢ each. 1000 costs 7.07¢ each — $0.49 less on an order this size.
Book read 8s ago.
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

It reads the size in the order form and prices **that** size, marked as yours,
alongside a few placeable sizes around it. When a different size would cost less
it says so in dollars on an order that size — the one actionable sentence it
produces, and arithmetic rather than a forecast. When the book is flat and there
is nothing to save, it says nothing: rounding noise dressed up as advice teaches
people to ignore the panel.

The header names the side it priced, because a trader on NO reading YES numbers
is not looking at a slightly different figure, they are looking at the other
contract. The footer states how old the book is, since freshness is the whole
reason this is not the website.

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
in light and dark schemes.

**Verified on the live venues**, by loading both sites and running the shipped
logic in the page (10 August 2026).

*Kalshi*, on `KXMLBGAME-26AUG101907BOSTOR-BOS` quoted 62¢: discovery picked the
Boston side, `$50` was read as **dollars** and converted to 80 contracts, and the
panel rendered at 320×283 fully on screen without overlapping the order form,
reading `1 → 64.0¢ (+3.2%)`, `10 → 63.7¢`, `80 [yours] → 63.6¢`. Clicking the
NO toggle flipped `aria-pressed` and the detector followed it; NO prices from a
different side of the same payload — top ask 39¢, `1 → 41.00¢` — where before
the fix a NO trader was shown 64¢, the other contract entirely.

*Kalshi, a multi-outcome board* (`kxbtcd-26aug1117`, 80 strikes in one event):
the page shows no order ticket until a strike is picked, so nothing names the
outcome — and the overlay now renders nothing. Before, the markup scan supplied
the first ticker in the HTML, a $64,000 strike nobody had selected, priced as
confidently as if it were theirs. Knowing the event and not the outcome is
exactly the state that must produce silence.

*Polymarket*, on the localised `/zh/event/…` path: the slug parsed, Gamma
returned both tokens, and the CLOB book gave 126 ask levels with a minimum order
of 5 — `5 → 14.10¢ each`, `1000 → 4.15¢`, the bridge allowance amortised.

That pass found three bugs that every unit test had been passing over, because
the tests encoded the same wrong assumptions as the code:

1. **Polymarket served `/zh/event/...`** and the slug pattern required `event`
   directly after the hostname — so on any localised page the overlay was absent.
2. **Kalshi puts no ticker in the URL and none in the rendered text.** Discovery
   scanned `innerText` and found nothing, on every page.
3. **Both order forms are denominated in dollars, not contracts.** The reader
   looked for a contract count and would have priced Kalshi's `50` as fifty
   contracts when it means fifty dollars — 80 contracts at 62¢, a 60% error
   printed beside a live order ticket.

**Still not verified:** the packaged extension itself. The logic above ran via
the page console, not through `manifest.json`, a content script and the
background service worker. Loading it unpacked is what exercises the message
channel, the host permissions and the injection timing.
