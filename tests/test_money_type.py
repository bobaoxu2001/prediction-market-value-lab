"""Portable Money columns must compare numerically without losing Decimal values."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from pmvl_shared.db.base import Money


class _Base(DeclarativeBase):
    pass


class _Amount(_Base):
    __tablename__ = "money_comparator_test"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[Decimal] = mapped_column(Money(), nullable=False)


VALUES = [
    Decimal("-9.5"),
    Decimal("0.01"),
    Decimal("9"),
    Decimal("99"),
    Decimal("999"),
    Decimal("9999"),
    Decimal("99999"),
    Decimal("3137050"),
]


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(_Amount(value=value) for value in VALUES)
    session.commit()
    return session


def test_sqlite_money_orders_numerically_and_round_trips_decimal() -> None:
    with _session() as session:
        descending = list(session.scalars(select(_Amount.value).order_by(_Amount.value.desc())))

    assert descending == sorted(VALUES, reverse=True)
    assert all(isinstance(value, Decimal) for value in descending)


def test_sqlite_money_thresholds_are_numeric_not_lexicographic() -> None:
    with _session() as session:
        values = list(
            session.scalars(
                select(_Amount.value)
                .where(_Amount.value >= Decimal("500"))
                .order_by(_Amount.value.asc())
            )
        )

    assert values == [Decimal("999"), Decimal("9999"), Decimal("99999"), Decimal("3137050")]


def test_sqlite_money_equality_ignores_decimal_text_scale() -> None:
    with _session() as session:
        session.add(_Amount(value=Decimal("100.00")))
        session.commit()
        matches = list(
            session.scalars(select(_Amount.value).where(_Amount.value == Decimal("100")))
        )

    assert matches == [Decimal("100.00")]
