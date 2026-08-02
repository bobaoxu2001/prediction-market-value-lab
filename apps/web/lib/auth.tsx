import { ClerkProvider } from "@clerk/nextjs";

/**
 * Authentication wiring that tolerates an unconfigured deployment.
 *
 * A Preview build has to be reviewable before the owner has created a Clerk
 * project. If `<ClerkProvider>` were mounted unconditionally it would throw on
 * every render for a missing publishable key, and the *public* marketing site -
 * the thing most in need of review - would be the first casualty. So the
 * provider is mounted only when a key exists, and every consumer treats
 * "unconfigured" as "signed out", never as "signed in".
 *
 * This file is deliberately free of any secret. It reads the publishable key
 * only, which is public by design; the secret key is confined to
 * `lib/auth-server.ts`, which is `server-only`.
 */

/** The publishable key, or null. Safe to evaluate on the client. */
export function clerkPublishableKey(): string | null {
  const key = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
  return key && key.length > 0 ? key : null;
}

/**
 * Whether the browser half of Clerk can run.
 *
 * Not a security boundary. The server-side check in `lib/auth-server.ts` is the
 * one that decides whether a request is authenticated; this only decides whether
 * to render Clerk's UI.
 */
export function isAuthUiConfigured(): boolean {
  return clerkPublishableKey() !== null;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  if (!isAuthUiConfigured()) return <>{children}</>;
  return (
    <ClerkProvider
      // Clerk's own appearance API rather than overriding its internals: the
      // components keep their focus management, labelling and error summaries,
      // and only the palette follows PMVL's tokens.
      appearance={{
        variables: {
          colorPrimary: "#1f5c8b",
          borderRadius: "2px",
          fontFamily: "var(--font-sans)",
        },
      }}
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
    >
      {children}
    </ClerkProvider>
  );
}
