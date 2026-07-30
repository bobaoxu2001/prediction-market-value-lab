import Link from "next/link";

/**
 * Positioning, deliberately demoted below the research briefing.
 *
 * The previous version owned the entire first viewport: a marketing headline, two
 * title-case CTAs, four text links and three cards introduced by emoji. A reader
 * arriving at a research terminal wants to know what changed and what is
 * actionable; the product pitch is what they read second, not first.
 *
 * The three claims survive because they are the actual engineering claims of the
 * project. They are set as a rule-separated band rather than three rounded cards,
 * and the emoji are gone - they marked nothing the text did not already say.
 */
export function Hero({
  demoHref,
  backtestHref,
  guidedHref,
  caseStudyHref,
}: {
  demoHref: string;
  backtestHref: string;
  guidedHref: string;
  caseStudyHref: string;
}) {
  return (
    <section className="mt-10 border-t border-line pt-6">
      <h2 className="t-section-title">
        What this platform does differently
      </h2>
      {/*
       * The value proposition is a tested contract (test_integration.py asserts
       * "after real" and "trading costs" appear here), so the phrase is kept on
       * one source line - JSX reflow across a newline broke the substring once
       * already and the failure looked like a copy change nobody made.
       */}
      <p className="t-prose mt-1">
        {"Finds prediction-market positions that remain attractive after real trading costs."}{" "}
        PMVL scans Kalshi and Polymarket using executable order-book prices,
        probability estimates independent of the market&apos;s own price, and
        permanently tracked recommendations.
      </p>

      <dl className="mt-5 grid gap-x-8 gap-y-5 sm:grid-cols-3">
        {DIFFERENTIATORS.map((item) => (
          <div key={item.title} className="border-t border-line-subtle pt-3">
            <dt className="t-sub-title">{item.title}</dt>
            <dd className="t-body mt-1 text-xs">{item.body}</dd>
          </div>
        ))}
      </dl>

      {/*
       * These four labels are tested contracts, so the wording is left exactly as
       * the project set it. The redesign changes their weight, not their words:
       * two quiet buttons and two text links instead of four equal-weight CTAs
       * competing for a first-time visitor's attention.
       */}
      <div className="mt-6 flex flex-wrap items-center gap-3">
        <Link href={demoHref} className="btn-quiet">
          Explore Demo Opportunities
        </Link>
        <Link href={backtestHref} className="btn-quiet">
          View Backtest Results
        </Link>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
        <Link
          href={guidedHref}
          className="underline decoration-line-strong underline-offset-2 hover:decoration-current"
        >
          Start guided demo
        </Link>
        <Link
          href={caseStudyHref}
          className="underline decoration-line-strong underline-offset-2 hover:decoration-current"
        >
          See a recommendation from price to settlement
        </Link>
      </div>
    </section>
  );
}

const DIFFERENTIATORS = [
  {
    title: "Executable, not theoretical",
    body:
      "Uses actual ask depth, fees, slippage, liquidity, transfer costs and time to " +
      "resolution — not last trades or midpoints.",
  },
  {
    title: "Independent evidence only",
    body:
      "The model cannot manufacture an edge from the target market's own price.",
  },
  {
    title: "Every call is auditable",
    body:
      "Recommendations are frozen at publication time, and both winners and losers " +
      "remain permanently visible.",
  },
] as const;
