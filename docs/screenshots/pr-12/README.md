# PR #12 review screenshots

All captured from the Vercel Preview at commit `615933d`, against the API
Preview at the same commit — not from a local dev server and not from
production. Headless Chrome, `deviceScaleFactor: 2`, then downscaled to 1600px
wide (1200 for the full-page shot).

Billing and authentication are **not configured** on this Preview, so these show
the honest unconfigured state: `/sign-in` explains that accounts are not enabled,
`/account` redirects there, and pricing offers early access rather than checkout.

| File | Route | Viewport | Theme |
| --- | --- | --- | --- |
| `home-hero-light-1440.png` | `/` | 1440×900 | light |
| `home-hero-dark-1440.png` | `/` | 1440×900 | dark |
| `home-full-light-1280.png` | `/` full page | 1280 | light |
| `home-mobile-light-390.png` | `/` full page | 390×844 | light |
| `pricing-light-1440.png` | `/pricing` | 1440 | light |
| `pricing-dark-1440.png` | `/pricing` | 1440 | dark |
| `pricing-mobile-390.png` | `/pricing` | 390×844 | light |
| `sign-in-unconfigured-1440.png` | `/sign-in` | 1440×900 | light |
| `account-redirect-1440.png` | `/account` → `/sign-in?reason=auth-unavailable` | 1440×900 | light |
| `risk-disclosure-light-1440.png` | `/risk-disclosure` | 1440 | light |
| `terms-light-1440.png` | `/terms` | 1440 | light |
| `privacy-dark-1440.png` | `/privacy` | 1440 | dark |
| `app-briefing-light-1440.png` | `/app` | 1440 | light |
| `app-briefing-dark-1440.png` | `/app` | 1440 | dark |
| `markets-light-1440.png` | `/markets` | 1440 | light |
| `arbitrage-actionable-1440.png` | `/arbitrage?view=actionable` | 1440 | light |
| `backtest-demo-1440.png` | `/backtest?mode=demo` | 1440 | light |
| `system-dark-1440.png` | `/system` | 1440 | dark |
| `market-630-light-1440.png` | `/market/630` | 1440 | light |

The six product screenshots embedded in the marketing homepage itself live in
`apps/web/public/product/` and were captured from the same Preview build at
1280×860.
