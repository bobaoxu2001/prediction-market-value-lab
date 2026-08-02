import { SiteFooter, SiteHeader } from "@/components/site-nav";
import { getCurrentEntitlement } from "@/lib/billing/entitlement";

/**
 * The public site shell: marketing, pricing, legal, authentication and account.
 *
 * One shell for all of them, because they are one funnel. The header changes
 * what it offers based on the session, and that decision is made here, on the
 * server, once per request.
 */
export default async function SiteLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const entitlement = await getCurrentEntitlement();
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader entitlement={entitlement} />
      {/* `min-w-0` so a wide child (a code block, a long token) scrolls inside
          its own container instead of widening the document. */}
      <main id="main" className="min-w-0 flex-1">
        {children}
      </main>
      <SiteFooter />
    </div>
  );
}
