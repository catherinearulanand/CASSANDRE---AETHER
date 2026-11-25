"""Small dialect helpers for race-free create-if-absent (the check-then-insert pattern raised unique violations on
PostgreSQL; SQLite's serialised writer had masked it)."""
from __future__ import annotations


def insert_ignore(session, model, **values) -> int:
    """INSERT ... ON CONFLICT DO NOTHING (any unique key; PostgreSQL and SQLite); plain add elsewhere.
    Returns the number of rows actually inserted (0 if the row already existed)."""
    name = session.get_bind().dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - other dialects are not supported by this build
        session.add(model(**values))
        session.flush()
        return 1
    # RETURNING (not rowcount: unreliable for ORM-enabled inserts) tells us whether this statement really inserted a row
    pk = next(iter(model.__table__.primary_key.columns))
    return len(session.execute(insert(model).values(**values).on_conflict_do_nothing().returning(pk)).all())
