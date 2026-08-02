import type { Metadata } from "next";
import Link from "next/link";

import { LegalPage, LegalSection } from "@/components/legal";
import { absoluteUrl, SUPPORT_EMAIL } from "@/lib/site";

export const metadata: Metadata = {
  title: "Risk disclosure",
  description:
    "PMVL publishes research, not advice. Prediction markets involve the risk of loss, estimates can be wrong, quotes can be stale, liquidity can be insufficient and settlement rules can decide outcomes.",
  alternates: { canonical: absoluteUrl("/risk-disclosure") },
};

export default function RiskDisclosurePage() {
  return (
    <LegalPage
      title="Risk disclosure"
      updated="2 August 2026"
      summary="What can go wrong when using this research, stated plainly. This page is the one to read before any other."
    >
      <LegalSection id="research-only" title="This is research, not advice">
        <p>
          Prediction Market Value Lab (&ldquo;PMVL&rdquo;) publishes research and
          informational analysis about publicly traded prediction-market
          contracts. It is <strong>not</strong> investment advice, legal advice,
          tax advice or financial advice. It is not a recommendation,
          solicitation or offer to buy or sell anything, and it is not
          personalised to any reader&apos;s circumstances, objectives or risk
          tolerance.
        </p>
        <p>
          PMVL is not a registered investment adviser, broker-dealer, futures
          commission merchant or exchange in any jurisdiction. Nothing on this
          site creates an advisory or fiduciary relationship. If you need advice
          about whether a particular position is appropriate for you, consult a
          licensed professional.
        </p>
      </LegalSection>

      <LegalSection id="loss" title="Prediction markets involve the risk of loss">
        <p>
          Prediction-market contracts settle to a fixed value. A contract that
          resolves against you is worth nothing, and the entire amount paid for
          it is lost. There is no partial recovery, no stop-loss that guarantees
          an exit and no protection against an adverse resolution.
        </p>
        <p>
          Losses can exceed what a reader expects even when a probability
          estimate is well calibrated: a position with a 90% estimated chance of
          resolving in your favour is still expected to fail one time in ten, and
          those failures are not evenly spaced. Never commit money you cannot
          afford to lose entirely.
        </p>
      </LegalSection>

      <LegalSection id="estimates" title="The estimates can be wrong">
        <p>
          PMVL&apos;s probability estimates are model output. Models are built on
          assumptions that can be wrong, on historical relationships that can
          break, and on data that can be incomplete or mistaken. The uncertainty
          intervals the site publishes describe the model&apos;s own view of its
          precision; they do not bound how wrong it can be, and a model can be
          confidently wrong.
        </p>
        <p>
          Coverage is partial by design. Many markets have no independent
          estimate at all, and those markets are shown on the watchlist rather
          than ranked. An absent estimate is not a neutral one — it means PMVL
          has no informed opinion about that contract.
        </p>
      </LegalSection>

      <LegalSection id="stale" title="Prices and quotes may be stale">
        <p>
          The hosted deployment serves a frozen research snapshot rather than a
          continuous live feed. Every price, spread, depth figure and model
          estimate on this site was observed at some point in the past, and the
          time of that observation is displayed on each page. Some quotes in a
          snapshot are considerably older than the freshest one shown.
        </p>
        <p>
          Prediction-market prices move, sometimes sharply, in response to news
          that a snapshot cannot contain. A price shown here may no longer exist,
          and an apparent difference between a model estimate and a displayed
          price may have closed, reversed or never have been executable at all.
          Always verify current prices on the venue before acting.
        </p>
      </LegalSection>

      <LegalSection id="liquidity" title="Liquidity may be insufficient">
        <p>
          A quoted price is only reachable for the size resting behind it. Many
          prediction-market contracts have thin order books in which a modest
          order moves the price materially, and a position that is easy to open
          can be difficult or impossible to close before resolution.
        </p>
        <p>
          PMVL prices against the visible ask ladder at a reference size and
          filters thin books out, but the depth it saw was itself a snapshot.
          Displayed depth may not be there when you look, and it may be withdrawn
          faster than an order can reach it.
        </p>
      </LegalSection>

      <LegalSection id="rules" title="Contract rules and settlement decide outcomes">
        <p>
          What a prediction-market contract pays is determined by its written
          settlement rules and by whoever the venue designates to interpret them —
          not by the plain-language reading of its title. Two contracts that
          appear to ask the same question can settle differently because they use
          different sources, different cut-off times or different definitions of
          the event.
        </p>
        <p>
          Rules can be amended, sources can be unavailable or revised, resolution
          can be delayed or disputed, and a venue can void a market. PMVL
          normalises rules as carefully as it can and declines to treat two
          contracts as equivalent when their rules disagree, but a rules-based
          outcome that surprises the market is always possible and is not a
          failure the model can anticipate.
        </p>
      </LegalSection>

      <LegalSection id="backtests" title="Backtests and demos are not live performance">
        <p>
          Backtest results, demo datasets and guided walkthroughs on this site are
          simulations. They are labelled as such wherever they appear. Simulated
          results are prepared with the benefit of hindsight, do not involve real
          capital, do not experience real fills, and do not reflect the effect
          that trading itself would have had on the prices being traded.
        </p>
        <p>
          <strong>Past results do not guarantee future results.</strong> A record
          of profitable historical recommendations is not evidence that future
          recommendations will be profitable, and a model that forecast well in
          one period can forecast badly in the next.
        </p>
      </LegalSection>

      <LegalSection id="execution" title="PMVL does not execute trades">
        <p>
          PMVL is read-only. It holds no funds, custodies no assets, stores no
          wallet keys or exchange credentials, and places no orders. It has no
          execution access to Kalshi, Polymarket or any other venue. Trading
          execution is disabled in the runtime, and the{" "}
          <Link href="/system" className="underline underline-offset-2">
            system page
          </Link>{" "}
          reports that state so it can be checked rather than taken on trust.
        </p>
        <p>
          Any position you take is one you open yourself, on a venue with which
          you have your own relationship, subject to that venue&apos;s terms. You
          are responsible for determining whether you are eligible to trade there
          under the laws that apply to you.
        </p>
      </LegalSection>

      <LegalSection id="eligibility" title="Eligibility and jurisdiction">
        <p>
          Prediction markets are regulated differently across jurisdictions, and
          in some they are restricted or prohibited. Availability of information
          on this site is not an indication that trading is lawful where you are.
          Verify your own eligibility with each venue and under your own local
          law before trading anywhere.
        </p>
      </LegalSection>

      <LegalSection id="no-guarantees" title="What is never claimed here">
        <p>
          PMVL does not claim, and no page on this site should be read as
          claiming, guaranteed returns, winning trades, risk-free positions,
          accurate profits, an ability to beat the market, sure opportunities or
          access to institutional secrets. Any material that appears to make such
          a claim is wrong and should be reported to{" "}
          <a
            href={`mailto:${SUPPORT_EMAIL}`}
            className="underline underline-offset-2"
          >
            {SUPPORT_EMAIL}
          </a>
          .
        </p>
      </LegalSection>
    </LegalPage>
  );
}
