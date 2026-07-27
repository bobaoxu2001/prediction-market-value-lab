"""Engine, session factory and portable column types.

The project targets PostgreSQL in production and SQLite for zero-infrastructure local
runs. The two disagree about ``NUMERIC`` and ``JSONB``, so both are abstracted here.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

from sqlalchemy import JSON, Numeric, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from ..config import get_settings


class Base(DeclarativeBase):
    pass


class Money(TypeDecorator):
    """A Decimal column that survives SQLite.

    SQLite has no native NUMERIC affinity, and SQLAlchemy would otherwise hand back
    ``float`` - which is exactly what this project forbids in financial paths. Storing
    the canonical string keeps every value exact on both backends.
    """

    impl = Numeric(20, 8)
    cache_ok = True

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
                # WAL lets the API read while the worker writes - required because
                # both run against the same file in the default local setup.
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
