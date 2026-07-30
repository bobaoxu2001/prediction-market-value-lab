"""Prove production is usable, not merely that Vercel called it READY.

PR #4 shipped with green CI, a successful Vercel build and a deployment marked
READY - and every production API route returned FastAPI's own 404, because the
redeploy exposed changed Vercel rewrite semantics. Nothing noticed until a human
looked.

A build that succeeds says the code compiled. It says nothing about whether the
deployment answers. This script asks.

It also refuses to accept a deployment that is merely *up*: the commit it serves
must be the commit that was pushed. A healthy old deployment answering while the
new one silently failed to replace it is the drift that took a day to spot once
already.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API_ROUTES = (
    "/health",
    "/system",
    "/markets/630",
    "/arbitrage?view=actionable",
    "/arbitrage?view=diagnostics",
)
WEB_ROUTES = (
    "/",
    "/system",
    "/arbitrage",
    "/markets",
    "/market/630",
    "/backtest?mode=demo",
)


class Failure:
    """Categories, so a report says what to do rather than only that it broke."""

    NOT_READY = "deployment not ready"
    COMMIT_MISMATCH = "commit mismatch"
    API_UNAVAILABLE = "API unavailable"
    WEB_UNAVAILABLE = "Web unavailable"
    UNEXPECTED_404 = "unexpected 404"
    UNEXPECTED_5XX = "unexpected 5xx"
    INVALID_JSON = "invalid JSON"
    DRIFT = "Web/API drift"
    SNAPSHOT_SEMANTICS = "snapshot semantics mismatch"


@dataclass
class SmokeReport:
    expected_commit: str
    api_commit: str = ""
    web_commit: str = ""
    routes: list[dict[str, Any]] = field(default_factory=list)
    parity: bool = False
    snapshot_mode: bool = False
    errors: list[dict[str, str]] = field(default_factory=list)

    def fail(self, category: str, detail: str) -> None:
        self.errors.append({"category": category, "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_commit": self.expected_commit,
            "api_commit": self.api_commit,
            "web_commit": self.web_commit,
            "routes": self.routes,
            "parity": self.parity,
            "snapshot_mode": self.snapshot_mode,
            "errors": self.errors,
        }


def _get(url: str, timeout: int = 45, attempts: int = 4) -> tuple[int, str]:
    """GET with retries for TRANSPORT failures only.

    A 4xx or 5xx is returned immediately: retrying a real defect until it passes
    is how a smoke test becomes decoration.
    """
    context = ssl.create_default_context()
    status, body = 0, ""
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": "pmvl-postdeploy/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")[:500]
        except Exception as exc:  # noqa: BLE001 - transport, not the deployment
            status, body = 0, f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                time.sleep(4 * (attempt + 1))
    return status, body


def _api_commit(base: str) -> tuple[str, dict[str, Any] | None]:
    status, body = _get(f"{base}/system")
    if status != 200:
        return "", None
    try:
        data = json.loads(body)["data"]
    except (json.JSONDecodeError, KeyError):
        return "", None
    return str(data.get("deployment", {}).get("commit_sha") or ""), data


def _web_commit(base: str) -> str:
    """The web app renders its own build commit on /system."""
    import re

    status, body = _get(f"{base}/system")
    if status != 200:
        return ""
    match = re.search(r"Web commit[^0-9a-f]{0,40}([0-9a-f]{12})", body)
    if match:
        return match.group(1)
    # Fall back to any 12-hex token, then let the caller compare.
    tokens = re.findall(r"\b([0-9a-f]{12})\b", body)
    return tokens[0] if tokens else ""


def wait_for_commit(api: str, web: str, expected: str, report: SmokeReport, *, minutes: int) -> bool:
    """Poll until BOTH projects serve the pushed commit, or give up loudly."""
    deadline = time.monotonic() + minutes * 60
    short = expected[:12]
    while time.monotonic() < deadline:
        api_sha, _ = _api_commit(api)
        web_sha = _web_commit(web)
        report.api_commit, report.web_commit = api_sha, web_sha
        if api_sha.startswith(short) and web_sha.startswith(short):
            return True
        print(f"  waiting: api={api_sha[:12] or '-'} web={web_sha[:12] or '-'} want={short}")
        time.sleep(20)

    if not report.api_commit:
        report.fail(Failure.API_UNAVAILABLE, "API never answered /system with a commit")
    elif not report.api_commit.startswith(short):
        report.fail(
            Failure.COMMIT_MISMATCH, f"API serves {report.api_commit[:12]}, expected {short}"
        )
    if not report.web_commit:
        report.fail(Failure.WEB_UNAVAILABLE, "Web never rendered a commit")
    elif not report.web_commit.startswith(short):
        report.fail(
            Failure.COMMIT_MISMATCH, f"Web serves {report.web_commit[:12]}, expected {short}"
        )
    if not report.errors:
        report.fail(Failure.NOT_READY, f"neither project reached {short} in {minutes} minutes")
    return False


def check_routes(base: str, routes: tuple[str, ...], *, expect_json: bool, report: SmokeReport) -> None:
    for route in routes:
        status, body = _get(f"{base}{route}")
        entry = {"url": base + route, "status": status, "json": None}
        if status == 0:
            report.fail(
                Failure.API_UNAVAILABLE if expect_json else Failure.WEB_UNAVAILABLE,
                f"{route}: {body[:120]}",
            )
        elif status == 404:
            # The signature of the outage this script exists for.
            report.fail(Failure.UNEXPECTED_404, f"{route} returned 404")
        elif status >= 500:
            report.fail(Failure.UNEXPECTED_5XX, f"{route} returned {status}")
        elif status != 200:
            report.fail(Failure.UNEXPECTED_5XX, f"{route} returned {status}")
        elif expect_json:
            try:
                json.loads(body)
                entry["json"] = True
            except json.JSONDecodeError:
                entry["json"] = False
                # An HTML error page where JSON belongs is how a platform-level
                # failure disguises itself as a working route.
                report.fail(Failure.INVALID_JSON, f"{route} returned non-JSON")
        report.routes.append(entry)
        print(f"  {status}  {route}")


def check_semantics(data: dict[str, Any] | None, report: SmokeReport) -> None:
    if not data:
        report.fail(Failure.SNAPSHOT_SEMANTICS, "/system produced no data block")
        return
    if data.get("runtime_mode") != "read_only_snapshot":
        report.fail(
            Failure.SNAPSHOT_SEMANTICS, f"runtime_mode is {data.get('runtime_mode')!r}"
        )
    else:
        report.snapshot_mode = True
    if data.get("trading_execution_enabled"):
        report.fail(Failure.SNAPSHOT_SEMANTICS, "trading execution is enabled in production")
    if (data.get("deployment") or {}).get("commit_ref") != "main":
        report.fail(
            Failure.SNAPSHOT_SEMANTICS,
            f"commit_ref is {(data.get('deployment') or {}).get('commit_ref')!r}, expected main",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="https://pmvl-api.vercel.app")
    parser.add_argument("--web", default="https://pmvl-web.vercel.app")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--wait-minutes", type=int, default=10)
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)

    report = SmokeReport(expected_commit=args.commit)
    print(f"post-deploy smoke: expecting {args.commit[:12]}")

    deployed = wait_for_commit(args.api, args.web, args.commit, report, minutes=args.wait_minutes)
    if deployed:
        print("both projects serve the pushed commit")
        report.parity = report.api_commit[:12] == report.web_commit[:12]
        if not report.parity:
            report.fail(
                Failure.DRIFT,
                f"api={report.api_commit[:12]} web={report.web_commit[:12]}",
            )
        print("API routes:")
        check_routes(args.api, API_ROUTES, expect_json=True, report=report)
        print("Web routes:")
        check_routes(args.web, WEB_ROUTES, expect_json=False, report=report)
        _, data = _api_commit(args.api)
        check_semantics(data, report)

    if args.report:
        Path(args.report).write_text(json.dumps(report.as_dict(), indent=2) + "\n")

    if report.errors:
        print("\nPOST-DEPLOY SMOKE FAILED:")
        for error in report.errors:
            print(f"   [{error['category']}] {error['detail']}")
        return 1
    print("\nPOST-DEPLOY SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
