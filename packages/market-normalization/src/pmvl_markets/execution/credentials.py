"""Kalshi credential loading and RSA-PSS request signing.

Scheme verified against Kalshi's published API-key documentation (2026-09) rather
than from memory:

* headers ``KALSHI-ACCESS-KEY``, ``KALSHI-ACCESS-TIMESTAMP``,
  ``KALSHI-ACCESS-SIGNATURE``
* the signed string is ``timestamp_ms + HTTP_METHOD + path``
* the path is signed **without** its query string
* RSA-PSS over SHA-256, MGF1(SHA-256), salt length = digest length

Handling of key material
------------------------
The private key exists as a parsed object in memory and nowhere else. It is not
written to SQLite, not placed in a log record, not returned by ``redacted()``, and
not included in ``repr``. :class:`KalshiCredentials` defines ``__repr__`` by hand
for that reason - the dataclass-generated one would print the PEM.

Credentials carry the environment they were loaded for. That is what makes gate 3
in :mod:`pmvl_markets.execution.modes` possible: production credentials paired
with a demo URL is a configuration error that raises, not a request that gets
sent to the wrong place.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .modes import (
    ExecutionMode,
    ExecutionModeError,
    KalshiEnvironment,
    environment_for,
)

log = get_logger(__name__)

__all__ = [
    "KalshiCredentials",
    "CredentialsUnavailable",
    "SigningUnavailable",
    "load_credentials",
    "credentials_present",
    "signing_headers",
    "signing_payload",
    "canonical_signing_path",
]

_HEADER_KEY = "KALSHI-ACCESS-KEY"
_HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
_HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"


class CredentialsUnavailable(ExecutionModeError):
    """No usable credentials are configured for the requested environment."""


class SigningUnavailable(ExecutionModeError):
    """The signing backend is not installed.

    Raised rather than falling back to an unsigned request, which would leak the
    key id to the venue in an unauthenticated call.
    """


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    """An API key id plus its private key, bound to one environment."""

    key_id: str
    environment: KalshiEnvironment
    #: PEM text. Never logged, never persisted, never serialised.
    private_key_pem: str
    source: str = ""

    def __repr__(self) -> str:  # pragma: no cover - trivial, but security-relevant
        return (
            f"KalshiCredentials(key_id={_mask(self.key_id)!r}, "
            f"environment={self.environment.value!r}, "
            f"private_key_pem=<redacted {len(self.private_key_pem)} bytes>, "
            f"source={self.source!r})"
        )

    __str__ = __repr__

    def assert_environment(self, expected: KalshiEnvironment) -> None:
        if self.environment is not expected:
            raise CredentialsUnavailable(
                f"credentials are for {self.environment.value} but "
                f"{expected.value} was requested; refusing to cross environments"
            )


def _mask(value: str) -> str:
    """Enough to identify which key is loaded, not enough to reuse it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]}"


def _read_pem(raw_pem: str, path_value: str) -> tuple[str, str]:
    """Return ``(pem_text, source_label)`` from either inline PEM or a file path."""
    if raw_pem.strip():
        return raw_pem, "env:inline"
    if path_value.strip():
        path = Path(path_value).expanduser()
        if not path.is_file():
            raise CredentialsUnavailable(
                f"private key path does not exist: {path}"
            )
        return path.read_text(encoding="utf-8"), f"file:{path.name}"
    return "", ""


def load_credentials(
    mode: ExecutionMode,
    *,
    settings: Any = None,
) -> KalshiCredentials:
    """Load the credentials a mode is permitted to use.

    SHADOW never authenticates, so asking for its credentials is a programming
    error rather than a missing-config error.
    """
    if mode is ExecutionMode.SHADOW:
        raise ExecutionModeError(
            "SHADOW mode never authenticates; no credentials should be loaded"
        )

    cfg = settings if settings is not None else get_settings()
    env = environment_for(mode)

    if env is KalshiEnvironment.DEMO:
        key_id = (getattr(cfg, "kalshi_demo_api_key_id", "") or "").strip()
        pem, source = _read_pem(
            getattr(cfg, "kalshi_demo_private_key_pem", "") or "",
            getattr(cfg, "kalshi_demo_private_key_path", "") or "",
        )
    else:
        key_id = (getattr(cfg, "kalshi_api_key_id", "") or "").strip()
        pem, source = _read_pem(
            getattr(cfg, "kalshi_private_key_pem", "") or "",
            getattr(cfg, "kalshi_private_key_path", "") or "",
        )

    if not key_id or not pem.strip():
        raise CredentialsUnavailable(
            f"no {env.value} credentials configured "
            f"(key_id {'set' if key_id else 'missing'}, "
            f"private key {'set' if pem.strip() else 'missing'})"
        )

    return KalshiCredentials(
        key_id=key_id,
        environment=env,
        private_key_pem=pem,
        source=source,
    )


def credentials_present(mode: ExecutionMode, *, settings: Any = None) -> bool:
    """Whether credentials exist, without raising and without loading key bytes.

    Used by operator commands and smoke tests to report
    ``BLOCKED_MISSING_CREDENTIALS`` rather than crashing.
    """
    if mode is ExecutionMode.SHADOW:
        return True
    try:
        load_credentials(mode, settings=settings)
    except ExecutionModeError:
        return False
    return True


def canonical_signing_path(path: str) -> str:
    """The path Kalshi expects in the signature: no query string.

    Signing a path that includes ``?limit=5`` produces a signature the venue
    rejects, so this is stripped in exactly one place.
    """
    return path.split("?", 1)[0]


def signing_payload(timestamp_ms: int, method: str, path: str) -> str:
    """The exact string Kalshi signs: timestamp, then verb, then bare path."""
    return f"{timestamp_ms}{method.upper()}{canonical_signing_path(path)}"


def _load_private_key(pem: str) -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover - depends on env
        raise SigningUnavailable(
            "the 'cryptography' package is required to sign Kalshi requests; "
            "install it before enabling any authenticated execution mode"
        ) from exc
    try:
        return serialization.load_pem_private_key(
            pem.encode("utf-8"), password=None
        )
    except Exception as exc:
        # Deliberately does not include the exception's own text: some PEM
        # parsers echo surrounding key bytes into the message.
        raise CredentialsUnavailable(
            f"could not parse private key ({type(exc).__name__}); "
            "expected an unencrypted PEM RSA private key"
        ) from None


def sign_payload(credentials: KalshiCredentials, payload: str) -> str:
    """RSA-PSS/SHA-256 signature of ``payload``, base64-encoded."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - depends on env
        raise SigningUnavailable(
            "the 'cryptography' package is required to sign Kalshi requests"
        ) from exc

    key = _load_private_key(credentials.private_key_pem)
    signature = key.sign(
        payload.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def signing_headers(
    credentials: KalshiCredentials,
    method: str,
    path: str,
    *,
    timestamp_ms: int | None = None,
) -> Mapping[str, str]:
    """Authentication headers for one request.

    ``timestamp_ms`` is injectable so tests can pin a signature without patching
    the clock globally.
    """
    ts = (
        timestamp_ms
        if timestamp_ms is not None
        else int(utcnow().timestamp() * 1000)
    )
    payload = signing_payload(ts, method, path)
    return {
        _HEADER_KEY: credentials.key_id,
        _HEADER_TIMESTAMP: str(ts),
        _HEADER_SIGNATURE: sign_payload(credentials, payload),
    }


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Header view safe to log: signature and key id are masked.

    Anything that logs request headers must route through this. The signature is
    not itself a secret, but it is derived key material and there is no reason
    for it to sit in a log file.
    """
    out: dict[str, str] = {}
    for name, value in headers.items():
        upper = name.upper()
        if upper == _HEADER_SIGNATURE:
            out[name] = "<redacted>"
        elif upper == _HEADER_KEY:
            out[name] = _mask(value)
        else:
            out[name] = value
    return out
