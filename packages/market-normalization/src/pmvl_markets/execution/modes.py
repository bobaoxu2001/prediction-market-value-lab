"""Execution modes, environments, and the layered refusal of real-money writes.

The safety property this module exists to hold
----------------------------------------------

    **No code path in this repository can place an order against a real Kalshi
    account, and that is true even if every caller above this layer is wrong.**

Phase 5A builds the execution machinery *before* the research system has earned
authorization to trade. So the machinery has to be safe while nothing above it
is trustworthy yet. One `if mode != live:` in a high-level caller is not that.

Six independent gates, any one of which is sufficient
-----------------------------------------------------

1. **Mode gate.** :class:`ExecutionMode` has no live member. There is no value to
   pass, no string to typo, no config flag to flip. A live mode cannot be
   *selected* because it does not exist to be selected.
2. **Environment gate.** :class:`KalshiEnvironment` binds each mode to exactly one
   base URL. A demo writer cannot hold a production URL because the URL is
   derived from the mode rather than supplied alongside it.
3. **Credential gate.** Credentials carry the environment they were loaded for.
   Production credentials paired with a demo URL - or the reverse - is a
   configuration error that raises rather than a request that gets sent.
4. **Client write guard.** The write client refuses to *construct* in a
   non-writing mode, and re-checks on every mutating call.
5. **Risk gate.** The risk engine refuses any intent whose mode is not a demo
   mode, independently of what the client would have done.
6. **Transport guard.** :func:`assert_transport_write_allowed` refuses any
   non-GET HTTP method aimed at a production host, unconditionally, with no
   parameter that disables it. This is the lowest practical write layer: it sits
   under the client, under the risk engine, and under every caller.

Gate 6 is the one that matters. Gates 1-5 are defence in depth against ordinary
programming error. Gate 6 is what makes the guarantee structural: to place a
real order you would have to edit this file, not merely misuse it.

What "production write" means here
----------------------------------
Deliberately broad. A cancel is a mutation of a real account even though it
removes rather than adds exposure, so `PRODUCTION_READ_ONLY` refuses it. The
conservative reading is the correct one while the account layer is unproven.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

__all__ = [
    "ExecutionMode",
    "KalshiEnvironment",
    "LiveTradingNotAuthorized",
    "ExecutionModeError",
    "PRODUCTION_HOST_MARKERS",
    "DEMO_BASE_URL",
    "PRODUCTION_BASE_URL",
    "base_url_for",
    "environment_for",
    "is_production_url",
    "assert_transport_write_allowed",
    "assert_mode_may_write",
    "WRITE_METHODS",
]


class ExecutionModeError(RuntimeError):
    """Configuration or usage error in the execution mode layer."""


class LiveTradingNotAuthorized(ExecutionModeError):
    """A production account mutation was attempted and refused locally.

    This is raised *before* any HTTP request is constructed. Seeing this
    exception means the guard worked; it never means a request was sent and
    rejected by the venue.
    """


class ExecutionMode(StrEnum):
    """How far an execution attempt is permitted to go.

    There is no ``PRODUCTION_LIVE``. Adding one is a deliberate future act that
    must also defeat :func:`assert_transport_write_allowed`, which is written to
    make that act visible in review rather than reachable by configuration.
    """

    #: Full pipeline except the venue write. Constructs, prices, risk-checks and
    #: persists the intended order, then stops. Never authenticates a write.
    SHADOW = "shadow"

    #: Authenticated against the Kalshi demo sandbox. May create and cancel demo
    #: orders. Demo money is not money.
    DEMO = "demo"

    #: Authenticated against the real account for reads only. Balance, positions,
    #: orders and fills. Every mutation is refused locally.
    PRODUCTION_READ_ONLY = "production_read_only"

    @property
    def may_write(self) -> bool:
        """Whether this mode may mutate *any* account, real or demo."""
        return self is ExecutionMode.DEMO

    @property
    def may_authenticate(self) -> bool:
        """Whether this mode talks to an authenticated endpoint at all."""
        return self is not ExecutionMode.SHADOW

    @property
    def touches_real_account(self) -> bool:
        return self is ExecutionMode.PRODUCTION_READ_ONLY


class KalshiEnvironment(StrEnum):
    """Which Kalshi deployment a set of credentials and URLs belongs to."""

    DEMO = "demo"
    PRODUCTION = "production"


#: Verified against Kalshi's published API-key documentation, 2026-09.
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

#: Matches the existing public-data client default in ``Settings.kalshi_api_base``.
PRODUCTION_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

#: Hostname fragments that identify a real-money deployment.
#:
#: Deliberately a denylist on the *production* side rather than an allowlist on
#: the demo side. An unrecognised host is treated as production - see
#: :func:`is_production_url` - so a typo, a new Kalshi hostname, or a redirect to
#: somewhere unexpected fails closed into "no writes" instead of open.
PRODUCTION_HOST_MARKERS = (
    "api.elections.kalshi.com",
    "trading-api.kalshi.com",
    "api.kalshi.com",
)

#: Hosts known to be sandboxes. Only these are ever writable.
_DEMO_HOST_MARKERS = (
    "demo.kalshi.co",
    "demo-api.kalshi.co",
)

#: HTTP methods that can change account state.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def environment_for(mode: ExecutionMode) -> KalshiEnvironment:
    """The one environment a mode is allowed to reach.

    SHADOW never authenticates, but it still resolves to DEMO so that any
    accidental transport use inherits the sandbox rather than production.
    """
    if mode is ExecutionMode.PRODUCTION_READ_ONLY:
        return KalshiEnvironment.PRODUCTION
    return KalshiEnvironment.DEMO


def base_url_for(mode: ExecutionMode) -> str:
    """Base URL derived from the mode.

    The URL is *derived*, never supplied next to the mode. That removes the
    entire class of bug where a demo mode is handed a production endpoint.
    """
    if environment_for(mode) is KalshiEnvironment.PRODUCTION:
        return PRODUCTION_BASE_URL
    return DEMO_BASE_URL


def is_production_url(url: str) -> bool:
    """Whether a URL points at a real-money deployment.

    Fails closed. An empty, malformed, or unrecognised host counts as
    production, because the cost of misclassifying a sandbox as production is a
    refused test and the cost of the reverse is a real order.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return True
    if any(host == m or host.endswith("." + m) for m in _DEMO_HOST_MARKERS):
        return False
    return True


def assert_transport_write_allowed(method: str, url: str) -> None:
    """Refuse any state-changing request aimed at a real account.

    The lowest write layer in the system. It knows nothing about modes,
    credentials, intents or risk - only the HTTP verb and the destination - so
    it cannot be bypassed by constructing the layers above it incorrectly.

    Read methods are unrestricted here; production *reads* are the entire point
    of ``PRODUCTION_READ_ONLY``.

    "Read" means GET, and nothing else. ``WRITE_METHODS`` used to be the test,
    which quietly exempted every verb nobody had thought of - OPTIONS, HEAD, and
    any future extension. The documented contract is "refuses any non-GET method
    aimed at a production host", so that is now what the code does. An unknown
    verb is treated as a mutation, because that is the safe direction.
    """
    verb = (method or "").upper()
    if verb == "GET":
        return
    if is_production_url(url):
        raise LiveTradingNotAuthorized(
            f"refusing {verb} to production endpoint {_safe_target(url)}: "
            "real-money order placement is not authorized in this build "
            "(no ExecutionMode permits it)"
        )


def assert_mode_may_write(mode: ExecutionMode, operation: str) -> None:
    """Refuse a mutation the mode does not permit.

    Separate from the transport guard on purpose: this one produces the message
    an operator needs ("PRODUCTION_READ_ONLY cannot cancel_order"), while the
    transport guard produces the guarantee.
    """
    if mode.may_write:
        return
    raise LiveTradingNotAuthorized(
        f"{operation} is not permitted in mode {mode.value}: "
        f"only {ExecutionMode.DEMO.value} may mutate an account"
    )


def _safe_target(url: str) -> str:
    """Host and path only - never query strings, which can carry identifiers."""
    parts = urlsplit(url)
    return f"{parts.hostname or '?'}{parts.path or ''}"


@dataclass(frozen=True, slots=True)
class ModeProfile:
    """Everything derived from a mode, resolved once and passed around together.

    Frozen so a caller cannot retarget a live client at a different environment
    after construction.
    """

    mode: ExecutionMode
    environment: KalshiEnvironment
    base_url: str

    @classmethod
    def of(cls, mode: ExecutionMode) -> "ModeProfile":
        env = environment_for(mode)
        url = base_url_for(mode)
        # Belt and braces: the two derivations above must agree. If a future edit
        # makes them disagree, that is a programming error, not a runtime state.
        if (env is KalshiEnvironment.PRODUCTION) != is_production_url(url):
            raise ExecutionModeError(
                f"mode {mode.value} resolved to inconsistent environment/URL"
            )
        return cls(mode=mode, environment=env, base_url=url)

    @property
    def may_write(self) -> bool:
        return self.mode.may_write

    def assert_may_write(self, operation: str) -> None:
        assert_mode_may_write(self.mode, operation)
        # A writable mode must never have resolved to a production URL. This
        # cannot currently happen; it is checked because the cost of the
        # combination existing undetected is unbounded.
        if is_production_url(self.base_url):
            raise LiveTradingNotAuthorized(
                f"{operation} refused: mode {self.mode.value} resolved to "
                "a production endpoint"
            )
