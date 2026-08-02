import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";
import type { NextFetchEvent } from "next/server";

import { isAlwaysOpenPath, isProtectedPath } from "@/lib/route-policy";

/**
 * Request proxy. (Next 16's replacement for `middleware.ts`.)
 *
 * Its job is to run Clerk's request handler so `auth()` works in server
 * components, and to send a signed-out visitor to the sign-in page instead of a
 * blank account screen. That redirect is a *convenience*, not the security
 * boundary: path matching in a proxy can diverge from how Next actually routes a
 * request, so `/account` and `/account/billing` each re-check the session
 * themselves and refuse to render without one, and every billing route handler
 * authenticates independently. Losing this file would make the app less
 * pleasant, not less safe.
 *
 * Two properties it must preserve:
 *
 *   1. The research product is public and must stay public. Nothing under
 *      `/app`, `/markets`, `/arbitrage` and friends is gated, and a
 *      misconfigured auth provider must not be able to gate them.
 *   2. `/account` fails *closed*. On a deployment with no Clerk credentials the
 *      account pages are unreachable, not unprotected.
 */

function authConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
  );
}

const withClerk = clerkMiddleware(async (auth, request) => {
  const { pathname } = request.nextUrl;
  if (isAlwaysOpenPath(pathname) || !isProtectedPath(pathname)) {
    return NextResponse.next();
  }

  const { userId, redirectToSignIn } = await auth();
  if (!userId) {
    // Clerk builds this URL from its own configuration rather than from the
    // request, so it cannot be pointed off-site.
    return redirectToSignIn({ returnBackUrl: request.url });
  }
  return NextResponse.next();
});

export default function proxy(request: NextRequest, event: NextFetchEvent) {
  if (!authConfigured()) {
    if (isProtectedPath(request.nextUrl.pathname)) {
      const url = new URL("/sign-in", request.url);
      url.searchParams.set("reason", "auth-unavailable");
      return NextResponse.redirect(url);
    }
    return NextResponse.next();
  }
  return withClerk(request, event);
}

export const config = {
  matcher: [
    // Everything except Next internals and static assets, plus API routes.
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
