import Link from "next/link";

/**
 * Above-the-fold positioning.
 *
 * The page previously opened straight into a horizon-tabbed opportunity table,
 * which reads as an internal quant console: a first-time visitor could not tell
 * what the product does, how it differs from any other market scanner, or where to
 * click. The three differentiators are the actual engineering claims of the project,
 * stated in one line each rather than as an essay.
 */
export function Hero({ demoHref, backtestHref }: { demoHref: string; backtestHref: string }) {
  return (
    <section className="mb-8">
      <h1 className="max-w-3xl text-2xl font-semibold leading-tight tracking-tight sm:text-3xl">
        Find prediction-market opportunities that remain attractive after real
        trading costs.
      </h1>
      <p className="mt-3 max-w-2xl text-sm leading-relaxed text-neutral-600 dark:text-neutral-400">
        PMVL scans Kalshi and Polymarket using executable order-book prices,
        independent probability estimates, and permanently tracked recommendations.
      </p>

      <div className="mt-5 flex flex-wrap gap-3">
        <Link
          href={demoHref}
          className="rounded-lg bg-neutral-900 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-neutral-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-neutral-900 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300 dark:focus-visible:outline-neutral-100"
        >
          Explore Demo Opportunities
        </Link>
        <Link
          href={backtestHref}
          className="rounded-lg border border-neutral-300 px-4 py-2.5 text-sm font-medium transition hover:bg-neutral-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-neutral-900 dark:border-neutral-700 dark:hover:bg-neutral-800 dark:focus-visible:outline-neutral-100"
        >
          View Backtest Results
        </Link>
      </div>

      <ul className="mt-8 grid gap-4 sm:grid-cols-3">
        {DIFFERENTIATORS.map((item) => (
          <li key={item.title} className="card p-4">
            <div aria-hidden className="text-lg leading-none">
              {item.icon}
            </div>
            <h2 className="mt-2 text-sm font-semibold">{item.title}</h2>
            <p className="mt-1 text-xs leading-relaxed text-neutral-600 dark:text-neutral-400">
              {item.body}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

const DIFFERENTIATORS = [
  {
    icon: "📐",
    title: "Executable, not theoretical",
    body:
      "Uses actual ask depth, fees, slippage, liquidity, transfer costs and time to " +
      "resolution — not last trades or midpoints.",
  },
  {
    icon: "🔍",
    title: "Independent evidence only",
    body:
      "The model cannot manufacture an edge from the target market's own price.",
  },
  {
    icon: "📄",
    title: "Every call is auditable",
    body:
      "Recommendations are frozen at publication time, and both winners and losers " +
      "remain permanently visible.",
  },
] as const;
