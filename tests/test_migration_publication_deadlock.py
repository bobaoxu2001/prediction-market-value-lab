"""A migration must not brick the publication pipeline.

The pipeline is a stateless recomputation: every run seeds its operational
database from the last published snapshot, migrates that copy to head, runs the
jobs, and publishes a fresh candidate.

Seeding verified the parent against the revision *this code* expects. So the
moment a migration landed, the published parent (revision N) was rejected by
code expecting N+1 - and the only component able to publish an artefact at N+1
was the pipeline that had just refused to start. Every migration deadlocked
publication until somebody hand-built a snapshot outside the pipeline.

Observed on 2026-08-26 while shipping ``8c41f0d7ab29``:

    PipelineError: the published snapshot failed verification and cannot seed a
    run: SQLite schema revision is unsupported: ['f1a2b3c4d5e6'];
    expected ['8c41f0d7ab29']

The fix is narrow on purpose. Only the seeding call opts in, because it migrates
the copy before anything reads it; every serving path still fails closed, since
a snapshot at the wrong revision genuinely cannot be served.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from pathlib import Path

import pytest

from pmvl_shared.snapshot_artifact import (
    CURRENT_SNAPSHOT_SCHEMA_REVISION,
    _sqlite_manifest_contract_problems,
)


def _snapshot_at(path: Path, revision: str) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE alembic_version (version_num TEXT)")
    con.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    con.execute(
        "CREATE TABLE job_runs (id INTEGER PRIMARY KEY, job_name TEXT, "
        "status TEXT, started_at TEXT)"
    )
    con.commit()
    con.close()
    return path


class TestRevisionGateDefaultsClosed:
    def test_a_stale_revision_is_rejected_by_default(self, tmp_path: Path) -> None:
        db = _snapshot_at(tmp_path / "s.db", "f1a2b3c4d5e6")
        problems = _sqlite_manifest_contract_problems(
            db, {"schema_revision": "f1a2b3c4d5e6"}
        )
        assert any("schema revision is unsupported" in p for p in problems)

    def test_the_current_revision_is_accepted(self, tmp_path: Path) -> None:
        db = _snapshot_at(tmp_path / "s.db", CURRENT_SNAPSHOT_SCHEMA_REVISION)
        problems = _sqlite_manifest_contract_problems(
            db, {"schema_revision": CURRENT_SNAPSHOT_SCHEMA_REVISION}
        )
        assert not any("schema revision is unsupported" in p for p in problems)


class TestParentRevisionOptIn:
    def test_a_parent_one_migration_behind_can_seed(self, tmp_path: Path) -> None:
        db = _snapshot_at(tmp_path / "s.db", "f1a2b3c4d5e6")
        problems = _sqlite_manifest_contract_problems(
            db,
            {"schema_revision": "f1a2b3c4d5e6"},
            accepted_revisions=(CURRENT_SNAPSHOT_SCHEMA_REVISION, "f1a2b3c4d5e6"),
        )
        assert not any("schema revision is unsupported" in p for p in problems)

    def test_the_manifest_must_still_agree_with_the_bytes(
        self, tmp_path: Path
    ) -> None:
        """Only the identity of the revision may lag; internal consistency may not.

        A manifest claiming one revision over bytes at another is a corrupt pair
        whatever the opt-in says.
        """
        db = _snapshot_at(tmp_path / "s.db", "f1a2b3c4d5e6")
        problems = _sqlite_manifest_contract_problems(
            db,
            {"schema_revision": "something-else"},
            accepted_revisions=(CURRENT_SNAPSHOT_SCHEMA_REVISION, "f1a2b3c4d5e6"),
        )
        assert any("does not match" in p for p in problems)

    def test_an_unknown_revision_is_still_rejected(self, tmp_path: Path) -> None:
        """The opt-in is the parent's declared revision, never a wildcard."""
        db = _snapshot_at(tmp_path / "s.db", "not-a-real-revision")
        problems = _sqlite_manifest_contract_problems(
            db,
            {"schema_revision": "not-a-real-revision"},
            accepted_revisions=(CURRENT_SNAPSHOT_SCHEMA_REVISION, "f1a2b3c4d5e6"),
        )
        assert any("schema revision is unsupported" in p for p in problems)


class TestOnlySeedingOptsIn:
    def test_the_resolver_defaults_to_refusing_a_parent_revision(self) -> None:
        from pmvl_shared.snapshot_artifact import resolve_snapshot_path

        default = inspect.signature(resolve_snapshot_path).parameters[
            "accept_parent_revision"
        ].default
        assert default is False

    def test_the_pipeline_seed_step_is_the_only_caller_that_opts_in(self) -> None:
        """A serving path that opted in would serve an unservable snapshot."""
        opted_in: list[str] = []
        for path in Path(".").rglob("*.py"):
            parts = set(path.parts)
            if ".venv" in parts or "tests" in parts:
                continue
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "id", None) != "resolve_snapshot_path":
                    continue
                if any(
                    k.arg == "accept_parent_revision"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value is True
                    for k in node.keywords
                ):
                    opted_in.append(str(path))
        assert opted_in == ["scripts/run_automated_snapshot_pipeline.py"], opted_in

    def test_seeding_migrates_the_copy_before_any_job_reads_it(self) -> None:
        """The opt-in is only safe because of this ordering."""
        source = Path("scripts/run_automated_snapshot_pipeline.py").read_text()
        tree = ast.parse(source)
        main = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        body = ast.unparse(main)
        assert body.index("initialise_operational_db") < body.index("migrate(")


@pytest.mark.parametrize("revision", ["", None])
def test_a_parent_with_no_declared_revision_gets_no_leniency(revision) -> None:  # noqa: ANN001
    """An absent declaration must not widen the accepted set."""
    from pmvl_shared.snapshot_artifact import resolve_snapshot_path

    source = inspect.getsource(resolve_snapshot_path)
    assert "isinstance(declared, str) and declared" in source
