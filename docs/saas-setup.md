# SaaS layer setup: accounts, entitlements and test billing

Everything the owner has to do to turn the public site, authentication and
test-mode billing on — and the checklist that must be complete before anyone is
charged for real.

**No secret value appears in this document, in the repository, or in any log,
build output, screenshot or pull-request description.** Only variable *names*
are written down. If you find a value anywhere in this repository, treat it as
compromised and rotate it.

---

## 0. What is already true without any setup

The Preview and production deployments work today with none of the variables
below set:

- the public marketing homepage, `/pricing` and the three legal pages render;
- the whole research product (`/app`, `/markets`, `/arbitrage`, `/backtest`,
  `/track-record`, `/methodology`, `/system`, `/demo`, `/case-study`,
  `/market/[id]`) is public and unchanged;
- `/sign-in` and `/sign-up` render an honest "accounts are not enabled here"
  page;
- `/account` and `/account/billing` redirect to `/sign-in` — they fail *closed*,
  not open;
- billing is disabled at the server, so no checkout can be created.

Nothing below is required to review the pull request. It is required to
demonstrate the authentication and billing flows in Preview.

---

## 1. Clerk

### 1.1 Create the project

1. Sign in at <https://dashboard.clerk.com> and create an application named
   `PMVL` (or the final product name once decided — see the owner checklist).
2. Under **Configure → Email, Phone, Username**, enable **Email address** and
   **Password**. Email verification code is recommended.
3. Under **Configure → SSO connections**, enable **Google**.
   - Clerk's shared development credentials work immediately for a development
     instance and are fine for Preview.
   - A production instance needs your own Google OAuth client: create one in the
     Google Cloud console, set the authorised redirect URI to the value Clerk
     shows on that screen, and paste the client ID and secret into Clerk. Do not
     put them in this repository.
4. Under **Configure → Paths**, set:
   - Sign-in URL `/sign-in`
   - Sign-up URL `/sign-up`
   - After sign-in / after sign-up: `/account`

### 1.2 Keys

From **Configure → API keys**, copy the **development** (test) keys:

| Variable                             | Where it goes            | Notes                                     |
| ------------------------------------ | ------------------------ | ----------------------------------------- |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`  | Vercel `pmvl-web`, local | Public by design; ships in the browser     |
| `CLERK_SECRET_KEY`                   | Vercel `pmvl-web`, local | **Secret.** Server only, never in a client |

Both are required together. A publishable key alone renders Clerk's UI but
cannot verify a session server-side, and the application deliberately treats
that as "not configured" rather than as half-working authentication.

### 1.3 Allowed origins

Add the Preview hostname to Clerk's allowed origins so its scripts load there.
Vercel Preview hostnames change per deployment; the stable
`pmvl-web-git-<branch>-<team>.vercel.app` alias is the one to add.

---

## 2. Stripe (test mode only)

> Do every step below with the **Test mode** toggle ON in the Stripe dashboard.
> This application refuses a live secret key outright, so a live key does not
> enable live billing — it disables billing entirely.

### 2.1 Product and prices

1. **Product catalogue → Add product**. Name it `PMVL Founding Pro`.
2. Add two recurring prices on that product:
   - monthly, `[MONTHLY PRICE]` `[BILLING CURRENCY]` / month
   - annual, `[ANNUAL PRICE]` `[BILLING CURRENCY]` / year

   Both amounts and the currency are owner decisions and are not set here.
3. Copy each price ID (`price_…`). These are configuration, not secrets, but
   they still belong in environment variables rather than in source: the
   server-side allowlist is the only thing standing between a client and an
   arbitrary price.

### 2.2 Webhook

1. **Developers → Webhooks → Add endpoint.**
2. URL: `https://<your-preview-or-production-host>/api/stripe/webhook`
3. Events to send:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
4. Copy the signing secret (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`.

The webhook secret is **required** for billing to enable at all. A deployment
that can create a subscription but cannot verify the events that confirm,
suspend or revoke it would grant access it could never take back.

### 2.3 Customer Portal

**Settings → Billing → Customer portal**, in test mode:

- turn the portal **on**;
- allow customers to **cancel subscriptions** (immediately or at period end —
  the application handles both);
- allow **payment method updates** and **invoice history**;
- set the business name and the links to your terms and privacy pages;
- do **not** enable plan switching until more than one live plan exists.

### 2.4 Local development with the Stripe CLI

```bash
stripe login
stripe listen --forward-to localhost:3000/api/stripe/webhook
```

`stripe listen` prints its own signing secret. Use *that* value as
`STRIPE_WEBHOOK_SECRET` locally — it differs from the dashboard endpoint's.

Trigger events without a browser:

```bash
stripe trigger checkout.session.completed
stripe trigger customer.subscription.updated
stripe trigger invoice.payment_failed
```

### 2.5 Test cards

Stripe's published test cards, any future expiry, any CVC, any postcode:

| Card                  | Behaviour                     |
| --------------------- | ----------------------------- |
| `4242 4242 4242 4242` | succeeds                      |
| `4000 0025 0000 3155` | requires 3-D Secure           |
| `4000 0000 0000 9995` | declined (insufficient funds)  |
| `4000 0000 0000 0341` | attaches, then fails on charge |

Never use a real card. Never switch the dashboard out of test mode while this
deployment is configured.

---

## 3. Environment variables

### 3.1 The full set

| Variable                            | Secret | Production                | Preview (demo) | Local        |
| ----------------------------------- | :----: | ------------------------- | -------------- | ------------ |
| `NEXT_PUBLIC_API_BASE`              |   no   | research API origin       | same           | `http://localhost:8000` |
| `NEXT_PUBLIC_SITE_URL`              |   no   | public origin, no path    | leave unset    | leave unset  |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` |   no   | test key                  | test key       | test key     |
| `CLERK_SECRET_KEY`                  | **yes**| test key                  | test key       | test key     |
| `NEXT_PUBLIC_BILLING_ENABLED`       |   no   | `false`                   | `true`         | `true`       |
| `BILLING_MODE`                      |   no   | `disabled`                | `test`         | `test`       |
| `STRIPE_SECRET_KEY`                 | **yes**| **unset**                 | `sk_test_…`    | `sk_test_…`  |
| `STRIPE_WEBHOOK_SECRET`             | **yes**| **unset**                 | `whsec_…`      | CLI value    |
| `STRIPE_PRO_MONTHLY_PRICE_ID`       |   no   | **unset**                 | test price ID  | test price ID|
| `STRIPE_PRO_ANNUAL_PRICE_ID`        |   no   | **unset**                 | test price ID  | test price ID|

### 3.2 The safe production state

```text
NEXT_PUBLIC_BILLING_ENABLED=false
BILLING_MODE=disabled
```

with no Stripe variables set at all. This is the state the pull request ships
and the state production must stay in until section 5 is complete.

### 3.3 The Preview demonstration state

```text
NEXT_PUBLIC_BILLING_ENABLED=true
BILLING_MODE=test
```

plus the four Stripe test values. Set these on the **Preview** environment only,
in the Vercel dashboard for `pmvl-web`. Do not copy them to Production.

### 3.4 Local

Create `apps/web/.env.local` — it is gitignored, and the repository-wide secret
scan fails the build if a `.env` file is ever tracked. Copy the variable names
from the table above and fill in your own values.

---

## 4. The rule that governs all of this

> **Setting a public UI flag alone must never activate billing.**

`NEXT_PUBLIC_BILLING_ENABLED` is inlined into the browser bundle, so anyone
holding the JavaScript can flip it in their own copy. It decides only whether
checkout buttons render.

Whether a Checkout Session can be created is decided server-side in
`apps/web/lib/billing/config.ts`, which requires **all** of:

1. `BILLING_MODE=test` exactly;
2. `STRIPE_SECRET_KEY` present **and** starting `sk_test_` / `rk_test_` — a live
   key is *rejected*, not honoured;
3. `STRIPE_WEBHOOK_SECRET` present;
4. both allowlisted price IDs present.

There is no configuration of that file that permits a live charge. `npm run test`
asserts each clause.

---

## 5. Live-launch checklist

Do not set a live Stripe key until every line is true.

- [ ] Legal entity name, registered address and governing jurisdiction decided
- [ ] `/terms`, `/privacy` and `/risk-disclosure` reviewed by a lawyer
- [ ] Every `OWNER INPUT REQUIRED` placeholder resolved (`docs/legal-placeholders.md`)
- [ ] The "not reviewed by counsel" banner removed from `components/legal.tsx`
      — deliberately a code change, so it cannot happen by accident
- [ ] Refund policy written and reflected in the terms
- [ ] Data retention policy written and reflected in the privacy notice
- [ ] Support email live and monitored, and substituted for the placeholder
- [ ] Billing currency, monthly price and annual price decided
- [ ] The Pro tier actually delivers something a subscriber would recognise as
      value — nothing on the pricing page currently promises a feature that
      exists
- [ ] Stripe account activated, business details and tax settings complete
- [ ] Live product and prices created; live price IDs recorded
- [ ] Live webhook endpoint created against the production URL, with its own
      signing secret
- [ ] Customer Portal configured in live mode
- [ ] Clerk production instance created with your own Google OAuth credentials
- [ ] A full test-mode lifecycle passes in Preview (section 6)
- [ ] Owner approval recorded

Then, and only then: a `BILLING_MODE` value that permits live keys has to be
*added to the code* — `config.ts` currently accepts only `disabled` and `test`.
That is intentional. Going live is a reviewed code change, not an environment
variable someone can set at 2am.

---

## 6. Verifying a full test lifecycle in Preview

With Clerk and Stripe test credentials set on Preview:

1. Open the Preview URL, sign up with email or Google.
2. Go to `/pricing` → **Start monthly test checkout**.
3. Pay with `4242 4242 4242 4242`.
4. Confirm the webhook delivered (Stripe dashboard → Webhooks → the endpoint).
5. `/account` shows **Pro — active**; `/account/billing` shows the period end.
6. **Open billing portal** → cancel.
7. `/account` shows **Pro — cancels at period end**, and access continues.
8. In Stripe, cancel the subscription immediately; `/account` shows
   **Pro — cancelled**.
9. `stripe trigger invoice.payment_failed` → `/account` shows
   **Pro — payment failed** and Pro access is withheld.

Throughout, the research pages must stay reachable and unchanged.

---

## 7. Verifying no secret reaches the browser

`npm run test` scans a real `next build` output for secret *values*. To prove it
end to end, build with canary values and re-run the scan:

```bash
cd apps/web
CLERK_SECRET_KEY=sk_test_canary_clerk_zzz \
STRIPE_SECRET_KEY=sk_test_canary_stripe_zzz \
STRIPE_WEBHOOK_SECRET=whsec_canary_hook_zzz \
BILLING_MODE=test NEXT_PUBLIC_BILLING_ENABLED=true \
npm run build
PMVL_BUNDLE_CANARY=canary npx vitest run tests/security.test.ts
```

Choose a canary string that does **not** also appear in your publishable key —
the publishable key is expected in the bundle and would otherwise look like a
false positive. Delete `.next` afterwards so no canary build is deployed.

---

## 8. Rollback

Nothing here changes the snapshot, the pipeline, the models or the API, so a
rollback is confined to the web application.

**Turn billing off immediately** (no deploy needed):

1. Vercel → `pmvl-web` → Settings → Environment Variables.
2. Set `BILLING_MODE=disabled` and `NEXT_PUBLIC_BILLING_ENABLED=false`.
3. Redeploy the environment. Checkout and the portal return 503; the account
   pages show "billing not yet live"; research is untouched.

**Turn accounts off**: unset `CLERK_SECRET_KEY`. `/sign-in` and `/sign-up`
explain the state, `/account` redirects, and the public site keeps working.

**Revert the whole feature**: `git revert` the merge commit. The research routes
return to `/` and their previous layout; no data migration is involved, because
this layer stores nothing of its own — entitlement state lives in Clerk private
metadata and the source of truth lives in Stripe.

---

## 9. Content Security Policy

No CSP is set by this application today, and none is added here: adding one
blind to Clerk's and Stripe's requirements is the fastest way to break a
checkout that was working. When one is introduced it must include at least:

```text
script-src   'self' https://*.clerk.accounts.dev https://*.clerk.com https://js.stripe.com
connect-src  'self' https://*.clerk.accounts.dev https://*.clerk.com https://api.stripe.com
frame-src    https://js.stripe.com https://hooks.stripe.com https://challenges.cloudflare.com
img-src      'self' data: https://img.clerk.com
worker-src   'self' blob:
```

Clerk publishes a helper that generates the current directive set; prefer it
over this list, which will age. Do not weaken an existing header to make an
integration work — fix the directive instead.
