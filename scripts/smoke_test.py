"""Post-deploy smoke test. Exits non-zero if the deployment is not usable.

This exists because "the deploy succeeded" and "the site works" turned out to be
different things. A Vercel deployment reported READY while every request returned
FUNCTION_INVOCATION_FAILED, because the database was missing from the bundle and the
API crashed during import. Nothing in the deploy pipeline noticed.

Run against the API, the web app, or both. The Makefile wires this in so a frontend
deploy is only promoted after the API answers.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: (path, key that must be present and non-empty)
API_CHECKS: tuple[tuple[str, str | None], ...] = (
    ("/health", None),
    ("/system", "data"),
    ("/methodology", "data"),
    ("/backtest?mode=demo", "data"),
    ("/track-record?mode=demo&limit=5", "data"),
    ("/opportunities?horizon=24h&mode=demo", None),
    ("/opportunities?horizon=7d", None),
    ("/opportunities/disagreements?horizon=24h", None),
    ("/opportunities/watchlist?horizon=24h", None),
    ("/opportunities/funnel?horizon=24h", "data"),
    ("/case-study?mode=demo", "data"),
    ("/case-study?mode=demo&result=winner", "data"),
    ("/case-study?mode=demo&result=loser", "data"),
    ("/arbitrage", None),
    ("/arbitrage?view=actionable", None),
    ("/arbitrage?view=diagnostics", None),
    ("/markets?limit=5", "data"),
)

WEB_CHECKS: tuple[str, ...] = (
    "/",
    "/case-study?mode=demo",
    "/case-study?mode=demo&result=winner",
    "/case-study?mode=demo&result=loser",
    "/demo?step=1",
    "/demo?step=2",
    "/demo?step=3",
    "/demo?step=4",
    "/demo?step=5",
    "/demo?step=99",
    "/arbitrage",
    "/markets",
    "/backtest?mode=demo",
    "/track-record?mode=demo",
    "/methodology",
    "/system",
)

#: Strings that must never appear in a production page. "API unavailable" means the
#: frontend could not reach the backend; the localhost hint is a development message
#: that has no business being shown to a visitor.
FORBIDDEN_IN_HTML: tuple[str, ...] = (
    "API unavailable",
    "localhost:8000",
    "make api",
)


def _ssl_context() -> ssl.SSLContext | None:
    """TLS context honouring a custom CA bundle.

    Machines behind a TLS-inspecting proxy present a private CA that is not in the
    default trust store. SSL_CERT_FILE is the standard way to point at it; a bundle
    exported into the venv is picked up automatically. Verification is never
    disabled - a smoke test that ignores certificate errors is not a safety check.
    """
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if not bundle:
        local = Path(__file__).resolve().parents[1] / ".venv" / "ca.pem"
        bundle = str(local) if local.exists() else None
    return ssl.create_default_context(cafile=bundle) if bundle else None


def _get_once(url: str, timeout: int) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "pmvl-smoke/1.0"})
    context = _ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        # Status 0 means the request never completed: DNS, TLS, or a dropped
        # connection. That is a property of the network between here and the
        # deployment, not of the deployment.
        return 0, f"{type(exc).__name__}: {exc}"


def _get(url: str, timeout: int, attempts: int = 5) -> tuple[int, str]:
    """GET with retries for TRANSPORT failures only.

    A 5xx is a real defect and is returned immediately - retrying it would mask the
    very thing this script exists to catch. A status of 0 (TLS reset, dropped
    connection, cold-start timeout) says nothing about the deployment, and retrying
    is the difference between a trustworthy check and one that cries wolf on a flaky
    link. Serverless cold starts also legitimately need a second attempt.
    """
    status, body = 0, ""
    for attempt in range(attempts):
        status, body = _get_once(url, timeout)
        if status != 0:
            return status, body
        if attempt < attempts - 1:
            time.sleep(3 * (attempt + 1))
    return status, body


def check_api(base: str, timeout: int) -> list[str]:
    failures: list[str] = []
    base = base.rstrip("/")
    for path, required_key in API_CHECKS:
        status, body = _get(f"{base}{path}", timeout)
        if status != 200:
            failures.append(f"API {path} -> HTTP {status}: {body[:160]}")
            continue
        if required_key:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                failures.append(f"API {path} -> 200 but body is not JSON")
                continue
            if not payload.get(required_key):
                failures.append(f"API {path} -> 200 but '{required_key}' is empty")
    return failures


def check_web(base: str, timeout: int) -> list[str]:
    failures: list[str] = []
    base = base.rstrip("/")
    for path in WEB_CHECKS:
        status, body = _get(f"{base}{path}", timeout)
        if status != 200:
            failures.append(f"WEB {path} -> HTTP {status}")
            continue
        for forbidden in FORBIDDEN_IN_HTML:
            if forbidden in body:
                failures.append(f"WEB {path} -> page contains {forbidden!r}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", help="API base URL, e.g. https://pmvl-api.vercel.app")
    parser.add_argument("--web", help="Web base URL, e.g. https://pmvl-web.vercel.app")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    if not args.api and not args.web:
        parser.error("pass --api and/or --web")

    failures: list[str] = []
    if args.api:
        print(f"smoke-testing API  {args.api}")
        failures += check_api(args.api, args.timeout)
    if args.web:
        print(f"smoke-testing WEB  {args.web}")
        failures += check_web(args.web, args.timeout)

    if failures:
        print(f"\nSMOKE TEST FAILED ({len(failures)} problem(s)):")
        for failure in failures:
            print(f"   {failure}")
        return 1

    total = (len(API_CHECKS) if args.api else 0) + (len(WEB_CHECKS) if args.web else 0)
    print(f"\nSMOKE TEST PASSED ({total} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
