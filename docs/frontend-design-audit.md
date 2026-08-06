# Frontend design audit

Audit of `apps/web` before any redesign work, per the brief's instruction not to
rewrite first and inspect afterwards.

**Baseline commit:** `52dac2a8deb4c6a5ddf6e094273b82946b43c446` (PR #7 merge)
**Branch:** `feat/frontend-taste-redesign`
**Audited:** 2026-07-30

## Skill availability — stated honestly

The brief asks for "the installed Taste / design-taste / frontend-design skill".
**No such skill is installed in this environment.** Searched:

- `~/.claude/skills/` — does not exist
- `.claude/skills/` in-repo — does not exist
- `~/.claude/plugins/marketplaces/claude-plugins-official/` — 16 external plugins
  (asana, context7, discord, firebase, github, gitlab, greptile, laravel-boost,
  linear, playwright, serena, slack, supabase, telegram); none is a design skill
- Every `SKILL.md` under `~/.claude` — one hit, `scheduled-tasks/daily-portfolio-report`

Per the brief's own constraint ("do not install an arbitrary third-party skill
without inspecting its source and scope"), nothing was installed. The work uses
the design skills that *are* available in this environment:

- **`artifact-design`** — design fundamentals: typography pairing, token-level
  theming, layout/spacing discipline, and an explicit list of AI-design tells to
  avoid. This is the closest available match to the brief's list (visual
  hierarchy, typography, spacing, responsive layout, design-system consistency).
- **`dataviz`** — reserved for the chart work (`PriceChart`, `CalibrationChart`).

**Update, 2026-08-06.** `frontend-design` now exists, in Anthropic's own
marketplace (`claude-plugins-official`), and has been enabled. Its source was
read before enabling: a single `SKILL.md`, no scripts, no network access.

It is applied here with one deliberate departure. Its default direction is to
"commit to a **BOLD** aesthetic direction" and reach for asymmetry, grain
overlays, dramatic shadows and distinctive webfonts. This product's value
proposition is epistemic restraint — most of its pages exist to say what is *not*
established — and the audit below already lists glassmorphism, emoji markers and
uniform rounded cards as AI-design tells to avoid. System fonts are a deliberate
build-time choice, recorded in `globals.css`.

The skill itself sanctions the branch taken: *"Bold maximalism and refined
minimalism both work — the key is intentionality, not intensity"*, and
*"Minimalist or refined designs need restraint, precision, and careful attention
to spacing, typography, and subtle details."* That is the mode used, on the pages
this audit had never covered.

`artifact-design`'s own precedence rule is *"honor what's already there — look for
an existing design system first; everything else fills gaps and never overrides."*
That rule shaped this audit: the finding below is that the existing system's
**data semantics are excellent and must not be touched**, while its **visual token
layer barely exists**. The redesign fills the second gap only.

## What must not change

This codebase has already solved the hard, project-specific problems, several of
them visibly the result of past bugs. All of it is load-bearing and is preserved:

- `relativeToSnapshot` / `ageRelativeToSnapshot` anchor every relative time to the
  snapshot instant, not `Date.now()`. The deleted `relativeTime` / `ageLabel`
  helpers are commented as deliberately removed because the bug returned twice.
- `displayTitle` strips venue markdown rather than rendering it — third-party
  strings are never interpreted.
- `VenueAvailability` never infers broker availability from an exchange listing.
- `toneFor` is three-state: a real `0` and an unknown are not coloured as losses.
- `HelpDot` uses `<details>`, not hover — hover does not exist on touch.
- `DemoBanner`, `SnapshotBanner`, and the footer disclaimer.
- `font-feature-settings: "tnum"` globally, `.num` for monospace columns.

## Cross-cutting findings

| # | Finding | Severity |
|---|---|---|
| G1 | **No design-token layer.** Colour is expressed as repeated Tailwind literals; `text-neutral-500 dark:text-neutral-400` appears in ~40 places across 12 files. There are no tokens for surface layers, border hierarchy, or the stale / unverified / live / demo states the brief asks for. | high |
| G2 | **No type scale and no typeface pairing.** Body is the browser default sans. Interior `h1` is `text-xl` (20px); every `h2` is `text-sm font-semibold` — the same size as body copy, so section boundaries carry on weight alone. Does not meet the "editorial quality: high" target. | high |
| G3 | **`prefers-reduced-motion` is never respected.** `transition` is applied to nav pills, buttons and rows with no guard. | high (a11y) |
| G4 | **Focus styles exist on exactly one component.** `hero.tsx` defines `focus-visible`; nav links, table links, all six form controls on `/markets`, and pagination do not. Keyboard users get the UA default or nothing. | high (a11y) |
| G5 | **`backdrop-blur` on the sticky header** — glassmorphism, explicitly on the brief's avoid list. | medium |
| G6 | **Emoji as section markers** in `hero.tsx` (`📐`, `🔍`, `📄`). Decorative, not informational; also a listed AI-design tell. | medium |
| G7 | **Single-level border hierarchy.** Card edges, table rules and section dividers are all `neutral-200 / neutral-800`. Nothing distinguishes structure from enclosure. | medium |
| G8 | **`.card` is `rounded-lg` and used ~30 times uniformly** — "a dashboard made entirely of rounded cards" is a named anti-goal. | medium |
| G9 | **`th` / `td` are styled with bare element selectors** in `@layer components`. Global, unavoidable, and impossible to vary per table. `whitespace-nowrap` on every `td` is what forces the horizontal scroll on `/markets`. | medium |
| G10 | **Truncation with no recovery.** Contract titles use `truncate` + `max-w-md` with no `title` attribute and no expand. The brief forbids exactly this. | medium |
| G11 | **Numeric columns are left-aligned.** `.num` sets `font-mono tabular-nums` but no alignment; `text-right` is applied ad-hoc in only 3 of ~60 numeric cells, so digit columns do not line up despite the tabular-numeral work. | medium |
| G12 | **No sticky table header or sticky first column** on any of the wide tables. | medium |

## Per-page findings

### `/` — Today

- **Purpose:** currently *product introduction*; the brief wants *research briefing*.
- **Strengths:** `FilterFunnel` is the single best component in the app — it
  answers "why is nothing recommended?" with counts instead of an apology, and
  its comment correctly frames recommending nothing as the product.
- **Hierarchy problem:** the entire first viewport is marketing — headline, two
  title-case CTAs ("Explore Demo Opportunities", "View Backtest Results"), four
  text links, three emoji cards. None of the brief's six first-viewport questions
  (what changed / what is actionable / what is not / why nothing / how fresh /
  what next) is answered above the fold.
- **`FilterFunnel` renders only when `rows.length === 0`** — the no-trade
  explanation disappears precisely when a reader would want to calibrate it
  against the opportunities shown.
- **Density:** 10 opportunity cards each carrying a 12-cell metric grid. Low
  density per screen, very long page. Target is 8/10 density.
- **Missing sections** from the brief: research snapshot, market coverage, model
  and data health, latest pipeline status.

### `/markets` — Market browser

- **Strengths:** genuinely good research table. `quote_source` distinguishes order
  book from venue-summary fallback and flags `summary stale` — real epistemic care.
- **Interaction problem:** sorting is a `<select>` + Apply round-trip. Column
  headers are inert and there is no indication which column is sorted.
- **Responsive problem:** 11 nowrap columns overflow on desktop with no sticky
  Market column, so scrolling right loses the row identity entirely.
- **Hierarchy problem:** category, resolution and quote-age all render at
  `text-neutral-500`, the same tone as metadata, so identifier and measurement sit
  at one level.
- Filter form is a 6-control card, visually heavier than the data it filters.

### `/market/630` — Market detail

- **Sequence problem:** order is title → venue availability → quotes → order book
  + model. Market *price* is presented before the *resolution rules* that define
  what is being priced, inverting the brief's analytical sequence.
- **The central labelling requirement is unmet:** there is no explicit
  market-implied / independent-estimate / decision-adjusted triad. "Quotes" and
  "Model estimate" are separate sections, and the reader must infer that one is
  the market's view and the other is independent.
- Eight-across metric grid at `lg` (`lg:grid-cols-8`) puts eight monospace values
  in a row — too tight for reliable scanning.

### `/arbitrage` — Arbitrage scan

- **Most significant brief violation.** Actionable and Diagnostics render with
  **identical** card, chip, grid and table treatment. The only differences are one
  paragraph of copy and the `ArbLabelChip` value. The brief states plainly: *"Do
  not use the same card or color treatment for both."* A diagnostics finding —
  stale, incompatible, not executable — currently looks exactly like a validated
  executable opportunity.
- The view toggle reuses the same pill styling as the home page's horizon filter,
  so switching between them reads as filtering, not as changing epistemic status.
- **Strength:** the "what each label means" legend with live counts, and the
  `rule_compatibility` chip, are the right instincts — they just need to be
  visually subordinate on Actionable and primary on Diagnostics.

### `/backtest?mode=demo` — Backtest

- **Trust problem, and the worst inversion in the app:** the demo caveat — *"this
  demo forecaster is deliberately imperfect… several strategies lose money"* — is
  rendered in a **neutral grey box** (`border-neutral-200 bg-neutral-50`), while
  the ROI figures beside it are coloured green. The most important warning on the
  page is its quietest element, and `DemoBanner`'s loud `warn` treatment is not
  used for it.
- Sample-size limitation appears only as `sub` text in one card and one trailing
  clause. The brief requires it prominently.
- 11-column strategy table colours ROI green/red per row, against the brief's
  "green/red everywhere" and "do not visually celebrate demo returns".
- **Strength:** `Verdict` asks the three right questions before the table, and
  `vs market` correctly separates *added information* from *made money*.

### `/system` — System

- Closest page to its target already; it is an operational console in intent.
- **Priority problem:** nine sections all rendered as identical `card p-4`.
  Deployment parity (critical) is indistinguishable from data sources (reference).
- **Table semantics:** the row-counts table is `<tbody>`-only with no `<thead>`.
- Job-health `error` column is `truncate` — an error message you cannot read.
- **Missing** from the brief's list: scheduler enable/publish state and candidate
  state. `scheduler_status` is surfaced; publication state is not.

## Recommended change order

Follows the brief's Phase B10, narrowed by the findings above:

1. **Token layer + typography + a11y baseline** (G1–G4, G7, G11) — one commit,
   touches `globals.css` and `tailwind.config.ts`, unblocks everything else.
2. **Global shell** — de-glass the header (G5), real focus rings, skip link.
3. **`/arbitrage`** — highest-severity single violation: separate the Actionable
   and Diagnostics treatments.
4. **`/backtest`** — promote the demo caveat to `warn`, de-emphasise demo ROI.
5. **`/` home** — research briefing above the marketing; always show the funnel.
6. **`/markets`** — sticky identity column, sortable headers, title recovery.
7. **`/market/[id]`** — reorder to the analytical sequence; add the labelling triad.
8. **`/system`** — section priority tiers, table semantics.
9. Responsive + reduced-motion + contrast pass at 1440/1280/1024/768/390.

## Constraints honoured

- No backend change. No new API field is required by any item above; where one
  would help (`/system` publication state) it is documented here rather than added.
- No pipeline workflow change, no snapshot mutation.
- All existing API calls, route shapes and query parameters preserved.

## Post-publication responsive closure

Re-audited after the first compressed Snapshot was published and this branch was
rebased onto `main` (2026-07-31 UTC). The remaining `/markets` blocker is closed
without changing the API, pipeline, or Snapshot.

- Below Tailwind's `md` breakpoint, the eleven-column table is replaced by one
  divided, compact list. Each row keeps contract identity, venue, status, YES ask,
  and resolution visible; native `details` / `summary` disclosures carry spread,
  depth, model-coverage routing, quote age/status, and venue coverage.
- Model coverage is not invented from unrelated fields. The market-index API does
  not expose a per-row coverage value, so the disclosure says so and links to the
  existing contract analysis where prediction coverage is source-backed.
- At 768px, the semantic sortable table remains, but only Market, Venue, YES ask,
  and Resolves are shown. Columns reveal progressively at `lg` and `xl`; the Market
  identity remains sticky and numeric cells remain right-aligned.
- Root-level horizontal clipping was removed. Wide desktop content, where needed,
  is reachable inside `.table-wrap` rather than hidden from the page.
- A stale venue summary remains visible on mobile when the captured order book
  disagrees with it.

Measured in Chrome against the production-mode branch build and the published
compressed Snapshot:

| Viewport | Before: root overflow | After: root overflow | Active representation |
|---|---:|---:|---|
| 375 x 812 | not previously recorded | 0px | mobile list; 50 disclosures |
| 390 x 844 | 397px | 0px | mobile list; desktop table hidden |
| 768 x 1024 | 131px | 0px | four-column table; mobile list hidden |
| 1440 x 900 | 0px | 0px | all eleven columns; internal table scroll only |

The first mobile disclosure opened with the Enter key. At 768px the measured
Market cell was `position: sticky`, the numeric cell was right-aligned, and the
table wrapper fit exactly (`734px` client and scroll width). At 1440px all eleven
headers were visible while the wrapper contained its own overflow (`1246px` client,
`1345px` scroll width), with no document-level overflow.

Accepted screenshots:

- [`markets-mobile-dark.png`](screenshots/pr-8/markets-mobile-dark.png) — 390 x 844
- [`markets-768-light.png`](screenshots/pr-8/markets-768-light.png) — 768 x 1024

---

## Second pass — the surfaces added after the first audit

`/cost`, `/cost/[id]`, `/watchlist` and the homepage sector band were built after
this document was written and had never been through it. They reused the token
layer correctly, and reproduced three of the defects the first pass had fixed.

| # | Finding | Severity | Fixed |
|---|---|---|---|
| H1 | **Heading tier skipped.** Every `<h2>` used `t-sub-title`, the *third* tier, and one used `t-label` — 10.5px uppercase, the fine-print style — for a section heading. This is G2 again: section boundaries carrying on weight alone. The audited `/system` uses `t-section-title` for every `<h2>`. | high | yes |
| H2 | **No progressive disclosure.** The 7-column cost table and 8-column watchlist rendered every column at every width. At 375px the wrapper showed 343px of an 817px table, so a reader scrolled for every number. `/markets` had solved this with `hidden lg:table-cell` ladders. | high | yes |
| H3 | **No sticky identity column.** Scrolling right lost the contract, exactly as G12 described. | medium | yes |
| H4 | **`colSpan` assumed a fixed column count.** The watchlist's loading and error rows spanned four cells while the Quoted column is hidden below `md`, so on a phone the row claimed one cell more than its header and pushed the last column out of alignment. HTML has no responsive `colSpan`; the placeholder is now one cell per column carrying the same breakpoints. | medium | yes |

`.cell-title` also gained a mobile step (10rem, 13rem at `sm`, 20rem at `lg`),
matching `.col-title`'s existing ladder — a 13rem identity column at 375px left
too little beside it for even one measurement.

Measured after the change, with the local API serving the published Snapshot:

| Viewport | Page overflow | Cost columns shown | Identity column | Row alignment |
|---|---:|---:|---|---|
| 375 x 812 | 0px | 4 of 7 | scrolls (by design below `sm`) | 5 header / 5 row |
| 768 x 1024 | 0px | 5 of 7, fits without scrolling | `position: sticky` | — |
| 1440 x 900 | 0px | 7 of 7, fits without scrolling | `position: sticky` | 8 header / 8 row |

Not changed, having been measured rather than assumed: the contract column is
639px against a 554px widest title at 1440px, which is ordinary table space
distribution and not the dead space it appears to be at a glance.
