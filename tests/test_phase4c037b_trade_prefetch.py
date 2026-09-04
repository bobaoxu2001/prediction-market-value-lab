"""Family C: the venue round trip must finish before the first write.

Ingest used to await ``provider.get_trades`` *after* its first INSERT, so
SQLite's write path stayed open for the whole network fetch. Measured across 31
natural cycles that gave ingest a 169.9 s p90 pre-commit window, a 348.3 s
maximum, and 64% of all observed pre-commit write time.

Nothing in that fetch needs the transaction: selection reads the in-memory
market list and ``get_trades`` takes a NormalizedMarket. So the fetch moves
above the first DML and the payloads are stored later, inside the *same*
transaction. No new commit boundary - that is the whole point of the design,
and ``test_single_business_transaction_preserved`` is what holds the line.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pmvl_markets.ingest import runner

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "packages/market-normalization/src/pmvl_markets/ingest/runner.py"


class _Market:
    def __init__(self, mid: str, platform: Any, vol: str = "10") -> None:
        self.platform_market_id = mid
        self.platform = platform
        self.volume_24h = Decimal(vol)


class _Provider:
    """Records the exact requests made, and when, relative to DB writes."""

    def __init__(self, platform: Any, log: list[str], fail: set[str] | None = None) -> None:
        self.platform = platform
        self._log = log
        self._fail = fail or set()
        self.requested: list[str] = []
        self.closed = 0

    async def get_trades(self, market: Any, *, limit: int = 100) -> Any:
        self._log.append(f"fetch:{market.platform_market_id}")
        self.requested.append(market.platform_market_id)
        if market.platform_market_id in self._fail:
            raise RuntimeError("provider boom")
        return [f"trade-{market.platform_market_id}"]

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture()
def platform():
    from pmvl_shared.enums import Platform

    return Platform.KALSHI


def test_fetch_helper_exists_and_old_in_transaction_fetch_is_gone() -> None:
    assert hasattr(runner, "_fetch_trade_payloads")
    assert hasattr(runner, "_store_fetched_trades")
    assert not hasattr(runner, "_ingest_trades"), (
        "the in-transaction fetch must not survive alongside the prefetch"
    )


def test_prefetch_needs_no_session_and_no_id_map(platform) -> None:
    """The Family-C premise, asserted on the signature itself."""
    import inspect

    params = set(inspect.signature(runner._fetch_trade_payloads).parameters)
    assert "session" not in params
    assert "id_map" not in params


def test_prefetch_returns_raw_gather_results(platform) -> None:
    log: list[str] = []
    p = _Provider(platform, log)
    markets = [_Market("A", platform), _Market("B", platform)]
    out = asyncio.run(runner._fetch_trade_payloads([p], markets))
    assert out == [["trade-A"], ["trade-B"]]
    assert p.requested == ["A", "B"]


def test_selection_semantics_unchanged(platform) -> None:
    """Same filter, same ranking, same per_platform cap, same limit."""
    log: list[str] = []
    p = _Provider(platform, log)
    markets = [
        _Market("low", platform, "1"),
        _Market("high", platform, "99"),
        _Market("zero", platform, "0"),   # volume 0 -> excluded, as before
        _Market("mid", platform, "50"),
    ]
    asyncio.run(runner._fetch_trade_payloads([p], markets, per_platform=2))
    assert p.requested == ["high", "mid"], "candidate filter or ranking changed"


def test_exception_isolation_preserved(platform) -> None:
    """return_exceptions=True upstream, per-result skip downstream."""
    log: list[str] = []
    p = _Provider(platform, log, fail={"B"})
    markets = [_Market("A", platform), _Market("B", platform), _Market("C", platform)]
    out = asyncio.run(runner._fetch_trade_payloads([p], markets))
    assert len(out) == 3
    assert isinstance(out[1], Exception), "a failed request must not abort the batch"

    stored: list[Any] = []

    def fake_store(_session, result, _id_map):  # noqa: ANN001
        stored.append(result)
        return len(result)

    written = _store_with(fake_store, out)
    assert stored == [["trade-A"], ["trade-C"]], "exception was not skipped"
    assert written == 2


def _store_with(fake_store, fetched):  # noqa: ANN001
    import pmvl_markets.ingest.runner as r

    orig = r.store_trades
    r.store_trades = fake_store
    try:
        return r._store_fetched_trades(object(), fetched, {})
    finally:
        r.store_trades = orig


def test_store_uses_id_map_and_session(platform) -> None:
    import inspect

    params = list(inspect.signature(runner._store_fetched_trades).parameters)
    assert params[0] == "session"
    assert "id_map" in params


# ---- source-level structural invariants -------------------------------------

def _run_ingest_body() -> str:
    s = SRC.read_text()
    return s[s.index("async def run_ingest"):s.index("async def _fetch_trade_payloads")]


def test_network_fetch_precedes_first_dml() -> None:
    """The decisive ordering assertion. Structure, not a timing threshold."""
    body = _run_ingest_body()
    fetch = body.index("_fetch_trade_payloads")
    first_dml = body.index("upsert_events(session")
    assert fetch < first_dml, (
        "trade prefetch must complete before the first business DML"
    )


def test_no_business_dml_before_prefetch() -> None:
    body = _run_ingest_body()
    fetch = body.index("_fetch_trade_payloads")
    before = body[:fetch]
    for dml in ("upsert_events(session", "upsert_markets(session",
                "store_orderbooks(", "store_trades(", "session.flush", "session.commit"):
        assert dml not in before, f"business DML {dml!r} occurs before the prefetch"


def test_single_business_transaction_preserved() -> None:
    """No new durability boundary - Family C was authorised on this basis."""
    body = _run_ingest_body()
    assert "session.commit()" not in body, "an intermediate commit was introduced"
    assert body.count("session_scope") == 0, "a second write session was introduced"


def test_store_happens_after_id_map_exists() -> None:
    body = _run_ingest_body()
    assert body.index("id_map = upsert_markets") < body.index("_store_fetched_trades")


def test_provider_teardown_still_after_writes() -> None:
    body = _run_ingest_body()
    assert body.index("_store_fetched_trades") < body.index("p.aclose()")


def test_no_awaits_between_first_dml_and_commit() -> None:
    """Nothing may re-introduce a network wait into the write window."""
    body = _run_ingest_body()
    window = body[body.index("upsert_events(session"):]
    # the only remaining await is provider teardown, which follows the writes
    awaits = [ln.strip() for ln in window.splitlines() if "await " in ln]
    assert awaits == ["await asyncio.gather(*(p.aclose() for p in providers), return_exceptions=True)"], (
        f"unexpected await inside the write window: {awaits}"
    )
