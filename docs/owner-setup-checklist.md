# Owner setup checklist — Clerk and Stripe test environments

Everything in this file is a step **only you can do**. Two independent reasons:

1. **They need your credentials and your consent.** Account creation, password
   entry, email verification codes, Google OAuth consent and CAPTCHAs are
   human-only by design.
2. **Pasting an API key is not something an assistant should do.** Creating
   accounts and entering passwords, API keys or tokens into a field is
   off-limits for me regardless of who asks — so the secret values must travel
   from the provider's dashboard to Vercel by your hand, and never through a
   chat transcript.

The practical upside: **no secret ever passes through the conversation**, which
is exactly the property the security rules ask for.

Budget about 25 minutes. Everything else on PR #12 is already done and verified.

---

## Values you will need

| Thing | Value |
| --- | --- |
| Owner email | `ax2183@nyu.edu` |
| Stable Preview hostname | `pmvl-web-git-feat-public-site-auth-billi-3529a0-ao-xus-projects.vercel.app` |
| Vercel project | `pmvl-web` (team `ao-xus-projects`) |
| Branch to scope variables to | `feat/public-site-auth-billing-foundation` |

Everything below stays in **test mode**. Do not activate live payments; the
application rejects a live Stripe key outright, so a live key would disable
billing rather than enable it.

---

## 1. Clerk (about 10 minutes)

1. Go to <https://dashboard.clerk.com> and sign in or sign up with
   `ax2183@nyu.edu`. *(password + email code — human-only)*
2. **Create application** → name it `PMVL Preview`.
3. Under sign-in options, enable **Email address** with **email verification
   code**, and enable **Google**.
   - Clerk's shared development Google credentials work immediately on a
     development instance. That is sufficient for this Preview. Do **not** set
     up a custom Google Cloud OAuth client for a development instance — it is
     only needed for a production instance later.
4. **Configure → Paths**, set:

   | Field | Value |
   | --- | --- |
   | Sign-in URL | `/sign-in` |
   | Sign-up URL | `/sign-up` |
   | After sign-in | `/account` |
   | After sign-up | `/account` |

5. **Configure → Domains / allowed origins**: add exactly

   ```
   https://pmvl-web-git-feat-public-site-auth-billi-3529a0-ao-xus-projects.vercel.app
   ```

   Add the exact hostname, not a wildcard. If Clerk later rejects a specific
   deployment URL, add that exact URL too rather than broadening to `*.vercel.app`.
6. **Configure → API keys**: keep this tab open. You need the **publishable key**
   and the **secret key** in step 4 below. Both must be the **development/test**
   pair.

---

## 2. Stripe (about 10 minutes)

Do all of this with the **Test mode** toggle **ON**.

1. Go to <https://dashboard.stripe.com> and sign in or sign up with
   `ax2183@nyu.edu`. *(password + verification — human-only)*
   - If Stripe prompts to activate payments, asks for a legal entity, business
     details, tax identity or a bank account: **skip it**. None of that is needed
     for test mode, and none of it should be supplied for this task.
2. **Product catalogue → Add product**:
   - Name: `PMVL Founding Pro`
   - Add a recurring price: **USD 19.00 / month**
   - Add a second recurring price: **USD 149.00 / year**

   > These are **Preview test prices only**. They are not approved live-launch
   > prices, and they deliberately do **not** fill the `MONTHLY PRICE`,
   > `ANNUAL PRICE` or `BILLING CURRENCY` placeholders in the terms. Those stay
   > open until you decide real pricing.

   Copy both **price IDs** (`price_…`).
3. **Developers → Webhooks → Add endpoint**:
   - URL:
     ```
     https://pmvl-web-git-feat-public-site-auth-billi-3529a0-ao-xus-projects.vercel.app/api/stripe/webhook
     ```
   - Select **exactly these six events**, nothing else:
     ```
     checkout.session.completed
     customer.subscription.created
     customer.subscription.updated
     customer.subscription.deleted
     invoice.paid
     invoice.payment_failed
     ```
   - Copy the **signing secret** (`whsec_…`).
4. **Settings → Billing → Customer portal** (test mode):
   - Turn the portal **on**.
   - Enable: view subscription, invoice history, update payment method,
     **cancel subscription**, and cancel **at end of billing period**.
   - Leave plan switching **off** — there is only one product.
   - Set the default return URL to:
     ```
     https://pmvl-web-git-feat-public-site-auth-billi-3529a0-ao-xus-projects.vercel.app/account/billing
     ```
     (The application also passes an explicit return URL on every portal session,
     so this is a fallback.)
5. **Developers → API keys**: you need the **test secret key** (`sk_test_…`).
   Confirm the page says *Test mode*. A key beginning `sk_live_` is rejected by
   the application by design.

---

## 3. Vercel variables (about 5 minutes)

Vercel → project **`pmvl-web`** → **Settings → Environment Variables**.

For each row: set **Environment = Preview** and **Git branch =
`feat/public-site-auth-billing-foundation`**. Branch scoping matters — an
unscoped Preview variable would apply to every branch's Previews.

| Name | Value | Secret? |
| --- | --- | :---: |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk publishable key (`pk_test_…`) | no |
| `CLERK_SECRET_KEY` | Clerk secret key | **yes** |
| `NEXT_PUBLIC_BILLING_ENABLED` | `true` | no |
| `BILLING_MODE` | `test` | no |
| `STRIPE_SECRET_KEY` | Stripe test secret key (`sk_test_…`) | **yes** |
| `STRIPE_WEBHOOK_SECRET` | Stripe signing secret (`whsec_…`) | **yes** |
| `STRIPE_PRO_MONTHLY_PRICE_ID` | monthly `price_…` | no |
| `STRIPE_PRO_ANNUAL_PRICE_ID` | annual `price_…` | no |

**Leave `NEXT_PUBLIC_API_BASE` exactly as it is** — it is already set and
branch-scoped.

**Do not add any of these to Production.** Production must keep no Stripe
configuration at all; billing is off there by absence, which is the strongest
form of off.

### Then force a fresh build

`NEXT_PUBLIC_*` values are inlined at build time, so an existing deployment will
not pick them up and a plain redeploy can reuse the cached build. Push an empty
commit instead:

```bash
git commit --allow-empty -m "chore: rebuild Preview with Clerk and Stripe test credentials" && git push
```

---

## 4. Tell me when that is done

Once the rebuild is green I can run the whole credentialed verification without
further input from you, except at these points, where I will stop and hand the
browser back:

| I will pause for | Why |
| --- | --- |
| Your Clerk account password | Password entry is human-only |
| The email verification code | Human-only, and never recorded |
| Google OAuth account choice and consent | Human-only |
| The Stripe test card, if you prefer to type it | Card fields are human-only. `4242 4242 4242 4242`, any future expiry, any CVC, any postcode — never a real card |

What I will then verify end to end: email sign-up and sign-in, session
restoration, protected-route behaviour, Google OAuth, monthly checkout, the
duplicate-subscription guard, annual checkout, real webhook delivery, signature
rejection, replay idempotency, event ordering, the Customer Portal, cancellation
through `pro_canceling` to `pro_canceled`, and cross-user isolation.

### One thing I will need from you separately

**A second test email address** you control. Cross-user isolation — proving User
B cannot reach User A's account, portal or Stripe customer — needs a real second
identity. I will not invent one or create a disposable inbox. Any second address
you own is fine.

---

## What is already done

- Support email set to `ax2183@nyu.edu` in the footer, terms, privacy notice and
  risk disclosure.
- Security headers added and verified: `X-Content-Type-Options`,
  `Referrer-Policy`, and `Content-Security-Policy: frame-ancestors 'self'`.
- All other legal placeholders still visibly unresolved; the "not reviewed by
  counsel" banners are untouched.
- Full regression green.

## What stays off

- Live billing. `BILLING_MODE` accepts only `disabled` and `test`; there is no
  `live` value, and adding one is a code change that needs review.
- Production billing configuration. Nothing in this checklist touches Production.
- Snapshot publication, pipeline scheduling and trading execution — all unchanged
  and all still disabled.
