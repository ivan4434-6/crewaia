"""Database engine and session helpers (SQLite by default, any SQLAlchemy URL)."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from creator_office.settings import get_settings


def new_id() -> str:
    return secrets.token_hex(8)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC and always returns timezone-aware datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        url = get_settings().resolved_database_url
        is_sqlite = url.startswith("sqlite")
        kwargs: dict[str, Any] = {"future": True}
        if is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kwargs)
        if is_sqlite:

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

        _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _factory is not None  # noqa: S101 - set by get_engine
    return _factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for worker threads and background code."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed on success."""
    with session_scope() as session:
        yield session


def init_db() -> None:
    from creator_office import models  # noqa: F401 - registers the tables

    Base.metadata.create_all(get_engine())


def reset_engine_for_tests() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None
