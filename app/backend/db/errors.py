"""Database exceptions, spelled once for both engines.

Application code used to catch `sqlite3.IntegrityError` and friends. Those are
the wrong classes the moment the database is PostgreSQL, where a duplicate key
raises `psycopg.errors.UniqueViolation` instead -- and an `except` clause that
misses it turns a clean refusal ("that address is taken") into a 500.

Each name here is a *tuple* of the equivalent classes, so `except IntegrityError`
catches either engine's exception and nothing else changes at the call site.
`psycopg` is imported only if it is installed, so a SQLite-only environment
(local development, most of the tests) keeps working without it.
"""

from __future__ import annotations

import sqlite3

try:  # pragma: no cover - exercised by which environment runs the suite
    import psycopg
    import psycopg.errors as _pg_errors

    _PG_INTEGRITY: tuple[type[BaseException], ...] = (psycopg.IntegrityError,)
    # A missing table raises `UndefinedTable` (a ProgrammingError) on PostgreSQL
    # where SQLite raises OperationalError; the callers that guard against "the
    # schema is not there yet" mean both.
    _PG_OPERATIONAL: tuple[type[BaseException], ...] = (
        psycopg.OperationalError,
        _pg_errors.UndefinedTable,
        _pg_errors.UndefinedColumn,
    )
    _PG_ERROR: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover
    _PG_INTEGRITY = ()
    _PG_OPERATIONAL = ()
    _PG_ERROR = ()

#: A constraint was violated: unique, foreign key, not-null, check.
IntegrityError: tuple[type[BaseException], ...] = (
    sqlite3.IntegrityError,
    *_PG_INTEGRITY,
)

#: The database refused the statement for a reason that is about its state (a
#: table that does not exist yet, a lock that timed out), not its content.
OperationalError: tuple[type[BaseException], ...] = (
    sqlite3.OperationalError,
    *_PG_OPERATIONAL,
)

#: Anything the database driver raises.
DatabaseError: tuple[type[BaseException], ...] = (sqlite3.Error, *_PG_ERROR)


class SchemaNotReadyError(RuntimeError):
    """The database exists but has not been migrated to the version this code needs.

    Raised instead of guessed at: applying DDL from a request handler is exactly
    what the migration phase exists to avoid.
    """
