import type { Metadata } from "next";
import Link from "next/link";
import { SignIn } from "@clerk/nextjs";

import { AuthUnavailable, AuthShell } from "@/components/auth-shell";
import { isAuthUiConfigured } from "@/lib/auth";
import { absoluteUrl } from "@/lib/site";

export const metadata: Metadata = {
  title: "Sign in",
  description:
    "Sign in to your PMVL account. The research product is public and does not require an account.",
  alternates: { canonical: absoluteUrl("/sign-in") },
  // A login page has no business in a search index.
  robots: { index: false, follow: false },
};

export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string }>;
}) {
  const { reason } = await searchParams;

  if (!isAuthUiConfigured()) {
    return <AuthUnavailable reason={reason} />;
  }

  return (
    <AuthShell
      title="Sign in"
      lead={
        <>
          An account is not needed to read the research — everything under{" "}
          <Link href="/app" className="underline underline-offset-2">
            /app
          </Link>{" "}
          is public. Sign in to manage your account and subscription.
        </>
      }
    >
      <SignIn
        // Rendered as the page rather than as a modal so the URL, the back
        // button and a bookmarked link all behave the way a visitor expects.
        routing="path"
        path="/sign-in"
        signUpUrl="/sign-up"
        fallbackRedirectUrl="/account"
      />
    </AuthShell>
  );
}
