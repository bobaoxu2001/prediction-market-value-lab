import Link from "next/link";

/**
 * Framing for the authentication pages.
 *
 * Clerk's own components are used unmodified inside it. Restyling their internals
 * would mean re-implementing the focus management, the field labelling and the
 * error summaries they already ship correctly, and doing that badly is the
 * ordinary way a sign-in form becomes unusable with a keyboard or a screen
 * reader. The page around them is ours; the form is theirs.
 */
export function AuthShell({
  title,
  lead,
  children,
}: {
  title: string;
  lead: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto max-w-xl px-4 pb-20 pt-14">
      <h1 className="t-page-title">{title}</h1>
      <p className="t-prose mt-3">{lead}</p>
      <div className="mt-8 flex justify-center">{children}</div>
    </div>
  );
}

/**
 * The state a Preview deployment is in before the owner has created a Clerk
 * project.
 *
 * This is deliberately not a mock sign-in. A fake authenticated session exposed
 * on a public Preview URL is a worse outcome than no authentication at all - it
 * would put the account surfaces, and any entitlement they imply, behind a door
 * that anyone can open. The page states the situation and sends the visitor back
 * to the research, which needs no account.
 */
export function AuthUnavailable({ reason }: { reason?: string }) {
  const cameFromProtectedRoute = reason === "auth-unavailable";

  return (
    <div className="mx-auto max-w-xl px-4 pb-20 pt-14">
      <h1 className="t-page-title">Accounts are not enabled here</h1>
      <p className="t-prose mt-3">
        {cameFromProtectedRoute
          ? "The page you asked for requires an account, and this deployment has no authentication provider configured. Rather than show you an account page with nobody signed in, the request was redirected here."
          : "This deployment has no authentication provider configured, so there is nothing to sign in to yet."}
      </p>
      <p className="t-prose mt-3">
        Nothing is missing from the research product as a result. It is public,
        it has never required an account, and it is unaffected by whether
        authentication is switched on.
      </p>

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <Link href="/app" className="btn-primary">
          Explore research
        </Link>
        <Link href="/" className="btn-quiet">
          Back to the homepage
        </Link>
      </div>

      <p className="mt-8 border-t border-line pt-6 t-meta">
        For the deployment owner: authentication activates once{" "}
        <span className="font-mono">NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY</span> and{" "}
        <span className="font-mono">CLERK_SECRET_KEY</span> are set. See{" "}
        <span className="font-mono">docs/saas-setup.md</span> for the full
        checklist. No credential is ever committed to the repository.
      </p>
    </div>
  );
}
