"""Migration release checks that SQLite alone cannot enforce."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from pmvl_shared.snapshot_artifact import CURRENT_SNAPSHOT_SCHEMA_REVISION


ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_candidate_revision_tracks_the_actual_migration_head():
    config = Config()
    config.set_main_option("script_location", str(ROOT / "packages/shared/src/pmvl_shared/db/migrations"))
    assert ScriptDirectory.from_config(config).get_current_head() == CURRENT_SNAPSHOT_SCHEMA_REVISION


def test_the_explicitly_supported_shipped_parent_remains_readable(tmp_path):
    from pmvl_shared.snapshot_artifact import _sqlite_manifest_contract_problems
    from tests.test_migration_publication_deadlock import _snapshot_at

    parent = "8c41f0d7ab29"
    db = _snapshot_at(tmp_path / "parent.db", parent)
    assert not _sqlite_manifest_contract_problems(db, {"schema_revision": parent})


def test_climate_boolean_defaults_compile_for_postgres_and_sqlite(monkeypatch):
    import importlib.util
    from sqlalchemy import MetaData, Table

    path = ROOT / "packages/shared/src/pmvl_shared/db/migrations/versions/c7d2e94b1f30_daily_climate_observations.py"
    spec = importlib.util.spec_from_file_location("climate_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tables = []
    monkeypatch.setattr(module.op, "create_table", lambda name, *columns: tables.append(Table(name, MetaData(), *columns)))
    monkeypatch.setattr(module.op, "create_index", lambda *args, **kwargs: None)
    module.upgrade()
    pg = str(CreateTable(tables[0]).compile(dialect=postgresql.dialect()))
    sq = str(CreateTable(tables[0]).compile(dialect=sqlite.dialect()))
    assert "is_final BOOLEAN DEFAULT false" in pg
    assert "is_backfill BOOLEAN DEFAULT false" in pg
    assert "is_final BOOLEAN DEFAULT 0" in sq
