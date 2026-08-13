# PMVL hit-product audit — 2026-08-13

## Scope and target user

Reviewed the public homepage, hosted entry-cost index, individual cost result,
research navigation/data modes, mobile layout, and the packaged Chrome extension.
The primary user is a prediction-market participant who is already considering a
specific YES/NO order and needs to know what that size can cost before submitting
it. The primary job is therefore not market discovery or a new forecast: it is
turning the order ticket into a transparent entry-cost decision.

The product remains read-only research software. It does not hold funds, store
wallet keys, change ticket fields, submit orders, or recommend a trade.

## Audited flow

1. **Understand the offer from the homepage.** Before: the homepage led with a
   broad research proposition and routed the strongest action into a stale hosted
   snapshot. After: the first action is the live, read-only overlay; the snapshot
   calculator and wider research record remain secondary proof surfaces.
2. **Find the contract.** Before: the hosted cost surface opened with a long table
   ranked by premium and no direct lookup. After: title/subtitle/venue-ID search is
   applied before the bounded volume scan and preserves size, filters, and data mode.
3. **Price the exact side and size.** Before: the detail was strong but size presets
   overflowed on mobile, Polymarket showed a Kalshi-specific fee-rounding note, and
   the currently selected result had no share control. After: the size rail scrolls
   within the viewport, venue-specific language is correct, and an exact URL-backed
   result can use native sharing or clipboard fallback.
4. **Use the live overlay.** Before: the working extension existed only inside the
   repository, its documented production build was broken, and its scenario footer
   omitted the annual capital-cost assumption. After: its bundle builds, its panel
   states contributing bridge/gas and capital-rate assumptions, its unused general
   browser permission is removed, and a deterministic ZIP is downloadable from a
   dedicated beta page with manual-install and privacy boundaries.
5. **Trust the data state.** Before: a locally writable pipeline with weeks-old data
   could still be labelled “Live pipeline,” and SQLite compared Money text
   lexicographically in DB filters/sorts. After: stale live-mode data gets an explicit
   historical-data block, and Money comparisons/order are numeric while Decimal
   round-trips remain exact.
6. **Move through demo/research pages.** Before: content links could drop
   `mode=demo` and silently switch datasets. After: a shared validated URL helper
   preserves demo mode across the research navigation and deep links.

## Health after implementation

| Flow | Health | Evidence |
| --- | --- | --- |
| Homepage to live overlay | Healthy | Primary CTA reaches `/extension`; desktop viewport has no horizontal overflow. |
| Find a contract | Healthy | “Fed decrease” returns two matching contracts; search state and filters persist. |
| Exact result and sharing | Healthy | Server-computed values remain URL-backed; share/copy/manual fallback tests pass. |
| Mobile sizing | Healthy | 390px document width stays 390px; preset rail scrolls internally. |
| Extension distribution | Beta | Deterministic ZIP builds and validates; manual Chrome installation remains required. |
| Data trust | Guarded | Numeric SQLite query parity verified on the current snapshot; stale data is visibly blocked from appearing current. |
| Accessibility | Partially verified | Semantic labels, status announcements, keyboard-native links/forms, alt text, and viewport containment were checked. No full screen-reader or external contrast-lab pass was performed. |

## Visual evidence

### Before

- `01-home-before.png`
- `02-cost-index-before.png`
- `03-cost-detail-before.png`
- `04-cost-detail-mobile-before.png`
- `05-extension-preview-before.png`

### After

- `06-home-after.png`
- `07-extension-page-after.png`
- `08-extension-preview-after.png`
- `09-cost-search-after.png`
- `10-cost-detail-mobile-after.png`

## Verification record

- Python: 1,233 tests collected; full run passed with one existing skip.
- Web: lint completed with zero errors and 14 pre-existing `no-explicit-any`
  warnings; typecheck passed; 213 tests passed; production Next.js build passed.
- Extension: 86 tests passed; typecheck and production bundle passed; ZIP integrity
  passed; two successive package runs produced the same SHA-256
  `4fc59e7b8bd9fc8f7a98bcbcfeffad9aad9c29f1f67193f6dedf1d6e718be665`.
- Current SQLite snapshot: DB-side `volume_24h >= 500` and descending order matched
  an independent Decimal/Python calculation exactly for all 1,420 matching open,
  accepting, live-provenance markets.
- Browser: homepage, beta page, searched cost surface, detail/share route, download
  response, and 390px mobile containment were exercised against the running app.

## Remaining launch work

The beta has not been submitted to or reviewed by the Chrome Web Store. Store icons,
store policy/privacy artifacts, automatic updates, and a current live-venue smoke pass
should be completed before calling it a generally distributed extension. The hosted
research data used during this audit was stale (newest quote 2026-07-29), so no current
market conclusion is made from it.
