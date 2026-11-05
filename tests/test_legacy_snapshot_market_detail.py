"""Frozen snapshots remain readable before additive match-verdict migrations."""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from pmvl_api.deps import DataMode
from pmvl_api.routers.markets import market_detail
from pmvl_shared.db.base import Base
from pmvl_shared.db.models import Market, MarketMatch
from pmvl_shared.timeutil import utcnow


def test_market_detail_reads_a_snapshot_without_new_match_verdict_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        a = Market(platform="kalshi", platform_market_id="legacy-a", title="A", provenance="fixture", created_at=utcnow())
        b = Market(platform="kalshi", platform_market_id="legacy-b", title="B", provenance="fixture", created_at=utcnow())
        db.add_all([a, b])
        db.flush()
        market_id, other_id = a.id, b.id
        db.add(MarketMatch(market_a_id=market_id, market_b_id=other_id, created_at=utcnow()))
        db.commit()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE market_matches DROP COLUMN equivalence_verdict"))
        connection.execute(text("ALTER TABLE market_matches DROP COLUMN equivalence_components"))
    with Session(engine) as db:
        payload = market_detail(market_id, db=db, mode=DataMode.LIVE)
        matches = payload["data"]["cross_platform_matches"]
        assert len(matches) == 1
        assert matches[0]["other_market_id"] == other_id
        assert "equivalence_verdict" not in matches[0]
    engine.dispose()
