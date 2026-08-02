# PR #12 — credentialed-review screenshots

Captured from the Vercel Preview at commit `13c6e9a`, against the API Preview at
the same commit. Headless Chrome, `deviceScaleFactor: 2`, downscaled to 1500px.

**Sanitised by construction.** This Preview has no Clerk and no Stripe
credentials configured, so there is no session, no customer, no card and no
personal data anywhere in these images. Nothing has been redacted because
nothing needed redacting.

| File | What it evidences |
| --- | --- |
| `home-light-1440.png`, `home-dark-1440.png` | Marketing homepage, both themes, live API figures |
| `pricing-light-1440.png`, `pricing-dark-1440.png` | Pricing with billing disabled; `h1` now present |
| `arbitrage-diagnostics-390.png`, `arbitrage-diagnostics-375-dark.png` | The mobile-overflow fix: this route scrolled the document sideways (413px against 375) before the table was wrapped |
| `app-briefing-light-1440.png` | `/app` research briefing, unchanged |
| `system-dark-1440.png` | Snapshot and pipeline transparency |
| `sign-in-unconfigured-1440.png` | Honest "accounts are not enabled" state — not a mock session |
| `account-redirect-1440.png` | `/account` failing closed to `/sign-in?reason=auth-unavailable` |
| `risk-disclosure-1440.png`, `terms-draft-banner-1440.png` | Legal drafts with the "not reviewed by counsel" banner |

Screenshots of a signed-in account, an active test subscription, the billing
page with a real customer and the Stripe Customer Portal **cannot be produced**:
they require Clerk and Stripe test credentials, which do not exist on any
environment of this project. See the PR description for the exact list.

The earlier set in `docs/screenshots/pr-12/` was captured at `615933d`, before
the review fixes.
