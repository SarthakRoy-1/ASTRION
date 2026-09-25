"""The database layer: one interface, PostgreSQL and SQLite behind it.

Application code asks `open_database(settings)` for a `Database`, calls
`.connect()` per request, and uses the connection exactly as it always used a
`sqlite3.Connection`. Which engine answers is `Settings.database_backend`.

PostgreSQL is the production engine. SQLite remains for local development and
the test suite, and is removed once nothing needs it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from app.backend.db.errors import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    SchemaNotReadyError,
)
from app.backend.db.postgres import PostgresDatabase, is_postgres_url
from app.backend.db.sqlite import SqliteDatabase

if TYPE_CHECKING:  # pragma: no cover
    from app.backend.core.config import Settings


class Database(Protocol):
    backend: str

    @property
    def location(self) -> str: ...

    def connect(self): ...
    def exists(self) -> bool: ...
    def ensure_ready(self) -> None: ...
    def migrate(self) -> list[str]: ...
    def close(self) -> None: ...


#: A seam, not a feature. When set, `open_database` calls it instead of choosing
#: an engine from the settings. The test suite uses it to run every existing
#: test, which builds its database from a file path, against PostgreSQL without
#: editing them. Production never sets it.
_database_factory: "Callable[[Settings], Database] | None" = None


def set_database_factory(factory: "Callable[[Settings], Database] | None") -> None:
    global _database_factory
    _database_factory = factory


def serialized(conn, key: str):
    """Mutual exclusion around a read-then-write, on whichever engine `conn` is.

    Accepts a plain `sqlite3.Connection` too (tests and scripts build those
    directly), which is treated as SQLite.
    """
    method = getattr(conn, "serialized", None)
    if method is not None:
        return method(key)
    from app.backend.db.sqlite import serialized_sqlite

    return serialized_sqlite(conn, key)


def open_database(settings: "Settings") -> Database:
    """The `Database` these settings describe. Cheap; connections are lazy."""
    if _database_factory is not None:
        return _database_factory(settings)
    if settings.database_backend == "postgres":
        return PostgresDatabase(
            settings.database_url or "",
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
        )
    return SqliteDatabase(settings.database_path)


__all__ = [
    "Database",
    "DatabaseError",
    "IntegrityError",
    "OperationalError",
    "PostgresDatabase",
    "SchemaNotReadyError",
    "SqliteDatabase",
    "is_postgres_url",
    "open_database",
    "serialized",
    "set_database_factory",
]
