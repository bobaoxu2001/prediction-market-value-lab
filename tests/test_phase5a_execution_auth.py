"""Phase 5A: request signing, credential handling, and secret hygiene.

No real key material is used. The signing *contract* - what string is signed,
which headers carry it, that the query string is excluded - is asserted without
any cryptography, because that contract is where the bugs actually live. The
cryptographic step itself is exercised only when ``cryptography`` is installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pmvl_markets.execution import (
    ExecutionMode,
    KalshiEnvironment,
    credentials_present,
    load_credentials,
)
from pmvl_markets.execution.credentials import (
    CredentialsUnavailable,
    KalshiCredentials,
    canonical_signing_path,
    redact_headers,
    signing_headers,
    signing_payload,
)
from pmvl_markets.execution.modes import ExecutionModeError


try:  # pragma: no cover - environment dependent
    import cryptography  # noqa: F401

    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False


def _creds(env: KalshiEnvironment = KalshiEnvironment.DEMO) -> KalshiCredentials:
    return KalshiCredentials(
        key_id="abcd1234efgh", environment=env, private_key_pem="-----FAKE-----"
    )


# ---------------------------------------------------------------- the contract
def test_signing_payload_is_timestamp_method_path() -> None:
    assert (
        signing_payload(1756950000000, "GET", "/trade-api/v2/portfolio/balance")
        == "1756950000000GET/trade-api/v2/portfolio/balance"
    )


def test_method_is_upper_cased() -> None:
    assert signing_payload(1, "post", "/x") == "1POST/x"


def test_query_string_is_excluded_from_the_signature() -> None:
    """Signing the query string produces a signature Kalshi rejects."""
    assert (
        canonical_signing_path("/trade-api/v2/portfolio/orders?limit=5&cursor=abc")
        == "/trade-api/v2/portfolio/orders"
    )
    assert (
        signing_payload(7, "GET", "/trade-api/v2/portfolio/orders?limit=5")
        == "7GET/trade-api/v2/portfolio/orders"
    )


def test_timestamp_is_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    import pmvl_markets.execution.credentials as mod

    monkeypatch.setattr(mod, "sign_payload", lambda c, p: "sig")
    headers = signing_headers(_creds(), "GET", "/trade-api/v2/portfolio/balance")
    ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
    # Milliseconds since epoch for any date after 2020 is 13 digits.
    assert len(str(ts)) == 13


def test_headers_carry_the_documented_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pmvl_markets.execution.credentials as mod

    monkeypatch.setattr(mod, "sign_payload", lambda c, p: "SIGNATURE")
    headers = signing_headers(
        _creds(), "GET", "/trade-api/v2/portfolio/balance", timestamp_ms=99
    )
    assert set(headers) == {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE",
    }
    assert headers["KALSHI-ACCESS-KEY"] == "abcd1234efgh"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "99"


def test_injected_timestamp_makes_the_signature_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pmvl_markets.execution.credentials as mod

    seen: list[str] = []
    monkeypatch.setattr(
        mod, "sign_payload", lambda c, p: (seen.append(p), "sig")[1]
    )
    signing_headers(_creds(), "DELETE", "/trade-api/v2/portfolio/orders/x?a=1",
                    timestamp_ms=1234567890123)
    assert seen == ["1234567890123DELETE/trade-api/v2/portfolio/orders/x"]


# --------------------------------------------------------------- secret hygiene
def test_repr_never_prints_the_private_key() -> None:
    creds = KalshiCredentials(
        key_id="abcd1234efgh",
        environment=KalshiEnvironment.DEMO,
        private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nSUPERSECRET\n",
    )
    for text in (repr(creds), str(creds), f"{creds}"):
        assert "SUPERSECRET" not in text
        assert "BEGIN RSA PRIVATE KEY" not in text


def test_repr_masks_the_key_id() -> None:
    assert "abcd1234efgh" not in repr(_creds())


def test_redact_headers_hides_the_signature() -> None:
    out = redact_headers(
        {
            "KALSHI-ACCESS-KEY": "abcd1234efgh",
            "KALSHI-ACCESS-SIGNATURE": "c2lnbmF0dXJl",
            "Accept": "application/json",
        }
    )
    assert out["KALSHI-ACCESS-SIGNATURE"] == "<redacted>"
    assert "abcd1234efgh" not in out["KALSHI-ACCESS-KEY"]
    assert out["Accept"] == "application/json"


def test_settings_redaction_covers_every_credential_field() -> None:
    from pmvl_shared.config import Settings

    redacted = Settings(
        kalshi_api_key_id="prod-key",
        kalshi_private_key_pem="SECRET-PEM",
        kalshi_private_key_path="/tmp/secret.pem",
        kalshi_demo_api_key_id="demo-key",
        kalshi_demo_private_key_pem="DEMO-PEM",
        kalshi_demo_private_key_path="/tmp/demo.pem",
    ).redacted()
    blob = repr(redacted)
    for secret in (
        "prod-key",
        "SECRET-PEM",
        "/tmp/secret.pem",
        "demo-key",
        "DEMO-PEM",
        "/tmp/demo.pem",
    ):
        assert secret not in blob
    assert redacted["kalshi_private_key_pem"] is True


def test_no_key_material_is_committed() -> None:
    """The repository must contain no PEM private key."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    needle = "BEGIN RSA PRIVATE KEY"
    other = "BEGIN PRIVATE KEY"
    offenders = []
    for rel in tracked:
        path = root / rel
        if not path.is_file() or path.suffix in {".png", ".jpg", ".gz", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if needle in text or other in text:
            offenders.append(rel)
    # This test file names the marker itself; that is the only allowed hit.
    assert [o for o in offenders if not o.endswith("test_phase5a_execution_auth.py")] == []


def test_gitignore_covers_key_material() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".gitignore").read_text(encoding="utf-8")
    assert "*.pem" in text
    assert ".env" in text


# ------------------------------------------------------------------- loading
def test_shadow_mode_never_loads_credentials() -> None:
    with pytest.raises(ExecutionModeError):
        load_credentials(ExecutionMode.SHADOW)


def test_missing_credentials_raise_rather_than_return_empty() -> None:
    class Empty:
        kalshi_api_key_id = ""
        kalshi_private_key_pem = ""
        kalshi_private_key_path = ""
        kalshi_demo_api_key_id = ""
        kalshi_demo_private_key_pem = ""
        kalshi_demo_private_key_path = ""

    with pytest.raises(CredentialsUnavailable):
        load_credentials(ExecutionMode.DEMO, settings=Empty())


def test_credentials_present_reports_false_without_raising() -> None:
    class Empty:
        kalshi_api_key_id = ""
        kalshi_private_key_pem = ""
        kalshi_private_key_path = ""
        kalshi_demo_api_key_id = ""
        kalshi_demo_private_key_pem = ""
        kalshi_demo_private_key_path = ""

    assert credentials_present(ExecutionMode.DEMO, settings=Empty()) is False


def test_demo_and_production_read_separate_fields(tmp_path: Path) -> None:
    """Demo must not silently authenticate with production keys."""
    demo_pem = tmp_path / "demo.pem"
    demo_pem.write_text("demo-key-material")

    class Cfg:
        kalshi_api_key_id = "PROD"
        kalshi_private_key_pem = "prod-key-material"
        kalshi_private_key_path = ""
        kalshi_demo_api_key_id = "DEMO"
        kalshi_demo_private_key_pem = ""
        kalshi_demo_private_key_path = str(demo_pem)

    demo = load_credentials(ExecutionMode.DEMO, settings=Cfg())
    assert demo.key_id == "DEMO"
    assert demo.environment is KalshiEnvironment.DEMO
    assert "prod-key-material" not in demo.private_key_pem

    prod = load_credentials(ExecutionMode.PRODUCTION_READ_ONLY, settings=Cfg())
    assert prod.key_id == "PROD"
    assert prod.environment is KalshiEnvironment.PRODUCTION


def test_a_missing_key_file_is_an_error_not_an_empty_key(tmp_path: Path) -> None:
    class Cfg:
        kalshi_api_key_id = ""
        kalshi_private_key_pem = ""
        kalshi_private_key_path = ""
        kalshi_demo_api_key_id = "DEMO"
        kalshi_demo_private_key_pem = ""
        kalshi_demo_private_key_path = str(tmp_path / "absent.pem")

    with pytest.raises(CredentialsUnavailable):
        load_credentials(ExecutionMode.DEMO, settings=Cfg())


def test_environment_assertion_blocks_crossing() -> None:
    with pytest.raises(CredentialsUnavailable):
        _creds(KalshiEnvironment.DEMO).assert_environment(
            KalshiEnvironment.PRODUCTION
        )


# ------------------------------------------------- real signing, when available
@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography is not installed")
def test_signature_verifies_against_the_public_key() -> None:
    """Generates an ephemeral key at runtime; nothing is committed."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from pmvl_markets.execution.credentials import sign_payload

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    creds = KalshiCredentials(
        key_id="k", environment=KalshiEnvironment.DEMO, private_key_pem=pem
    )
    payload = signing_payload(1756950000000, "GET", "/trade-api/v2/portfolio/balance")
    import base64

    signature = base64.b64decode(sign_payload(creds, payload))

    key.public_key().verify(
        signature,
        payload.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography is not installed")
def test_a_malformed_key_does_not_echo_its_bytes() -> None:
    from pmvl_markets.execution.credentials import sign_payload

    creds = KalshiCredentials(
        key_id="k",
        environment=KalshiEnvironment.DEMO,
        private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nNOTAKEY-SENTINEL\n",
    )
    with pytest.raises(CredentialsUnavailable) as exc:
        sign_payload(creds, "payload")
    assert "NOTAKEY-SENTINEL" not in str(exc.value)
