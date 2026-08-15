"""Engine, session factory and portable column types.

The project targets PostgreSQL in production and SQLite for zero-infrastructure local
runs. The two disagree about ``NUMERIC`` and ``JSONB``, so both are abstracted here.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator, Sequence

from sqlalchemy import JSON, Numeric, cast, create_engine, event, literal
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from ..config import get_settings


def insert_or_skip(
    session: Session,
    table,
    values: dict[str, object],
    *,
    conflict_cols: Sequence[str],
    conflict_where=None,
) -> bool:
    """INSERT with ON CONFLICT DO NOTHING; True when this writer inserted.

    The database-native arbiter for SELECT-then-INSERT upserts. Two writers can
    both miss the same SELECT and race to INSERT the same key; catching the
    resulting IntegrityError is NOT a safe repair, because SQLAlchemy marks the
    whole session transaction dead after a failed flush and the loser would lose
    its own batch progress on the required rollback. The upsert primitive never
    fails on the conflict: it does nothing and the rowcount says who won.

    The conflict target is explicit, so every OTHER constraint failure (NOT
    NULL, foreign key) still raises instead of being swallowed as "already
    exists". ``values`` must cover every NOT NULL column without a server
    default.
    """
    if session.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    stmt = dialect_insert(table).values(**values)
    kwargs: dict[str, object] = {"index_elements": list(conflict_cols)}
    if conflict_where is not None:
        kwargs["index_where"] = conflict_where
    # RETURNING, not rowcount: psycopg3 does not reliably report rows-affected
    # for INSERT ... ON CONFLICT, so rowcount-based winner detection silently
    # broke on PostgreSQL. A returned primary key means this writer inserted.
    stmt = stmt.on_conflict_do_nothing(**kwargs).returning(table.c.id)
    return session.execute(stmt).first() is not None


class Base(DeclarativeBase):
    pass


class Money(TypeDecorator):
    """A Decimal column that survives SQLite.

    SQLite has no native NUMERIC affinity, and SQLAlchemy would otherwise hand back
    ``float`` - which is exactly what this project forbids in financial paths. Storing
    the canonical string keeps every value exact on both backends.

    Bound: on SQLite the canonical string lives in a String(40) column. Binary
    contract prices are at most a few dollars at sub-cent granularity, so any
    value this codebase actually writes fits with huge headroom; magnitudes
    beyond ~1e33 (or exponents) would not round-trip and must not be stored in
    a Money column.
    """

    impl = Numeric(20, 8)
    cache_ok = True

    class comparator_factory(TypeDecorator.Comparator):
        """Make SQLite compare the stored decimal text as a number.

        SQLite's TEXT affinity otherwise makes ``9990`` sort ahead of
        ``3137050`` and makes a threshold such as ``>= 500`` lexicographic. The
        cast lives on the comparator rather than the stored representation so
        existing read-only snapshots remain compatible and Decimal round-trips
        stay exact. Applying the same NUMERIC cast on PostgreSQL is semantically
        neutral.
        """

        def _number(self):  # noqa: ANN201
            return cast(self.expr, Numeric(20, 8))

        @staticmethod
        def _other(value):  # noqa: ANN001, ANN205
            if isinstance(value, (Decimal, int, float, str)):
                # Bind as canonical text and let the database cast it. This
                # avoids sending a Decimal through SQLite's float-only numeric
                # binder at the exact comparison boundary.
                return cast(literal(str(value)), Numeric(20, 8))
            return value

        def __lt__(self, other):  # noqa: ANN001, ANN204
            return self._number() < self._other(other)

        def __le__(self, other):  # noqa: ANN001, ANN204
            return self._number() <= self._other(other)

        def __gt__(self, other):  # noqa: ANN001, ANN204
            return self._number() > self._other(other)

        def __ge__(self, other):  # noqa: ANN001, ANN204
            return self._number() >= self._other(other)

        def __eq__(self, other):  # noqa: ANN001, ANN204
            if other is None:
                return self.expr.is_(None)
            return self._number() == self._other(other)

        def __ne__(self, other):  # noqa: ANN001, ANN204
            if other is None:
                return self.expr.is_not(None)
            return self._number() != self._other(other)

        def asc(self):  # noqa: ANN201
            return self._number().asc()

        def desc(self):  # noqa: ANN201
            return self._number().desc()

    def load_dialect_impl(self, dialect):  # noqa: ANN001, ANN201
        if dialect.name == "sqlite":
            from sqlalchemy import String

            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(Numeric(20, 8))

    def process_bind_param(self, value: Any, dialect) -> Any:  # noqa: ANN001
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        return str(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value: Any, dialect) -> Decimal | None:  # noqa: ANN001
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class JSONColumn(TypeDecorator):
    """JSONB on PostgreSQL, JSON elsewhere. Used for raw provider payloads."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001, ANN201
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB

            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    url = url or settings.database_url
    read_only_sqlite = url.startswith("sqlite") and (
        "mode=ro" in url or "immutable=1" in url
    )
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # check_same_thread=False lets the FastAPI threadpool share the connection.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(pool_size=10, max_overflow=20)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            try:
                if read_only_sqlite:
                    # A verified production Snapshot is immutable.  Attempting to
                    # switch a decompressed /tmp copy to WAL would mutate the bytes
                    # we just verified and create -wal/-shm sidecars.  query_only
                    # makes an accidental write fail at SQLite's own boundary.
                    cur.execute("PRAGMA query_only=ON")
                else:
                    # WAL lets the API read while the worker writes - required
                    # because both run against the same file in local development.
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA synchronous=NORMAL")
            except Exception:  # noqa: BLE001
                # A read-only mount (serverless deployments ship the database inside
                # the bundle) rejects journal changes. Reads still work, so this must
                # not be fatal - the alternative is the whole API failing to start.
                pass
            try:
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
            except Exception:  # noqa: BLE001
                pass
            finally:
                cur.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine(url: str | None = None) -> Engine:
    """Rebind the global engine. Used by tests and by `pmvl-db` CLI targets."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = _build_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def create_all() -> None:
    from . import models  # noqa: F401  (import registers mappers)

    Base.metadata.create_all(bind=get_engine())
