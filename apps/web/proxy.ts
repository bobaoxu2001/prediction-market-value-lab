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

/**
 * Percent-encoded path separators, refused outright.
 *
 * Two reasons, one concrete and one structural.
 *
 * Concrete: on Vercel, `GET /account%2Fbilling` produced a **500**. The encoded
 * slash matches no App Router route, so the request fell through to the
 * platform's page launcher, which `require()`d a module named literally
 * `pages/account%2Fbilling.js` and threw MODULE_NOT_FOUND. Every other encoded
 * path 404s; only a real route prefix reached the launcher. `next start` returns
 * 404 for the same request, so this is reachable only on the deployed platform -
 * which is exactly the kind of defect a local test cannot find.
 *
 * Structural: protection here is decided by path prefix (`isProtectedPath`).
 * Anything that lets one path be spelled two ways is a bad thing to have
 * anywhere near that decision, whichever layer happens to decode first. A
 * request is either `/account/billing` or it is not, and no legitimate route on
 * this site carries `%2F` or `%5C` in a path segment.
 *
 * The query string is untouched: encoded slashes are ordinary there.
 */
const ENCODED_SEPARATOR = /%2f|%5c/i;

function hasEncodedSeparator(request: NextRequest): boolean {
  // Both spellings are checked because it is not guaranteed which layer
  // normalises first: `nextUrl.pathname` may already be decoded while the raw
  // request URL still carries the escape, or the reverse.
  if (ENCODED_SEPARATOR.test(request.nextUrl.pathname)) return true;
  try {
    return ENCODED_SEPARATOR.test(new URL(request.url).pathname);
  } catch {
    return false;
  }
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
  // Before anything else, including Clerk: a path that cannot be spelled
  // unambiguously does not get to reach a route-matching decision.
  if (hasEncodedSeparator(request)) {
    return new NextResponse(null, { status: 404 });
  }

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
