"""SQLite behind the same interface as PostgreSQL.

SQLite stays supported for local development and the test suite while the
production database is PostgreSQL. `SqliteConnection` is `sqlite3.Connection`
plus the two things the PostgreSQL adapter also offers, so application code can
ask for them without knowing which engine it is talking to:

- `dialect`, for the few places that must differ.
- `serialized(key)`, mutual exclusion around a read-then-append.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def serialized_sqlite(conn: sqlite3.Connection, key: str) -> Iterator[None]:
    """Take the database's single write lock before reading, so a read and
    the write that depends on it are one indivisible step.

    SQLite's default transaction is deferred and holds nothing until the
    first write, which is how two requests once read the same audit-chain
    head and forked it. `BEGIN IMMEDIATE` takes the reserved lock up front.
    A caller already inside a transaction keeps it: SQLite cannot nest one,
    and committing on their behalf would publish their half-finished work.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


class SqliteConnection(sqlite3.Connection):
    dialect = "sqlite"

    def serialized(self, key: str):
        return serialized_sqlite(self, key)


class SqliteDatabase:
    """One SQLite file. Connections are opened per request, as they always were."""

    backend = "sqlite"

    @property
    def location(self) -> str:
        return str(self.path)

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._ready = False
        self._lock = threading.Lock()

    def connect(self) -> SqliteConnection:
        from app.backend.services.database import get_connection

        return get_connection(self.path)  # type: ignore[return-value]

    def exists(self) -> bool:
        return self.path.exists()

    def ensure_ready(self) -> None:
        """Bring a local database up to the current schema, once per process."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            from app.backend.services.database import initialize_schema

            conn = self.connect()
            try:
                initialize_schema(conn)
            finally:
                conn.close()
            self._ready = True

    def migrate(self) -> list[str]:
        self.ensure_ready()
        return []

    def close(self) -> None:
        return None
