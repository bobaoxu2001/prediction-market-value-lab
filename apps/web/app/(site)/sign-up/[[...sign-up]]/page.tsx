import type { Metadata } from "next";
import Link from "next/link";
import { SignUp } from "@clerk/nextjs";

import { AuthShell, AuthUnavailable } from "@/components/auth-shell";
import { isAuthUiConfigured } from "@/lib/auth";
import { absoluteUrl } from "@/lib/site";

export const metadata: Metadata = {
  title: "Create an account",
  description:
    "Create a free PMVL account. The research product is public and free; an account is only needed for account and subscription management.",
  alternates: { canonical: absoluteUrl("/sign-up") },
  robots: { index: false, follow: false },
};

export default async function SignUpPage({
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
      title="Create a free account"
      lead={
        <>
          Free, and it unlocks nothing that is currently paywalled — the research
          is public either way. By creating an account you agree to the{" "}
          <Link href="/terms" className="underline underline-offset-2">
            terms
          </Link>
          , the{" "}
          <Link href="/privacy" className="underline underline-offset-2">
            privacy notice
          </Link>{" "}
          and the{" "}
          <Link href="/risk-disclosure" className="underline underline-offset-2">
            risk disclosure
          </Link>
          .
        </>
      }
    >
      <SignUp
        routing="path"
        path="/sign-up"
        signInUrl="/sign-in"
        fallbackRedirectUrl="/account"
      />
    </AuthShell>
  );
}
