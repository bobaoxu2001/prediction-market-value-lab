import type { Metadata } from "next";
import Link from "next/link";
import { unstable_cache } from "next/cache";
import { redirect } from "next/navigation";

import {
  Faq,
  FeatureRow,
  ProductShot,
  ProofFigure,
  Section,
  SectionHeading,
} from "@/components/marketing";
import { PricingPlans } from "@/components/pricing";
import { SectorPremium } from "@/components/sector-premium";
import {
  apiGet,
  qs,
  type CostAtSize,
  type CostByCategory,
  type CostDetail,
  type CostIndexRow,
} from "@/lib/api";
import { ladderStrip } from "@/lib/cost-ladder";
import { cents, displayTitle, pct, utcTime } from "@/lib/format";
import { getResearchProof, PROOF_HORIZON, type ResearchProof } from "@/lib/proof";
import { getCurrentEntitlement } from "@/lib/billing/entitlement";
import { isAuthConfigured } from "@/lib/auth-server";
import { absoluteUrl } from "@/lib/site";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Know what a prediction-market order really costs",
  description:
    "PMVL's read-only browser overlay estimates entry cost and break-even probability from public Kalshi and Polymarket books, at the order size on the ticket.",
  alternates: { canonical: absoluteUrl("/") },
};

/**
 * Order size the sector comparison is priced at.
 *
 * A round, plausible retail order. The ranking between categories is stable
 * across sizes, but the absolute figures are not - fee rounding punishes small
 * orders hardest - so the size is stated everywhere the numbers appear rather
 * than left for a reader to assume.
 */
const SECTOR_SIZE = "100";

/**
 * How long the shared, snapshot-derived half of this page is cached.
 *
 * Generous on purpose. Every figure here comes from the published artefact,
 * which is **committed to this repository** — so new data means a new commit and
 * a new deployment, and a deployment invalidates this cache wholesale. The
 * values are therefore constant for a deployment's entire lifetime, and the
 * window is a backstop rather than the thing keeping them correct.
 */
const SNAPSHOT_CACHE_SECONDS = 3600;

interface HomeSnapshotData {
  proof: ResearchProof;
  sectors: CostByCategory[] | null;
  heroExample: HeroExample | null;
}

/** One real contract's ladder, shown in the hero as a worked example. */
interface HeroExample {
  marketId: number;
  title: string;
  platform: string;
  quoted: string;
  rungs: CostAtSize[];
}

/**
 * A live contract whose ladder demonstrates the hero's claim.
 *
 * The hero asserted that a 1¢ Kalshi order pays a 1¢ fee and then asked the
 * reader to take it on trust for four paragraphs. The assertion is true and it
 * is abstract, and the surface it describes is the one thing here that always
 * has an answer — so the first screen should show the answer, on a contract
 * that exists, rather than describe the shape of one.
 *
 * Priced at one contract because the index sorts by premium and the fee-rounding
 * effect is largest at a single lot, which surfaces exactly the contract that
 * makes the point. Depth must be known: a ladder built from a venue summary
 * quote has no depth impact in it, so its right-hand end would be flat and the
 * U-shape — the actual finding — would not appear.
 */
async function readHeroExample(): Promise<HeroExample | null> {
  // The full page of candidates, not the first handful. Order books are captured
  // for only part of the snapshot, and the premium ranking is dominated by
  // cheap contracts priced from a venue summary quote - on the live snapshot the
  // top 25 Kalshi rows were *all* summary-quoted, so a short scan found nothing
  // with depth and the hero silently fell back to prose.
  const index = await apiGet<CostIndexRow[]>(
    `/cost${qs({ limit: "200", platform: "kalshi", size: "1" })}`,
  );
  const candidates = (index?.data ?? []).filter(
    (row) => row.depth_known && row.measured_premium_ratio !== null,
  );

  // A few attempts, because a candidate can still fail to produce a usable
  // ladder - every rung below the venue minimum, or a book too thin to fill the
  // larger sizes. Bounded so the landing page never waits on a long scan.
  for (const candidate of candidates.slice(0, 4)) {
    const detail = await apiGet<CostDetail>(`/cost/${candidate.market.id}`);
    const data = detail?.data;
    if (!data?.priced) continue;

    const rungs = ladderStrip(data.ladder ?? []);
    // Two rungs is the minimum that shows a spread; one is just a price.
    if (rungs.length < 2) continue;

    return {
      marketId: data.market.id,
      title: data.market.title,
      platform: data.market.platform,
      quoted: rungs[0].nominal_price,
      rungs,
    };
  }
  return null;
}

/**
 * Everything on this page that is identical for every visitor.
 *
 * Four API calls, all reading the frozen artefact: the proof band's three and
 * the sector comparison. On production they measured 5–16s each, and the page
 * waited on the slowest, so a first-time reader met a blank tab for that long.
 *
 * Deliberately not the whole route. `getCurrentEntitlement` reads the visitor's
 * session, so this page must stay dynamic — caching the route would serve one
 * visitor's signed-in state to everyone the moment accounts are switched on.
 * Only the shared half is cached.
 */
/**
 * Bumped whenever {@link HomeSnapshotData} gains or changes a field.
 *
 * The key has to describe the *shape* it caches, not just the inputs. Adding
 * `heroExample` without touching the key meant a running deployment kept serving
 * entries written before the field existed, so the value read back was
 * `undefined` and the hero rendered its fallback — for an hour, with no error
 * anywhere, on a page that looked fine.
 */
const HOME_SNAPSHOT_SHAPE = "v3-hero-example";

const readHomeSnapshotData = unstable_cache(
  readHomeSnapshotDataUncached,
  ["home-snapshot-data", HOME_SNAPSHOT_SHAPE, SECTOR_SIZE],
  { revalidate: SNAPSHOT_CACHE_SECONDS, tags: ["home-snapshot-data"] },
);

/** The same reads, going straight to the API. */
async function readHomeSnapshotDataUncached(): Promise<HomeSnapshotData> {
  const [proof, sectors, heroExample] = await Promise.all([
    getResearchProof(),
    apiGet<CostByCategory[]>(`/cost/by-category${qs({ size: SECTOR_SIZE })}`),
    // The hero degrades to prose if this fails; it must never fail the page.
    readHeroExample().catch(() => null),
  ]);
  return { proof, sectors: sectors?.data ?? null, heroExample };
}

/**
 * The cached read, which falls back to an uncached one in two cases.
 *
 * **A failure is never cached.** `getResearchProof` answers `available: false`
 * when the API cannot be reached, and the page renders a plain "figures
 * unavailable" state for it. Caching that would pin the outage in place for an
 * hour after the API came back — turning a thirty-second blip into a long one,
 * on the page most likely to be a reader's first impression.
 *
 * **A missing cache runtime is not an error.** `unstable_cache` throws
 * "incrementalCache missing" when called outside Next's request context, which
 * is exactly what happens when this page is rendered directly in a test. The
 * cache is an optimisation and never a correctness requirement, so anything that
 * stops it working means doing the work instead — not failing the page.
 */
async function getHomeSnapshotData(): Promise<HomeSnapshotData> {
  try {
    const cached = await readHomeSnapshotData();
    if (cached.proof.available) return cached;
  } catch {
    // Fall through and read directly.
  }
  return readHomeSnapshotDataUncached();
}

/** Query keys the research briefing used to accept when it lived at `/`. */
const RESEARCH_QUERY_KEYS = [
  "horizon",
  "mode",
  "platform",
  "side",
  "min_edge",
  "min_confidence",
  "min_liquidity",
  "include_inactive",
] as const;

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;

  // Deep-link preservation. The research briefing lived at `/` until this
  // change, and links to it in the wild carry a horizon or a mode. Landing one
  // of those on the marketing page would silently drop the reader's filters, so
  // a request that names a briefing parameter is forwarded to `/app` with the
  // whole query intact. A bare `/` is the marketing homepage, as intended.
  const search = new URLSearchParams();
  for (const key of RESEARCH_QUERY_KEYS) {
    const value = params[key];
    if (typeof value === "string" && value !== "") search.set(key, value);
  }
  if ([...search.keys()].length > 0) redirect(`/app?${search.toString()}`);

  // The snapshot-derived half is shared across visitors and cached; the
  // entitlement is this visitor's and is not. Both still run in parallel.
  const [snapshot, entitlement] = await Promise.all([
    getHomeSnapshotData(),
    getCurrentEntitlement(),
  ]);
  const { proof, sectors, heroExample } = snapshot;
  // Whether anyone can actually register. Both Clerk keys, not just the public
  // one: a publishable key alone renders a form that cannot verify a session.
  const accountsEnabled = isAuthConfigured();

  return (
    <>
      <Hero
        proof={proof}
        example={heroExample}
      />
      <ProofBand proof={proof} />
      <SectorBand rows={sectors} />
      <ProductSurfaces />
      <HowItWorks />
      <Trust proof={proof} />
      <Section id="pricing">
        <PricingPlans entitlement={entitlement} />
      </Section>
      <Questions />
      <ClosingCta signedIn={entitlement.signedIn} accountsEnabled={accountsEnabled} />
    </>
  );
}

/* ------------------------------------------------------------------ hero -- */

/**
 * The hero's worked example: one contract, three order sizes.
 *
 * Deliberately a table and not a chart. Three numbers do not need an axis, and
 * the reader's takeaway is the comparison between them, which a chart would make
 * prettier and slower to read.
 */
function HeroExampleCard({ example }: { example: HeroExample }) {
  const cheapest = example.rungs.reduce((best, rung) =>
    Number(rung.measured_cost) < Number(best.measured_cost) ? rung : best,
  );
  const dearest = example.rungs.reduce((worst, rung) =>
    Number(rung.measured_cost) > Number(worst.measured_cost) ? rung : worst,
  );
  const spread =
    Number(cheapest.measured_cost) > 0
      ? Number(dearest.measured_cost) / Number(cheapest.measured_cost)
      : null;

  return (
    <div className="mt-6 max-w-xl rounded-lg border border-line bg-sunken p-4">
      <p className="t-label mb-2">Hosted snapshot example</p>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <Link
          href={`/cost/${example.marketId}`}
          className="text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
        >
          {displayTitle(example.title)}
        </Link>
        <span className="t-meta">
          {example.platform} · quoted{" "}
          <span className="font-mono">{cents(example.quoted)}</span>
        </span>
      </div>

      <div className="table-wrap mt-3">
        <table className="w-full text-left">
          <thead>
            <tr className="t-meta">
              <th className="pb-1 font-normal">Order size</th>
              <th className="pb-1 text-right font-normal">Est. cost each</th>
              <th className="pb-1 text-right font-normal">Over quote</th>
            </tr>
          </thead>
          <tbody className="font-mono text-sm">
            {example.rungs.map((rung) => (
              <tr key={rung.size} className="border-t border-line">
                <td className="py-1">{rung.size}</td>
                <td className="py-1 text-right">{cents(rung.measured_cost)}</td>
                <td className="py-1 text-right text-warn">
                  {rung.measured_premium_ratio === null
                    ? "—"
                    : `+${pct(rung.measured_premium_ratio)}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-2.5 t-meta">
        {spread && spread >= 1.5 ? (
          <>
            Same contract, same instant, a{" "}
            <strong>{spread.toFixed(1)}× spread</strong> in cost per contract.{" "}
          </>
        ) : null}
        Observed depth and published fee rules; transfer and capital assumptions
        disclosed separately.
      </p>
    </div>
  );
}

function Hero({
  proof,
  example,
}: {
  proof: ResearchProof;
  example: HeroExample | null;
}) {
  return (
    <section className="mx-auto max-w-6xl px-4 pb-14 pt-16 sm:pt-24">
      <p className="t-label">Prediction Market Value Lab</p>
      <h1 className="mt-3 max-w-4xl text-[2rem] leading-[1.12] sm:text-[2.75rem]">
        Know what your order really costs — before you place it.
      </h1>
      {/*
       * The hero used to end on "most of the time the answer is that no trade is
       * justified, and it says so." That sentence is true, it is the reason to
       * trust the rest of the site — and it was the whole pitch, which left a
       * first-time reader with a product whose headline promise is emptiness.
       *
       * Honesty now backs the claim instead of being the claim. The lead is the
       * measurement that has an answer on every visit: cost needs no probability
       * estimate, so it is not gated on the independence rule that keeps the
       * opportunity surfaces empty.
       */}
      <p className="t-lead mt-5 max-w-2xl">
        PMVL&apos;s read-only browser overlay prices the side and size on an open
        Kalshi or Polymarket ticket from the public book. It shows estimated cost
        per contract, break-even probability, and when another placeable size costs
        less — without touching the order.
      </p>

      {/*
       * The worked example, not a restatement of the claim.
       *
       * The hero used to spend two paragraphs asserting that fee rounding makes
       * small orders expensive, and showed no number until three screens down.
       * The assertion is abstract and the demonstration is not, so the
       * demonstration goes first — on a contract that exists, priced now.
       *
       * The second paragraph, about where the models do and do not have
       * independent coverage, moved to the research section. It is true and it
       * is a statement about a different surface; in the hero it answered a
       * question a first-time reader has not asked yet.
       */}
      {example ? <HeroExampleCard example={example} /> : null}

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <Link href="/extension" className="btn-primary">
          Get the live cost overlay
        </Link>
        <Link href="/cost" className="btn-quiet">
          Try the snapshot calculator
        </Link>
        <Link
          href="/app"
          className="text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
        >
          Research briefing
        </Link>
      </div>

      {/*
       * The freshness caveat sits with the hero rather than three screens down.
       * The hosted deployment serves a frozen snapshot; a landing page that
       * implies a continuous live scan would be making the one claim this
       * product spends the most effort not making.
       */}
      <p className="mt-6 t-meta">
        {proof.available && proof.snapshotMode ? (
          <>
            This deployment serves a <strong>frozen research snapshot</strong>,
            not a continuous live scan. Freshest observed quote:{" "}
            <span className="font-mono">{utcTime(proof.freshestQuoteAt)}</span>.
            Most markets carry older quotes than that one; each page shows its
            own.
          </>
        ) : proof.available ? (
          <>
            Serving mode:{" "}
            <span className="font-mono">{proof.servingMode ?? "unknown"}</span>.
            Freshest observed quote:{" "}
            <span className="font-mono">{utcTime(proof.freshestQuoteAt)}</span>.
          </>
        ) : (
          <>
            Live figures are unavailable right now, so none are shown. The
            research API could not be reached from this page.
          </>
        )}
      </p>
    </section>
  );
}

/* --------------------------------------------------------- research proof -- */

function ProofBand({ proof }: { proof: ResearchProof }) {
  if (!proof.available) {
    return (
      <Section id="proof" className="bg-sunken">
        <SectionHeading
          eyebrow="Live research state"
          title="Figures unavailable"
          lead="The research API did not answer this request, so this section is empty rather than filled with a cached or plausible number. The research pages themselves will say the same thing."
        />
        <Link
          href="/system"
          className="mt-6 inline-block text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
        >
          Check system status
        </Link>
      </Section>
    );
  }

  const count = (value: number | null) =>
    value === null ? "—" : value.toLocaleString();

  return (
    <Section id="proof" className="bg-sunken">
      <SectionHeading
        eyebrow="Live research state"
        title="What the deployment is actually serving"
        lead="Read from the same API the research pages read, at the moment this page was rendered. Nothing here is a stored marketing figure."
      />

      <dl className="mt-8 grid grid-cols-2 gap-x-6 gap-y-6 border-y border-line py-6 sm:grid-cols-3 lg:grid-cols-6">
        <ProofFigure
          label="Markets covered"
          value={count(proof.markets)}
          note="ingested across Kalshi and Polymarket"
        />
        <ProofFigure
          label={`Actionable · ${PROOF_HORIZON}`}
          value={count(proof.actionable)}
          note={
            proof.actionable === 0
              ? "nothing cleared every gate — the normal result"
              : "cleared every gate at this horizon"
          }
        />
        <ProofFigure
          label="Watchlist"
          value={count(proof.watchlist)}
          note="scored, not actionable — the coverage gap"
        />
        <ProofFigure
          label="Pipeline jobs"
          value={
            proof.jobsTotal === null
              ? "—"
              : `${proof.jobsSucceeded ?? 0}/${proof.jobsTotal}`
          }
          note="recorded jobs that succeeded"
        />
        <ProofFigure
          label="Freshest quote"
          value={utcTime(proof.freshestQuoteAt)}
          note="one observation, not a capture time for the set"
        />
        <ProofFigure
          label="Serving mode"
          value={proof.servingMode ?? "unknown"}
          note={
            proof.tradingExecutionEnabled
              ? "trading execution ENABLED"
              : "trading execution disabled"
          }
          tone="muted"
        />
      </dl>

      <p className="mt-4 t-meta">
        Every figure above describes a frozen snapshot and carries that caveat on
        the page it comes from. Model {proof.modelVersion ?? "version unknown"}.{" "}
        <Link href="/system" className="underline underline-offset-2">
          Full operational detail
        </Link>
        .
      </p>
    </Section>
  );
}

/* -------------------------------------------------------- sector premium -- */

function SectorBand({ rows }: { rows: CostByCategory[] | null }) {
  // A failed read renders nothing at all. This band exists to make a claim about
  // relative cost across sectors; a partial or stale version of that claim is
  // worse than its absence, and the page reads fine without it.
  if (!rows || rows.length < 2) return null;

  return (
    <Section id="sectors">
      <SectionHeading
        eyebrow="Measured across the snapshot"
        title="Politics has the highest median entry premium in this snapshot"
        lead={`Descriptive snapshot comparison at ${SECTOR_SIZE} contracts. The stack uses observed depth and published venue fee rules plus disclosed transfer and capital-cost assumptions; it does not require a probability forecast.`}
      />
      <SectorPremium rows={rows} size={SECTOR_SIZE} />
    </Section>
  );
}

/* ------------------------------------------------------ product surfaces -- */

function ProductSurfaces() {
  return (
    <Section id="product">
      <SectionHeading
        eyebrow="The product"
        title="Seven surfaces, all of them public"
        lead="Screens from the working application and packaged extension beta, not renderings of a planned product. Every surface is reachable without an account."
      />

      <div className="mt-6">
        {/*
         * Cost leads. It is the only surface that answers on every visit — it
         * needs no probability estimate, so the independence rule that empties
         * the others cannot empty it — and it covers the categories the models
         * decline: politics, sports and macro all have a cost even when they
         * have no forecast.
         */}
        <FeatureRow
          index={1}
          title="Live cost, on the order ticket"
          body="The browser overlay reads the side and size a trader is already considering, fetches the public book, and calculates the entry-cost stack locally. The hosted snapshot remains available for exploration and shareable deep dives; the time-sensitive answer lives beside the ticket."
          points={[
            "The user's size, cost per contract, premium, and break-even in one compact panel",
            "A lower-cost placeable size when the arithmetic supports one",
            "Read-only public data, with assumptions and quote freshness stated in the panel",
          ]}
          href="/extension"
          linkLabel="Open the live-overlay beta"
          shot={
            <ProductShot
              src="/product/live-entry-cost-overlay.jpg"
              alt="A working PMVL browser-extension preview showing the user's Polymarket order size, estimated cost, premium over the quote, break-even probability, a lower-cost size, public-book freshness, and read-only research disclosures."
              width={1280}
              height={720}
              priority
            />
          }
        />

        <FeatureRow
          index={2}
          title="Research briefing"
          body="The first thing a returning reader wants is not a pitch. It is what was actionable at the displayed snapshot, what was not, and how stale the data is. The briefing answers those above the fold and puts the filtering funnel underneath, so a list of ten has a denominator and a list of zero has a reason."
          points={[
            "Actionable, watchlist and disagreement counts at the chosen horizon",
            "The funnel that produced them, stage by stage, with counts",
            "Freshest observed quote and pipeline state, stated in text",
          ]}
          reverse
          href="/app"
          linkLabel="Open the briefing"
          shot={
            <ProductShot
              src="/product/briefing.webp"
              alt="The PMVL research briefing: a measurement band showing actionable, watchlist and disagreement counts, followed by ranked opportunity cards and a stage-by-stage filtering funnel."
              width={1600}
              height={1075}
            />
          }
        />

        <FeatureRow
          index={3}
          title="The probability triad"
          body="A market detail page shows three probabilities side by side: what the market implies, what the model estimates independently, and the conservative lower bound the ranking actually uses. When there is no independent estimate, the page says so instead of quietly reporting the market price back as a forecast."
          points={[
            "Market-implied, model mean, and the conservative bound used for ranking",
            "The uncertainty interval, and the confidence attached to it",
            "An explicit statement when no independent estimate exists",
          ]}
          href="/market/630"
          linkLabel="See a market detail page"
          shot={
            <ProductShot
              src="/product/probability.webp"
              alt="A PMVL market detail page showing market-implied probability, the independent model estimate, its uncertainty interval and the conservative bound, alongside the order-book ladder."
              width={1600}
              height={1075}
            />
          }
        />

        <FeatureRow
          index={4}
          title="Actionable, kept apart from Diagnostics"
          body="Arbitrage results are split into two views that are never mixed. Actionable means every condition for execution held at scan time. Diagnostics collects the rest — logical mispricings, stale quotes, structures that cannot be executed — because they are informative about the venues and misleading as trade ideas."
          points={[
            "Executable claimed only when every stated condition holds",
            "Stale-quote and logical-mispricing rows labelled as such, not hidden",
            "Both views carry the scan time they were computed from",
          ]}
          href="/arbitrage?view=actionable"
          linkLabel="Compare the two views"
          shot={
            <ProductShot
              src="/product/actionable-diagnostics.webp"
              alt="The PMVL arbitrage page with its Actionable and Diagnostics views side by side, each row labelled with its arbitrage kind and risk flags."
              width={1600}
              height={1075}
            />
          }
        />

        <FeatureRow
          index={5}
          title="Rule and liquidity filtering"
          body="Market discovery shows the things that decide whether a quoted price is reachable: the ask ladder's depth in dollars, the spread, the tick size, the fee rate, and whether the quote is stale. A contract with an attractive price and no size behind it is not an opportunity, and the table is built so that is visible rather than inferred."
          points={[
            "Executable depth in dollars, per side, not a single top-of-book price",
            "Spread, tick size, fee rate and quote age on every row",
            "Venue availability shown separately from exchange listing",
          ]}
          reverse
          href="/markets"
          linkLabel="Browse markets"
          shot={
            <ProductShot
              src="/product/markets.webp"
              alt="The PMVL markets table listing contracts from both venues with best bid and ask, spread, order-book depth in dollars, volume and resolution time."
              width={1600}
              height={1075}
            />
          }
        />

        <FeatureRow
          index={6}
          title="Track record and backtest transparency"
          body="Recommendations are frozen at publication and kept whether they win or lose. The backtest reports return and accuracy independently, because a strategy can make money while forecasting worse than the market — and reporting only the profitable half of that is how backtests mislead."
          points={[
            "Return and Brier score reported separately, never merged into one claim",
            "Look-ahead prevention and data-quality grade stated per run",
            "Demo and backtest surfaces labelled as simulation, not live performance",
          ]}
          href="/backtest?mode=demo"
          linkLabel="Read a backtest run"
          shot={
            <ProductShot
              src="/product/backtest.webp"
              alt="A PMVL backtest page showing settled-bet counts, return, drawdown, Brier score against the market benchmark and a calibration curve."
              width={1600}
              height={1075}
            />
          }
        />

        <FeatureRow
          index={7}
          title="System and snapshot transparency"
          body="The system page states what this deployment is: which commit it runs, whether it serves a frozen snapshot, when each part of that snapshot was observed, which pipeline jobs last succeeded, and whether trading execution is enabled. It is the page that lets a reader check the claims on this one."
          points={[
            "Deployed commit, runtime mode and snapshot timing block",
            "Per-job pipeline state with last success and last failure",
            "Data sources listed with what each is used for",
          ]}
          reverse
          href="/system"
          linkLabel="Inspect system status"
          shot={
            <ProductShot
              src="/product/system.webp"
              alt="The PMVL system page showing runtime mode, deployed commit, snapshot timing, per-job pipeline status and the list of upstream data sources."
              width={1600}
              height={1075}
            />
          }
        />
      </div>
    </Section>
  );
}

/* ----------------------------------------------------------- how it works -- */

const PIPELINE = [
  {
    title: "Collect market and order-book data",
    body: "Public Kalshi and Polymarket endpoints. No credentials, no private feeds, no execution access.",
  },
  {
    title: "Normalize contract rules",
    body: "Strike type, settlement source, close time and fee schedule differ per venue and per series. Two contracts are treated as the same question only when their rules agree.",
  },
  {
    title: "Generate independent estimates where supported",
    body: "Category models that use evidence other than the target market's own price — index levels on a trading-time clock, spot references, coherent sibling outcome sets. Where no such evidence exists, no estimate is produced.",
  },
  {
    title: "Apply fee, liquidity and freshness filters",
    body: "Executable VWAP against the ask ladder at reference size, then fees, rounding, slippage, transfer and capital cost. Stale quotes and thin books are filtered, not discounted.",
  },
  {
    title: "Publish a validated read-only snapshot",
    body: "The result is checked and published as a frozen artifact the site serves read-only. Nothing in this path can place an order.",
  },
] as const;

function HowItWorks() {
  return (
    <Section id="how-it-works" className="bg-sunken">
      <SectionHeading
        eyebrow="How it works"
        title="Five stages, and one of them is allowed to produce nothing"
        lead="Stage three is where most prediction-market products quietly stop being honest. A model that always returns a probability will return one for markets it has no information about — and that probability will be the market price wearing a hat."
      />

      <ol className="mt-8 grid gap-px overflow-hidden rounded-[3px] border border-line bg-line lg:grid-cols-5">
        {PIPELINE.map((stage, index) => (
          <li key={stage.title} className="bg-base p-5">
            <p className="num t-label">{String(index + 1).padStart(2, "0")}</p>
            <h3 className="t-sub-title mt-2">{stage.title}</h3>
            <p className="t-body mt-2 text-xs">{stage.body}</p>
          </li>
        ))}
      </ol>

      <p className="mt-6 t-prose">
        Markets without an independent estimate are not called AI predictions and
        are not ranked. They appear on the watchlist, which exists so the
        coverage gap is visible instead of papered over.
      </p>
    </Section>
  );
}

/* ------------------------------------------------------------------ trust -- */

function Trust({ proof }: { proof: ResearchProof }) {
  const signals = [
    {
      title: "The methodology is published in full",
      body: "The formulas, the admission rule, the independence rule and the known limitations are on a public page, generated from the same constants the pipeline runs on.",
      href: "/methodology",
      label: "Read the methodology",
    },
    {
      title: "The snapshot timestamp is visible everywhere",
      body: proof.available
        ? `Every page states the instant its data was observed. The freshest observation in the current snapshot is ${utcTime(proof.freshestQuoteAt)}, and most markets are older than that.`
        : "Every page states the instant its data was observed, and says so even when — as right now — the figure cannot be read.",
      href: "/system",
      label: "See snapshot timing",
    },
    {
      title: "Model coverage is stated, not implied",
      body: "Categories with an independent model are named. Markets without one are shown on the watchlist and are explicitly not ranked as opportunities.",
      href: "/app",
      label: "See the watchlist",
    },
    {
      title: "Limitations are part of the documentation",
      body: "The methodology page ends with what the platform does not do well. It is maintained as carefully as the parts that work.",
      href: "/methodology",
      label: "Read the limitations",
    },
    {
      title: "The track record keeps losers",
      body: "Recommendations are frozen at publication time and remain visible after settlement regardless of outcome. Backtests report accuracy and return as separate facts.",
      href: "/track-record",
      label: "Open the track record",
    },
    {
      title: "Read-only, with execution disabled",
      body: proof.available
        ? `The runtime serves ${proof.servingMode ?? "a read-only snapshot"} and reports trading execution ${proof.tradingExecutionEnabled ? "ENABLED" : "disabled"}. PMVL holds no funds, stores no keys and places no orders.`
        : "The runtime is read-only. PMVL holds no funds, stores no wallet keys and places no orders.",
      href: "/system",
      label: "Verify on the system page",
    },
  ];

  return (
    <Section id="trust">
      <SectionHeading
        eyebrow="Why you might trust this"
        title="Checkable claims only"
        lead="There are no testimonials, user counts, partner logos or performance claims on this page, because none of them could be verified by a reader. Everything below can be."
      />
      <div className="mt-8 grid gap-x-10 gap-y-8 sm:grid-cols-2 lg:grid-cols-3">
        {signals.map((signal) => (
          <div key={signal.title} className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">{signal.title}</h3>
            <p className="t-body mt-2 text-xs">{signal.body}</p>
            <Link
              href={signal.href}
              className="mt-3 inline-block text-xs underline decoration-line-strong underline-offset-4 hover:decoration-current"
            >
              {signal.label}
            </Link>
          </div>
        ))}
      </div>
    </Section>
  );
}

/* -------------------------------------------------------------------- faq -- */

function Questions() {
  return (
    <Section id="faq" className="bg-sunken">
      <SectionHeading eyebrow="Questions" title="Answers that match the behaviour" />
      <Faq
        items={[
          {
            q: "Is this financial advice?",
            a: (
              <>
                No. PMVL publishes research and informational analysis. It is not
                investment, legal, tax or financial advice, not a solicitation
                and not an offer to trade. See the{" "}
                <Link href="/risk-disclosure" className="underline underline-offset-2">
                  risk disclosure
                </Link>
                .
              </>
            ),
          },
          {
            q: "Is the data live?",
            a: "No. The hosted deployment serves a frozen, validated snapshot published by the pipeline. Prices and model estimates are stale by design, every page states the instant its data was observed, and automatic snapshot publication is currently switched off.",
          },
          {
            q: "What does Actionable mean?",
            a: "That every stated condition held at scan time: an independent probability estimate existed, the conservative lower bound of that estimate still beat the executable price after fees, slippage, transfer and capital costs, the quote was fresh enough, and the book had the depth to fill the reference size. It is a description of the scan, not a recommendation to trade.",
          },
          {
            q: "Why can Actionable be zero?",
            a: (
              <>
                Because these venues are usually priced efficiently enough that
                nothing clears the bar. Zero is the ordinary, correct result, and
                the filtering funnel shows how many candidates were examined and on
                what grounds each stage declined them. A scanner that always finds
                something has set its bar to guarantee that. The{" "}
                <Link href="/cost" className="underline underline-offset-2">
                  cost surface
                </Link>{" "}
                is not gated that way — it needs no probability estimate, so it has
                an answer for every market with a quote whether or not anything is
                actionable.
              </>
            ),
          },
          {
            q: "What does the cost surface measure that the venues do not show?",
            a: "An estimated entry-cost stack at the size you choose: observed depth and published venue fee and rounding rules, plus separately disclosed transfer and capital-cost assumptions. Because a binary contract pays exactly $1, that estimate maps to a break-even probability under the same assumptions. Kalshi ceils its fee to the whole cent on the whole order, so a 1¢ contract bought one at a time carries a 1¢ venue fee — a rule-derived fact that does not depend on any forecast.",
          },
          {
            q: "Which markets are modelled independently?",
            a: "Only those where evidence exists that is not the target market's own price — index thresholds against a trading-time volatility clock, crypto thresholds against spot references, and outcome sets that are complete enough for a coherence constraint. Everything else is scored as watchlist-only and is never ranked as an opportunity.",
          },
          {
            q: "Does PMVL place trades?",
            a: "No. The runtime is read-only, trading execution is disabled, and the system page reports that state. PMVL holds no funds, stores no wallet keys and has no execution credentials for either venue.",
          },
          {
            q: "How are fees and liquidity handled?",
            a: "Prices are the volume-weighted average of the ask ladder at a reference size, not a last trade or a midpoint. On top of that the cost stack adds venue fees, tick rounding, slippage, cross-venue transfer cost and the capital cost of holding to resolution. Thin books and stale quotes are filtered out rather than adjusted for.",
          },
          {
            q: "Can a subscription be cancelled?",
            a: (
              <>
                There is nothing to cancel yet — billing is not live on this
                deployment. When the paid tier is finished, subscriptions will be
                managed through Stripe&apos;s own customer portal, where a
                subscription can be cancelled at any time and access continues
                until the end of the period already paid for. The{" "}
                <Link href="/terms" className="underline underline-offset-2">
                  terms
                </Link>{" "}
                describe this.
              </>
            ),
          },
        ]}
      />
    </Section>
  );
}

/* ------------------------------------------------------------------- cta -- */

function ClosingCta({
  signedIn,
  accountsEnabled,
}: {
  signedIn: boolean;
  accountsEnabled: boolean;
}) {
  return (
    <Section id="get-started">
      <div className="max-w-2xl">
        <h2 className="t-page-title">Start with the cost of the order in front of you</h2>
        <p className="t-prose mt-3">
          Install the read-only beta for a live venue page, or use the hosted
          snapshot calculator to inspect and share the same cost stack without an
          extension. The full research record and methodology remain public.
        </p>
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <Link href="/extension" className="btn-primary">
            Get the live overlay
          </Link>
          <Link href="/app" className="btn-quiet">
            Explore research
          </Link>
          {signedIn ? (
            <Link href="/account" className="btn-quiet">
              Account
            </Link>
          ) : accountsEnabled ? (
            <Link href="/sign-up" className="btn-quiet">
              Create free account
            </Link>
          ) : null}
          <Link
            href="/pricing"
            className="text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
          >
            Compare plans
          </Link>
        </div>
      </div>
    </Section>
  );
}
