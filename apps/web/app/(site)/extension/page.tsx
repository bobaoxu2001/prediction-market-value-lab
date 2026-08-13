import type { Metadata } from "next";
import Link from "next/link";

import { ProductShot, Section, SectionHeading } from "@/components/marketing";
import { chromeWebStoreUrl } from "@/lib/extension-distribution";
import { absoluteUrl } from "@/lib/site";

export const metadata: Metadata = {
  title: "Live entry-cost overlay for Kalshi and Polymarket",
  description:
    "A read-only Chrome beta that estimates entry cost, break-even probability, and lower-cost order sizes from public Kalshi and Polymarket books.",
  alternates: { canonical: absoluteUrl("/extension") },
};

const DOWNLOAD = "/downloads/pmvl-entry-cost-beta.zip";

export default function ExtensionPage() {
  const storeUrl = chromeWebStoreUrl();

  return (
    <>
      <section className="mx-auto grid max-w-6xl gap-10 px-4 pb-14 pt-16 sm:pt-24 lg:grid-cols-[0.85fr_1.15fr] lg:items-center">
        <div>
          <p className="t-label">
            {storeUrl ? "Chrome Web Store beta" : "Developer-mode Chrome beta"}
          </p>
          <h1 className="mt-3 text-[2rem] leading-[1.12] sm:text-[2.75rem]">
            Live entry cost, where the order is placed.
          </h1>
          <p className="t-lead mt-5">
            PMVL reads the public order book behind an open Kalshi or Polymarket
            contract and prices the size on the ticket. See estimated cost per
            contract, break-even probability, and whether another placeable size
            costs less — before an order is submitted.
          </p>
          <div className="mt-7 flex flex-wrap items-center gap-3">
            <InstallLink storeUrl={storeUrl} />
            <Link href="/cost" className="btn-quiet">
              Try the snapshot calculator
            </Link>
          </div>
          <p className="t-meta mt-4 max-w-xl">
            {storeUrl
              ? "Install once from the Chrome Web Store and receive automatic updates. The extension places no orders and asks for no wallet, account, or browser-storage access."
              : "Manual install for Chrome desktop. This beta is not reviewed or distributed by the Chrome Web Store. It places no orders and asks for no wallet, account, or browser-storage access."}
          </p>
        </div>

        <ProductShot
          src="/product/live-entry-cost-overlay.jpg"
          alt="A working PMVL browser-extension preview showing a Polymarket YES order at the user's size, estimated cost per contract, premium over the quote, break-even probability, a cheaper placeable size, quote freshness, and read-only research disclosures."
          width={1280}
          height={720}
          priority
        />
      </section>

      <Section className="bg-sunken">
        <SectionHeading
          eyebrow="One job, in context"
          title="From a quoted price to the cost of your order"
          lead="The venue's displayed price is not necessarily the ask a buyer pays, and the order size can change both depth impact and per-contract fees. The overlay keeps the calculation beside the ticket instead of asking you to copy a market into another tab."
        />
        <div className="mt-8 grid gap-6 md:grid-cols-3">
          <Value
            number="01"
            title="Reads your side and size"
            body="YES and NO are priced separately. Dollar-denominated order forms are converted to contract counts before calculation."
          />
          <Value
            number="02"
            title="Explains the stack"
            body="Observed ask depth and published venue rules are separated from the disclosed bridge/gas and annual capital-cost scenario inputs."
          />
          <Value
            number="03"
            title="Suggests only arithmetic"
            body="It may show a cheaper placeable size. It never supplies a forecast, tells you to trade, changes the ticket, or submits an order."
          />
        </div>
      </Section>

      <Section>
        <div className="grid gap-12 lg:grid-cols-2">
          <div>
            <SectionHeading
              eyebrow="Install"
              title={storeUrl ? "Install once from Chrome" : "Load the beta in three steps"}
              lead={
                storeUrl
                  ? "The reviewed store package installs in one click and receives automatic updates from Chrome. After installation, PMVL opens a private, packaged first-run guide."
                  : "The ZIP contains the same built files covered by the extension package tests. Keep the extracted folder after installation; Chrome loads the extension from that folder."
              }
            />
            <ol className="mt-6 space-y-5">
              {storeUrl ? (
                <>
                  <Step number="1" title="Add PMVL to Chrome">
                    Use the store button above and confirm the narrow venue access
                    shown by Chrome.
                  </Step>
                  <Step number="2" title="Follow the private setup guide">
                    Chrome opens a guide stored inside the extension. No install event
                    is sent to a PMVL server.
                  </Step>
                  <Step number="3" title="Open one contract">
                    Select a Kalshi or Polymarket contract and enter an amount to see
                    the live overlay.
                  </Step>
                </>
              ) : (
                <>
                  <Step number="1" title="Download and unzip">
                    Download the beta ZIP above, then extract it to a folder you will keep.
                  </Step>
                  <Step number="2" title="Open Chrome Extensions">
                    Go to <span className="font-mono">chrome://extensions</span> and turn
                    on Developer mode.
                  </Step>
                  <Step number="3" title="Load the extracted folder">
                    Choose “Load unpacked”, select the extracted folder, then open a
                    single-contract Kalshi or Polymarket page.
                  </Step>
                </>
              )}
            </ol>
          </div>

          <div>
            <SectionHeading
              eyebrow="Trust boundary"
              title="Read-only by construction"
              lead="The useful product behavior does not require PMVL credentials or a PMVL server."
            />
            <ul className="mt-6 space-y-3 t-body">
              <li>
                <strong>Venue access:</strong> reads the open contract page and the
                three public Kalshi/Polymarket API hosts declared in the package.
              </li>
              <li>
                <strong>No general browser permission:</strong> the manifest requests
                no storage, tabs, clipboard, identity, wallet, or trading permission.
              </li>
              <li>
                <strong>Local calculation:</strong> cost math runs in the extension;
                it sends no order size or browsing history to a PMVL backend.
              </li>
              <li>
                <strong>Known limits:</strong> public books can be stale or incomplete,
                page markup can change, and taxes, exit costs, and future slippage are
                not settled by this estimate.
              </li>
            </ul>
            <div className="mt-6 flex flex-wrap gap-3">
              <Link href="/methodology" className="btn-quiet">
                Read the methodology
              </Link>
              <Link href="/risk-disclosure" className="btn-quiet">
                Read the risk disclosure
              </Link>
            </div>
          </div>
        </div>
      </Section>

      <Section id="first-result" className="bg-sunken">
        <SectionHeading
          eyebrow="First success"
          title="See the first live result in under five minutes"
          lead="Installation is not activation. The useful moment is a verified live cost beside a real order ticket—without placing an order."
        />
        <ol className="mt-8 grid gap-6 md:grid-cols-3">
          <FirstResult
            number="01"
            title="Open one contract"
            body="On a multi-outcome board, select the exact contract first so its YES/NO order ticket is visible."
          />
          <FirstResult
            number="02"
            title="Choose a side and amount"
            body="PMVL reads the visible ticket. It never types into the form, changes the amount, or requires submission."
          />
          <FirstResult
            number="03"
            title="Confirm the cost panel"
            body="Look beside the ticket for cost per contract, break-even probability, quote age, and any cheaper placeable size."
          />
        </ol>
        <p className="t-meta mt-6 max-w-2xl">
          Not visible? Refresh the market tab once after installation and select a
          specific contract. Event pages without a selected outcome intentionally
          show no estimate rather than guessing.
        </p>
      </Section>

      <Section>
        <div className="max-w-2xl">
          <p className="t-label">Beta status</p>
          <h2 className="t-page-title mt-2">
            {storeUrl ? "Useful now, distributed with automatic updates" : "Useful now, still manually installed"}
          </h2>
          <p className="t-prose mt-3">
            {storeUrl
              ? "The calculation, venue adapters, rendered panel, packaged files, and first-run guide are covered by automated tests. Venue page markup can still change, so this remains a beta even after store review."
              : "The calculation, venue adapters, rendered panel, packaged files, and first-run guide are covered by automated tests. Store review and automatic updates are not yet configured, and venue page markup can still change. That boundary is why this download is labelled a developer-mode beta."}
          </p>
          <InstallLink storeUrl={storeUrl} className="mt-6" />
        </div>
      </Section>
    </>
  );
}

function InstallLink({
  storeUrl,
  className = "",
}: {
  storeUrl: string | null;
  className?: string;
}) {
  if (storeUrl) {
    return (
      <a
        href={storeUrl}
        target="_blank"
        rel="noreferrer"
        data-pmvl-funnel="extension_install_intent"
        data-pmvl-placement="chrome_web_store"
        className={`btn-primary ${className}`}
      >
        Add to Chrome
      </a>
    );
  }

  return (
    <a
      href={DOWNLOAD}
      download
      data-pmvl-funnel="extension_install_intent"
      data-pmvl-placement="beta_zip"
      className={`btn-primary ${className}`}
    >
      Download beta ZIP
    </a>
  );
}

function Value({
  number,
  title,
  body,
}: {
  number: string;
  title: string;
  body: string;
}) {
  return (
    <div className="border-t border-line pt-4">
      <p className="t-label">{number}</p>
      <h2 className="t-sub-title mt-2">{title}</h2>
      <p className="t-body mt-2">{body}</p>
    </div>
  );
}

function Step({
  number,
  title,
  children,
}: {
  number: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <li className="grid grid-cols-[2rem_1fr] gap-3">
      <span className="font-mono text-sm text-ink-faint" aria-hidden="true">
        {number.padStart(2, "0")}
      </span>
      <div>
        <h3 className="t-sub-title">{title}</h3>
        <p className="t-body mt-1">{children}</p>
      </div>
    </li>
  );
}

function FirstResult({
  number,
  title,
  body,
}: {
  number: string;
  title: string;
  body: string;
}) {
  return (
    <li className="border-t border-line pt-4">
      <p className="t-label">{number}</p>
      <h3 className="t-sub-title mt-2">{title}</h3>
      <p className="t-body mt-2">{body}</p>
    </li>
  );
}
