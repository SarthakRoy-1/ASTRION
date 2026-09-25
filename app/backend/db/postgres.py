"""PostgreSQL behind the interface the repositories already use.

The repositories are plain SQL against a connection: `conn.execute(sql, params)`,
`with conn:` around every write, rows read by name. This module gives psycopg 3
exactly that shape, so moving engines is an adapter, not a rewrite:

- `?` placeholders become `%s` (outside string literals; a literal `%` is
  escaped), so no statement is spelled twice.
- Connections run in autocommit. `with conn:` opens a transaction and commits
  on exit, rolling back on an exception -- what `with sqlite3_connection:` did.
  Nested blocks become savepoints, so an inner failure that the caller catches
  does not poison the outer transaction the way it would on a raw connection.
- `serialized(key)` is the one place a caller may ask for mutual exclusion; it
  is `BEGIN IMMEDIATE` on SQLite and a transaction-scoped advisory lock here.
  Transaction-scoped on purpose: a *session* advisory lock does not survive a
  transaction-mode pooler (Supabase's port 6543), and this app must run on one.
- Prepared statements are off, for the same pooler.
- Rows are `Row`, which reads like `sqlite3.Row`.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from app.backend.db.rows import Row

logger = logging.getLogger("astrion.db")

try:
    import psycopg
    from psycopg.pq import TransactionStatus
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - PostgreSQL support is optional at import
    psycopg = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[assignment,misc]
    TransactionStatus = None  # type: ignore[assignment,misc]


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and url.strip().lower().startswith(("postgresql://", "postgres://"))


# --- placeholder translation ------------------------------------------------


@lru_cache(maxsize=4096)
def translate(sql: str) -> str:
    """Rewrite SQLite-style `?` placeholders as psycopg `%s`.

    Walks the statement once, tracking single-quoted string literals (where a
    `?` is data, not a parameter). Every literal `%` is doubled, because psycopg
    treats `%` as syntax whenever parameters are bound.
    """
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_string:
            out.append("%%" if ch == "%" else ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":  # escaped quote
                    out.append("'")
                    i += 1
                else:
                    in_string = False
        elif ch == "'":
            in_string = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _normalise_params(params: Sequence[Any] | None) -> tuple[Any, ...] | None:
    """SQLite stores `True` as 1; an INTEGER column here would refuse a bool."""
    if not params:
        return None
    return tuple(int(p) if isinstance(p, bool) else p for p in params)


def _row_factory(cursor) -> Callable[[Sequence[Any]], Row]:
    columns = [d.name for d in (cursor.description or ())]
    return lambda values: Row(columns, values)


# --- cursor and connection --------------------------------------------------


class PgCursor:
    """The slice of `sqlite3.Cursor` the repositories touch."""

    __slots__ = ("_cursor",)

    def __init__(self, cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int:
        raise NotImplementedError(
            "lastrowid does not exist on PostgreSQL; INSERT ... RETURNING id instead"
        )

    def fetchone(self) -> Row | None:
        if self._cursor.description is None:
            return None
        return self._cursor.fetchone()

    def fetchall(self) -> list[Row]:
        if self._cursor.description is None:
            return []
        return self._cursor.fetchall()

    def fetchmany(self, size: int = 1) -> list[Row]:
        if self._cursor.description is None:
            return []
        return self._cursor.fetchmany(size)

    def __iter__(self) -> Iterator[Row]:
        if self._cursor.description is None:
            return iter(())
        return iter(self._cursor)

    def close(self) -> None:
        self._cursor.close()


class PgConnection:
    """One pooled connection, presented as a `sqlite3.Connection` would be."""

    dialect = "postgres"

    def __init__(self, raw, release: Callable[[Any], None]) -> None:
        self._raw = raw
        self._release = release
        self._transactions: list[Any] = []
        self._closed = False

    # -- statements ----------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PgCursor:
        return PgCursor(self._raw.execute(translate(sql), _normalise_params(params)))

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> PgCursor:
        cursor = self._raw.cursor()
        cursor.executemany(translate(sql), [_normalise_params(p) for p in seq])
        return PgCursor(cursor)

    # -- transactions --------------------------------------------------------

    @property
    def in_transaction(self) -> bool:
        return self._raw.info.transaction_status != TransactionStatus.IDLE

    def __enter__(self) -> "PgConnection":
        transaction = self._raw.transaction()
        transaction.__enter__()
        self._transactions.append(transaction)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        transaction = self._transactions.pop()
        # `Transaction.__exit__` commits (or releases the savepoint) on success
        # and rolls back on an exception; it returns False so the exception
        # continues to propagate, as it does out of a sqlite3 connection block.
        return bool(transaction.__exit__(exc_type, exc, tb))

    def run_script(self, sql: str) -> None:
        """Execute raw SQL with no placeholder translation: DDL for migrations."""
        self._raw.execute(sql)

    def commit(self) -> None:
        # Autocommit means there is nothing outstanding outside a `with` block.
        # Kept so code written for sqlite3 keeps its shape.
        if self._transactions:
            raise RuntimeError("commit() inside a `with conn:` block; leave the block")

    def rollback(self) -> None:
        if self._transactions:
            raise RuntimeError("rollback() inside a `with conn:` block; raise instead")

    @contextmanager
    def serialized(self, key: str) -> Iterator[None]:
        """Run the block with everyone else asking for `key` waiting their turn.

        A transaction (or a savepoint, if the caller is already in one) that
        holds a transaction-scoped advisory lock: released on commit or
        rollback, never left behind by a crashed process. A failure inside the
        block rolls back to the savepoint only, so a caller's larger
        transaction is not aborted by it.
        """
        with self._raw.transaction():
            self._raw.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"astrion:{key}",)
            )
            yield

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Anything still open belongs to a request that ended early; the pool
        # would discard the connection, but say so rather than hide it.
        while self._transactions:
            self._transactions.pop().__exit__(RuntimeError, RuntimeError("closed"), None)
        self._release(self._raw)


# --- the database -----------------------------------------------------------


class PostgresDatabase:
    """A pool of connections to one PostgreSQL database (and, for tests, schema)."""

    backend = "postgres"

    @property
    def location(self) -> str:
        """Where this is, for logs: the URL with its password removed."""
        return strip_secret(self._url)

    def __init__(
        self,
        url: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        schema: str | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        if psycopg is None:
            raise RuntimeError(
                "DATABASE_URL names a PostgreSQL database but psycopg is not "
                "installed. Install psycopg[binary,pool]."
            )
        self._url = url
        self._min_size = min_size
        self._max_size = max_size
        self._schema = schema
        self._connect_timeout = connect_timeout
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()
        self._ready = False

    # The pool is opened on first use rather than at construction: an app built
    # without its lifespan running (as the test client does) must still work.
    def _pool_or_open(self):
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    kwargs: dict[str, Any] = {
                        "autocommit": True,
                        "row_factory": _row_factory,
                        # No server-side prepared statements: a transaction-mode
                        # pooler hands each statement to whichever backend is free.
                        "prepare_threshold": None,
                    }
                    if self._schema:
                        kwargs["options"] = f"-c search_path={self._schema}"
                    pool = ConnectionPool(
                        self._url,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        kwargs=kwargs,
                        timeout=self._connect_timeout,
                        open=False,
                        name="astrion",
                    )
                    pool.open(wait=True, timeout=self._connect_timeout)
                    self._pool = pool
        return self._pool

    def connect(self) -> PgConnection:
        pool = self._pool_or_open()
        raw = pool.getconn()
        return PgConnection(raw, pool.putconn)

    def ensure_ready(self) -> None:
        """Refuse to serve from a database that is behind the code.

        No DDL here: this is the request path. Migrations are applied by the
        deploy step (`python -m app.backend.db.migrate`), and a database that
        has not been migrated is reported, not silently repaired.
        """
        if self._ready:
            return
        from app.backend.db.migrations import check_current

        conn = self.connect()
        try:
            check_current(conn, "postgres")
        finally:
            conn.close()
        self._ready = True

    def migrate(self) -> list[str]:
        from app.backend.db.migrations import apply_migrations

        conn = self.connect()
        try:
            applied = apply_migrations(conn, "postgres")
        finally:
            conn.close()
        self._ready = True
        return applied

    def exists(self) -> bool:
        try:
            self.ensure_ready()
        except Exception:
            return False
        return True

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()


def strip_secret(url: str) -> str:
    """The URL with its password removed, for logs."""
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)
