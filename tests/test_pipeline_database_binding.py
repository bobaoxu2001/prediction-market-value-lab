"""The jobs must write to the database the candidate is built from.

Canary A on production ran a full pipeline: 7,666 markets ingested, all nine jobs
SUCCESS, candidate validated, `publication_eligible: true`. The candidate was
byte-identical in row counts to its parent across all 22 tables. It contained none
of the work.

`get_settings()` is `@lru_cache(maxsize=1)`, and alembic's `env.py` calls it. The
pipeline migrated before binding, so the cached Settings froze on the default
database path. Setting `DATABASE_URL` afterwards changed nothing: every job wrote
to `data/pmvl.db` while the candidate was built from the untouched operational
copy.

Nothing in the run reported a problem, because from each component's point of view
nothing was wrong. Had `publish=true` been used, it would have published a snapshot
identical to its parent forever, and the track record would have frozen silently
while every run reported success.

That is why these tests assert the *binding*, not just the outcome: an outcome
assertion on a healthy run passes even when the binding is wrong, because a
correct-looking candidate is exactly what the bug produces.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/shared/src"))

from pmvl_shared.config import get_settings  # noqa: E402

from run_automated_snapshot_pipeline import (  # noqa: E402
    PipelineError,
    _bind_database,
)


@pytest.fixture(autouse=True)
def _restore_settings():  # noqa: ANN202
    """Leave the process-wide binding exactly as we found it.

    These tests deliberately rebind a global. Both layers have to be restored -
    the settings cache AND the engine - or every later test in the session runs
    against a temporary database, which is the same bug this file is about,
    inflicted on the suite.
    """
    import os

    from pmvl_shared.db import reset_engine

    original = os.environ.get("DATABASE_URL")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        get_settings.cache_clear()
        reset_engine()


class TestBindingRedirectsSettings:
    def test_binding_changes_the_resolved_database(self, tmp_path, monkeypatch) -> None:  # noqa: ANN001
        """The exact failure: setting the environment variable alone is not enough
        once something has already read settings."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()
        before = get_settings().database_url

        target = tmp_path / "operational.db"
        _bind_database(target)

        after = get_settings().database_url
        assert after != before
        assert str(target) in after

    def test_binding_survives_settings_being_read_first(
        self, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        """Reproduces the ordering that caused the incident: alembic's env.py
        materialises the cache before the pipeline binds."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()
        get_settings()  # stands in for env.py

        target = tmp_path / "operational.db"
        _bind_database(target)

        assert str(target) in get_settings().database_url

    def test_a_bare_environment_variable_would_not_have_worked(
        self, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        """Pins the reason the fix needs a cache clear rather than an assignment.

        If this ever starts passing without the clear, the caching behaviour has
        changed and the fix can be simplified - but silently relying on that would
        be how the bug returns.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()
        get_settings()

        monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'other.db'}")
        assert "other.db" not in get_settings().database_url, (
            "settings are no longer cached; _bind_database's cache_clear may be "
            "redundant, but removing it needs a deliberate decision"
        )

    def test_binding_raises_if_it_did_not_take_effect(
        self, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        """Fail loudly rather than run against the wrong database.

        The incident's defining quality was silence: every component reported
        success. A run that cannot bind must stop, not proceed.
        """
        target = tmp_path / "operational.db"

        def uncooperative_clear() -> None:
            return None  # simulate the cache refusing to drop

        monkeypatch.setattr(get_settings, "cache_clear", uncooperative_clear)
        monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///somewhere/else.db")
        get_settings.__wrapped__  # noqa: B018 - ensure we are patching the real object

        with pytest.raises(PipelineError, match="settings bound to"):
            _bind_database(target)


class TestJobsWriteWhereTheCandidateIsBuiltFrom:
    def test_a_job_writing_after_binding_lands_in_the_operational_db(
        self, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        """The end-to-end property, without the cost of a real pipeline run.

        A write issued through the application's own session must appear in the
        file the candidate is built from. In the incident it appeared in a
        different file entirely.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()

        operational = tmp_path / "operational.db"
        sqlite3.connect(operational).executescript(
            "CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT);"
        )

        _bind_database(operational)

        from sqlalchemy import text

        from pmvl_shared.db import session_scope

        with session_scope() as session:
            session.execute(text("INSERT INTO probe (note) VALUES ('written by a job')"))

        rows = [
            r[0]
            for r in sqlite3.connect(f"file:{operational}?mode=ro", uri=True).execute(
                "SELECT note FROM probe"
            )
        ]
        assert rows == ["written by a job"], (
            "the write did not land in the operational database the candidate is "
            "built from"
        )


class TestOrderingIsEnforcedInSource:
    def test_the_pipeline_binds_before_it_migrates(self) -> None:
        """Pinned by reading the source, because the runtime symptom is invisible.

        A healthy-looking candidate is exactly what the wrong order produces, so
        an outcome-based test cannot catch a regression here.
        """
        import run_automated_snapshot_pipeline as pipeline

        source = Path(pipeline.__file__).read_text()
        body = source[source.index("        operational = initialise_operational_db(") :]
        body = body[: body.index("asyncio.run(run_jobs(")]

        bind_at = body.index("_bind_database(operational)")
        migrate_at = body.index("migrate(operational, outcome)")
        assert bind_at < migrate_at, (
            "migrate() runs before _bind_database(); alembic's env.py will "
            "materialise the settings cache on the wrong database"
        )


class TestAnEngineBuiltBeforeBindingIsReplaced:
    """The second cache layer, which the first version of this fix missed.

    Clearing the settings cache is not enough: the SQLAlchemy engine is a
    module-level global built once from whatever settings said at the time. A fix
    that corrected settings and left the engine alone passed its own unit
    assertions and still could not write to the operational database.
    """

    def test_a_stale_engine_does_not_survive_binding(self, tmp_path, monkeypatch) -> None:  # noqa: ANN001
        import sqlite3

        from pmvl_shared.db import get_engine

        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()

        # Build the engine first, then move settings underneath it. The engine
        # keeps its original URL - that staleness is the bug, and it is what makes
        # a settings-only fix insufficient.
        stale_url = str(get_engine().url)
        decoy = tmp_path / "decoy.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{decoy}")
        get_settings.cache_clear()
        assert str(get_engine().url) == stale_url, (
            "the engine is no longer cached; reset_engine may be redundant, but "
            "removing it needs a deliberate decision"
        )

        operational = tmp_path / "operational.db"
        sqlite3.connect(operational).executescript(
            "CREATE TABLE probe (id INTEGER PRIMARY KEY);"
        )
        evidence = _bind_database(operational)

        assert "operational.db" in str(get_engine().url)
        assert str(get_engine().url) != stale_url
        assert "operational.db" in evidence["engine_url"]

    def test_binding_reports_both_layers(self, tmp_path, monkeypatch) -> None:  # noqa: ANN001
        """The report must be able to show they agree, not just assert it."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()

        operational = tmp_path / "operational.db"
        evidence = _bind_database(operational)

        assert str(operational) in evidence["requested_operational_db"]
        assert str(operational) in evidence["settings_database_url"]
        assert str(operational) in evidence["engine_url"]


class TestTheDefaultDatabaseIsLeftAlone:
    """A run must not touch the repository's own working database.

    In the incident every job wrote to `data/pmvl.db`, silently building a local
    database nobody asked for while the candidate stayed empty. Binding correctly
    means that file is never opened.
    """

    def test_binding_points_away_from_the_repository_default(
        self, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()
        default_url = get_settings().database_url
        assert "data/pmvl.db" in default_url, "unexpected default; test needs updating"

        operational = tmp_path / "operational.db"
        evidence = _bind_database(operational)

        assert "data/pmvl.db" not in evidence["settings_database_url"]
        assert "data/pmvl.db" not in evidence["engine_url"]

    def test_a_write_after_binding_does_not_reach_the_default_file(
        self, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        """The observable form of the incident: the default file growing while the
        candidate stayed empty."""
        import sqlite3
        from pathlib import Path as _Path

        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()
        default_path = _Path(get_settings().database_url.split("///")[-1])
        before = default_path.stat().st_mtime if default_path.exists() else None

        operational = tmp_path / "operational.db"
        sqlite3.connect(operational).executescript(
            "CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT);"
        )
        _bind_database(operational)

        from sqlalchemy import text

        from pmvl_shared.db import session_scope

        with session_scope() as session:
            session.execute(text("INSERT INTO probe (note) VALUES ('job write')"))

        after = default_path.stat().st_mtime if default_path.exists() else None
        assert after == before, "the run modified the repository's default database"

        rows = [
            r[0]
            for r in sqlite3.connect(f"file:{operational}?mode=ro", uri=True).execute(
                "SELECT note FROM probe"
            )
        ]
        assert rows == ["job write"]
