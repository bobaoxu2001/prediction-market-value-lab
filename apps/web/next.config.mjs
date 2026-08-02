/**
 * Security headers.
 *
 * Only the three that are provider-agnostic. Each was chosen because it can be
 * verified *now*, without Clerk or Stripe credentials — a header added blind and
 * discovered to be broken during a payment flow is worse than no header.
 *
 * Deliberately NOT set here: `script-src`, `connect-src`, `frame-src`,
 * `img-src`, `style-src`, `worker-src`. Those are the directives that break
 * Clerk and Stripe when guessed, and their correct values depend on the Clerk
 * instance's own frontend-API domain, which does not exist until the owner
 * creates the application. Clerk ships a generator for exactly this
 * (`@clerk/nextjs/server`'s content-security-policy helper, surfaced through
 * `clerkMiddleware({ contentSecurityPolicy })`); that is the right tool, and it
 * should be turned on in report-only mode first, against a Preview that has
 * real credentials. Tracked in docs/saas-setup.md §9.
 */
const SECURITY_HEADERS = [
  {
    // Stops a browser from second-guessing a Content-Type. Nothing this app or
    // either provider serves relies on sniffing.
    key: "X-Content-Type-Options",
    value: "nosniff",
  },
  {
    // The concrete leak this closes: `/account/billing?checkout=complete&
    // session_id=cs_test_…` is a real URL a visitor lands on, and without this
    // the full query — including the Checkout Session ID — rides along in the
    // `Referer` of any outbound link they click next. Modern browsers already
    // default to this value; stating it removes the dependency on that default.
    key: "Referrer-Policy",
    value: "strict-origin-when-cross-origin",
  },
  {
    // `frame-ancestors` only — a CSP header carrying this single directive
    // constrains who may embed *us* and places no restriction whatsoever on
    // what we load, so it cannot break a provider.
    //
    // `'self'` rather than `'none'`: the protection against a third-party
    // clickjacking frame is identical (an attacker cannot serve from our
    // origin), and it leaves room for any same-origin embedding without needing
    // a credentialed Preview to prove the negative first.
    key: "Content-Security-Policy",
    value: "frame-ancestors 'self'",
  },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The API base is read at build time for server components and injected into the
  // client bundle for client components.
  env: { NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000" },
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
};
export default nextConfig;
