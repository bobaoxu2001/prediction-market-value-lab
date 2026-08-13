# Chrome Web Store submission packet

This file is source copy and reviewer context, not evidence that the listing has
been created, reviewed, or published.

## Name

PMVL — true entry cost

## Short description

See estimated entry cost and lower-cost placeable sizes beside Kalshi and
Polymarket order tickets.

## Single purpose

PMVL reads the public order book for the Kalshi or Polymarket contract already
open in the browser and calculates the estimated entry cost for the visible side
and amount. It displays that arithmetic beside the order ticket. It never edits
or submits an order and does not provide a forecast or trade recommendation.

## Listing description

The quoted contract price is not always what an order costs after depth, venue
fees, and disclosed transfer and capital-cost assumptions. PMVL keeps that
calculation beside the order ticket.

On a supported single-contract page, PMVL shows:

- estimated cost per contract at the visible order amount;
- break-even probability implied by that estimated cost;
- premium over the displayed quote;
- quote freshness; and
- a lower-cost placeable size when the arithmetic supports one.

PMVL is read-only. It does not request a wallet or venue login, type into the
ticket, change an amount, place an order, or send the ticket amount and browsing
history to a PMVL server. Public books can be stale or incomplete, page markup
can change, and the result is an estimate rather than a settled execution price.

## Permission justification

- `kalshi.com` and `polymarket.com` page matches: identify the selected contract,
  side, and visible order amount, then render the local cost panel.
- `api.elections.kalshi.com`, `gamma-api.polymarket.com`, and
  `clob.polymarket.com`: read public contract metadata and order-book depth.
- No general manifest permissions are requested. Runtime messaging, the toolbar
  action, and opening the packaged onboarding tab use base extension APIs.

## Data-use disclosure

- Account credentials and wallet data: not accessed.
- Order placement or modification: not performed.
- Ticket amount and browsing history: processed locally and not sent to PMVL.
- Analytics, advertising, sale, or profiling: not performed by this package.
- Remote code: not used; executable code is bundled in the submitted archive.

## Reviewer test path

1. Install the package; confirm the packaged onboarding guide opens.
2. Open a supported single-contract Kalshi or Polymarket page.
3. Select YES or NO and enter an amount in the venue ticket.
4. Confirm the PMVL panel appears beside the ticket with an observed timestamp.
5. Confirm changing the amount redraws the panel but never changes or submits the
   venue form.
6. Open a multi-outcome board without selecting a contract; confirm PMVL remains
   silent instead of guessing an outcome.

## Owner actions before submission

- Upload the packaged `icons/icon128.png` store icon and capture the required
  listing screenshots from the verified live overlay. Set the support URL and
  the published PMVL privacy-policy URL in the owner-controlled store fields.
- Generate the current deterministic archive with `npm run package` and verify
  its digest.
- Complete the store dashboard's data-use answers from the behavior above.
- After the listing is published, set `NEXT_PUBLIC_CHROME_EXTENSION_ID` on the
  website deployment to the exact 32-character ID assigned by Chrome. The site
  constructs the listing URL from that pinned ID, so it cannot be pointed at an
  unrelated extension. Until then it intentionally retains the developer-mode
  ZIP path and makes no store-review claim.
