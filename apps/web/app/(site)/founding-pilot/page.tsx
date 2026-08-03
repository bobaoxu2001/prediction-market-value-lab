import type { Metadata } from "next";
import Link from "next/link";

import { Faq, Section, SectionHeading } from "@/components/marketing";
import {
  PILOT_DURATION_DAYS,
  PILOT_INTEREST_MAILTO,
  PILOT_MEMBER_CAP,
  PILOT_PRICE_USD,
  pilotPaymentLink,
  pilotSeatsRemaining,
} from "@/lib/pilot";
import { absoluteUrl, SUPPORT_EMAIL } from "@/lib/site";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Founding Research Pilot",
  description:
    "A 30-day paid pilot of PMVL's daily prediction-market research digest. One-time USD 49, capped at 20 members. Research only — not advice, and no return is promised.",
  alternates: { canonical: absoluteUrl("/founding-pilot") },
};

export default function FoundingPilotPage() {
  const paymentLink = pilotPaymentLink();
  const remaining = pilotSeatsRemaining();

  return (
    <>
      {/* ------------------------------------------------------------ hero -- */}
      <section className="mx-auto max-w-6xl px-4 pb-12 pt-16">
        <p className="t-label">Founding Research Pilot</p>
        <h1 className="mt-3 max-w-4xl text-[2rem] leading-[1.12] sm:text-[2.5rem]">
          Thirty days of the research, including the days it says there is nothing
          to do.
        </h1>
        <p className="t-lead mt-5 max-w-2xl">
          Each morning you get one report: what cleared the bar, what did not, and
          how many candidates were examined to get there. Most days the answer is
          zero. That is the finding, not a failure — and you will be able to see
          exactly why.
        </p>

        <dl className="mt-8 grid max-w-xl grid-cols-3 gap-x-6 border-y border-line py-5">
          <div>
            <dt className="t-label">Price</dt>
            <dd className="num mt-1 text-xl font-semibold">
              ${PILOT_PRICE_USD}
            </dd>
            <dd className="t-meta mt-0.5">one time, not a subscription</dd>
          </div>
          <div>
            <dt className="t-label">Runs for</dt>
            <dd className="num mt-1 text-xl font-semibold">
              {PILOT_DURATION_DAYS} days
            </dd>
            <dd className="t-meta mt-0.5">from your first delivery</dd>
          </div>
          <div>
            <dt className="t-label">Members</dt>
            <dd className="num mt-1 text-xl font-semibold">
              {PILOT_MEMBER_CAP} max
            </dd>
            <dd className="t-meta mt-0.5">
              {remaining === null
                ? "delivered by hand"
                : `${remaining} still open`}
            </dd>
          </div>
        </dl>

        <div className="mt-8 flex flex-wrap items-center gap-3">
          {paymentLink ? (
            <a href={paymentLink} className="btn-primary" rel="noreferrer noopener">
              Join the pilot — ${PILOT_PRICE_USD}
            </a>
          ) : (
            // No payment URL is configured, so there is nothing to buy. A button
            // that looks like a purchase and is not one is the single most
            // dishonest thing this page could do, so the CTA is an enquiry and
            // says so.
            <a href={PILOT_INTEREST_MAILTO} className="btn-primary">
              Request a founding spot
            </a>
          )}
          <Link href="/app" className="btn-quiet">
            Read the free research first
          </Link>
        </div>

        {!paymentLink ? (
          <p className="mt-4 t-meta">
            <strong className="text-ink-muted">
              Payment is not open yet. Founding spots are being reviewed manually.
            </strong>{" "}
            Requesting a spot sends an email — it is an enquiry, not a purchase,
            and nothing is charged.
          </p>
        ) : null}

        <p className="mt-6 t-meta">
          <strong className="text-ink-muted">
            Research and information only.
          </strong>{" "}
          Not investment, legal, tax or financial advice, not personalised to you,
          and not a recommendation to trade. No return is promised or implied.
          Prediction-market contracts can settle worthless. Read the{" "}
          <Link href="/risk-disclosure" className="underline underline-offset-2">
            risk disclosure
          </Link>{" "}
          before buying.
        </p>
      </section>

      {/* --------------------------------------------------------- receive -- */}
      <Section id="what-you-get" className="bg-sunken">
        <SectionHeading
          eyebrow="What arrives"
          title="One report a day, and a review at the end of each week"
          lead="Every figure is traceable to a Snapshot with a published checksum, a pipeline commit and a data cutoff, all printed on the report."
        />

        <div className="mt-8 grid gap-8 lg:grid-cols-3">
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">Zero to three candidates</h3>
            <p className="t-body mt-2 text-xs">
              Never more than three. Past three, the marginal entry exists to fill
              a page rather than because it earned a place. Each carries the
              executable ask as a volume-weighted average at size, the all-in cost
              after fees and slippage, the market-implied probability, the
              independent estimate, the decision-adjusted bound the gate actually
              uses, net edge after costs, resting liquidity, rules risk, the
              conditions that would invalidate it, and the resolution date.
            </p>
          </div>
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">The days with nothing</h3>
            <p className="t-body mt-2 text-xs">
              A no-opportunity report is a full report. It shows the filtering
              funnel — how many live markets, how many open, how many carried an
              estimate independent of the market&apos;s own price, how many were
              priced against the ladder — and a tally of exactly where each
              candidate stopped. On the sample below, 317 sides were priced and
              every one failed on net EV after costs.
            </p>
          </div>
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">A weekly reckoning</h3>
            <p className="t-body mt-2 text-xs">
              Every scored market that settled during the week is graded, whether
              or not it was ever recommended — which is what keeps the review
              meaningful in a week with no recommendations. Estimates that had an
              independent prior are scored separately from ones derived from the
              market price, because scoring an echo against its source proves
              nothing.
            </p>
          </div>
        </div>
      </Section>

      {/* ---------------------------------------------------------- samples -- */}
      <Section id="samples">
        <SectionHeading
          eyebrow="Read them before you buy"
          title="Three real reports, generated from the published Snapshot"
          lead="Not mock-ups, and not current research. Each came out of the generator in this repository, run against the validated Snapshot dated 2026-07-31, and each is stamped as a historical sample in every format."
        />

        <div className="mt-8 grid gap-px overflow-hidden rounded-[3px] border border-line bg-line lg:grid-cols-3">
          {[
            {
              name: "A day with no opportunity",
              file: "historical-no-actionable",
              body:
                "1,595 live markets narrowed to 186 carrying an independent prior, 317 sides priced against the ask ladder, none admitted. The funnel and the rejection tally are the report.",
            },
            {
              name: "The diagnostic watchlist",
              file: "historical-watchlist",
              body:
                "The same scan at a 30-day horizon, showing the near misses and exactly why each one is not actionable. Nothing on a watchlist is a recommendation, and no net edge is quoted for any of it.",
            },
            {
              name: "A weekly outcome review",
              file: "historical-weekly-review",
              body:
                "410 scored markets settled that week. Independent estimates scored 0.0359 Brier against the market's 0.0435; market-derived ones scored exactly the market, which is the point of splitting them.",
            },
          ].map((sample) => (
            <div key={sample.file} className="bg-base p-6">
              <h3 className="t-sub-title">{sample.name}</h3>
              <p className="t-body mt-2 text-xs">{sample.body}</p>
              <p className="t-meta mt-3 font-mono">
                docs/samples/pilot/{sample.file}.md
              </p>
            </div>
          ))}
        </div>

        <p className="mt-6 t-meta">
          <strong className="text-ink-muted">
            All three are historical samples, not current market research.
          </strong>{" "}
          They were generated from the validated Snapshot dated 2026-07-31 and
          every price, probability and edge in them is now historical. All three,
          in Markdown, HTML email and plain text, are committed at{" "}
          <span className="font-mono">docs/samples/pilot/</span>; the generator is{" "}
          <span className="font-mono">scripts/generate_pilot_digest.py</span>.
        </p>
      </Section>

      {/* ------------------------------------------------------------ gate -- */}
      <Section id="stale-data" className="bg-sunken">
        <SectionHeading
          eyebrow="The part worth paying for"
          title="It refuses rather than guesses"
          lead="A daily research product has one temptation: send something every day. The gate is what removes it."
        />
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">What must hold before any candidate is published</h3>
            <ul className="mt-3 space-y-2">
              {[
                "The Snapshot file matches the checksum in its own manifest.",
                "The manifest says validation passed and the release was published, not held.",
                "The ingest, orderbook, scoring and ranking jobs all finished successfully.",
                "Top-of-book, full-order-book and model-prediction data are all inside their freshness limits — measured at the moment of sending, not the moment the Snapshot was built.",
              ].map((item) => (
                <li key={item} className="flex gap-2 text-sm text-ink-muted">
                  <span aria-hidden="true" className="mt-[0.45rem] h-px w-3 shrink-0 bg-line-strong" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">If any of them fails</h3>
            <p className="t-body mt-2">
              You still get a report. It names which input was stale and by how
              many hours, and it contains no candidates — because the generator
              stops before reading the data rather than reading it and hoping. A
              stale quote dressed as a candidate is worse than no report, and it
              is the specific failure a paid daily product is most likely to
              produce.
            </p>
          </div>
        </div>
      </Section>

      {/* --------------------------------------------------------- honesty -- */}
      <Section id="not-buying">
        <SectionHeading
          eyebrow="Before you buy"
          title="What this is not"
          lead="Written plainly, because the reasons not to buy are the ones you would find out later anyway."
        />
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          {[
            {
              h: "Not advice, and not personalised",
              p: "Every member receives an identical report. It is written without any knowledge of your capital, tax position, jurisdiction or risk tolerance, and nothing in it is a recommendation to you. If you want advice, you want a licensed adviser, which PMVL is not.",
            },
            {
              h: "No promised return",
              p: "There is no claim, express or implied, that following this research makes money. Estimates can be wrong, quotes can be stale, liquidity can vanish and settlement rules can decide an outcome the plain reading of a title would not. Most days there will be nothing to act on at all.",
            },
            {
              h: "Not a trading service",
              p: "PMVL places no orders, holds no funds, custodies no assets and has no execution access to any venue. Trading execution is disabled in the runtime and the system page reports that state. Any position is one you open yourself, on a venue where you have your own account.",
            },
            {
              h: "Not a live feed",
              p: "The research runs on a validated Snapshot published on a schedule, not a continuous stream. Every report prints its Snapshot identity and data cutoff. If you need sub-second data, this is the wrong product and no amount of it will change that.",
            },
            {
              h: "Manual, and small on purpose",
              p: `Delivery is by hand to at most ${PILOT_MEMBER_CAP} members. There is no account to create, no dashboard and no login — the reports arrive by email. That is the honest shape of a pilot, and it is why the cap exists.`,
            },
            {
              h: "Refunds",
              p: "If the pilot does not deliver reports for the days you paid for, write to the support address and you will be refunded. Deciding that the research is not useful to you is not a defect, and that judgement is yours to make from the free samples before you buy.",
            },
          ].map((item) => (
            <div key={item.h} className="border-t border-line-subtle pt-4">
              <h3 className="t-sub-title">{item.h}</h3>
              <p className="t-body mt-2">{item.p}</p>
            </div>
          ))}
        </div>
      </Section>

      {/* ------------------------------------------------------------- faq -- */}
      <Section id="pilot-faq" className="bg-sunken">
        <SectionHeading eyebrow="Questions" title="Answers that match the product" />
        <Faq
          items={[
            {
              q: "What exactly am I paying for?",
              a: `A one-time USD ${PILOT_PRICE_USD} for ${PILOT_DURATION_DAYS} days of the daily digest and the weekly outcome review, delivered by email. It does not renew, there is nothing to cancel, and no card is stored by PMVL.`,
            },
            {
              q: "What happens on a day with no opportunities?",
              a: "You get the report anyway, with the full filtering funnel and the tally of where each candidate stopped. On efficiently priced venues that is the common outcome — the sample reports show a day where 317 sides were priced and none was admitted.",
            },
            {
              q: "Do I need an account?",
              a: "No. There is no login, no dashboard and no subscription. Fulfilment is a person sending you an email, which is why the pilot is capped at 20 members.",
            },
            {
              q: "Will you tell me what to buy?",
              a: "No. A candidate is a description of what the scan found — an executable ask, an independent probability, a net edge after costs, and the conditions that would make it wrong. What to do with that is your decision, and the report is identical for everybody who receives it.",
            },
            {
              q: "Does the free site already show me this?",
              a: (
                <>
                  Much of it, yes — and you should{" "}
                  <Link href="/app" className="underline underline-offset-2">
                    read the free research
                  </Link>{" "}
                  before paying. The pilot adds the daily written report, the
                  rules-risk and invalidation analysis per candidate, the weekly
                  graded outcome review, and delivery to your inbox. It does not
                  put anything currently free behind a paywall.
                </>
              ),
            },
            {
              q: "What if the pipeline breaks mid-pilot?",
              a: "You are told. The gate refuses to issue actionable research and the report says which input failed and by how much. Days lost to a refusal are added to the end of your 30 days.",
            },
            {
              q: "Can I see a report with actual candidates in it?",
              a: "Not from the current Snapshot — it contains none, and fabricating one to demonstrate the format would undermine the only thing being sold. The candidate layout is exercised by the test suite; the committed samples show what the real data actually produced.",
            },
          ]}
        />
      </Section>

      {/* ------------------------------------------------------------- cta -- */}
      <Section id="pilot-join">
        <div className="max-w-2xl">
          <h2 className="t-page-title">
            {paymentLink ? "Join the pilot" : "The pilot is not open yet"}
          </h2>
          <p className="t-prose mt-3">
            {paymentLink
              ? `USD ${PILOT_PRICE_USD}, one time, ${PILOT_DURATION_DAYS} days, capped at ${PILOT_MEMBER_CAP} members. Read the samples first — they are the product, not a teaser for it.`
              : `Payment is not open yet and founding spots are being reviewed manually, so nothing on this page can charge you. Requesting a spot sends an email and is an enquiry, not a purchase. Until payment opens, the samples and the whole research product are free to read.`}
          </p>
          <div className="mt-6 flex flex-wrap items-center gap-3">
            {paymentLink ? (
              <a href={paymentLink} className="btn-primary" rel="noreferrer noopener">
                Join the pilot — ${PILOT_PRICE_USD}
              </a>
            ) : (
              <a href={PILOT_INTEREST_MAILTO} className="btn-primary">
                Request a founding spot
              </a>
            )}
            <Link href="/app" className="btn-quiet">
              Explore the free research
            </Link>
            <Link
              href="/risk-disclosure"
              className="text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
            >
              Risk disclosure
            </Link>
          </div>
          <p className="mt-6 t-meta">
            Payments, when open, are processed by Stripe on Stripe&apos;s own
            hosted page. PMVL never sees or stores card details. Questions:{" "}
            <a
              href={`mailto:${SUPPORT_EMAIL}`}
              className="font-mono underline underline-offset-2"
            >
              {SUPPORT_EMAIL}
            </a>
            .
          </p>
        </div>
      </Section>
    </>
  );
}
