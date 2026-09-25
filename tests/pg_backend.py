"""Run the existing suite against PostgreSQL.

The suite was written against SQLite: every test names its database as a file
path (`full_db`, `tmp_path / "x.db"`) and hands that path to `get_connection`
or to `Settings(database_path=...)`. Rewriting a thousand tests to name a
different kind of thing would be a large, risky edit for no gain; this module
instead makes the *path* an identity. When `ASTRION_TEST_DATABASE_URL` is set:

- each path maps to its own PostgreSQL **schema** (`t_<hash of the path>`),
  created and migrated on first use, so tests stay as isolated as separate
  files were;
- `get_connection(path)` and `open_database(settings)` are routed to that
  schema through the two seams the production code exposes for this.

Unset, none of this is imported and the suite runs on SQLite exactly as before.

Point it at a throwaway database -- it creates and drops schemas named `t_*`:

    docker run -d --name astrion-pg-test -e POSTGRES_USER=astrion \\
        -e POSTGRES_PASSWORD=astrion_test_pw -e POSTGRES_DB=astrion_test \\
        -p 55432:5432 postgres:17
    ASTRION_TEST_DATABASE_URL=postgresql://astrion:astrion_test_pw@localhost:55432/astrion_test \\
        pytest
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path

import psycopg

from app.backend.db import PostgresDatabase, set_database_factory
from app.backend.db.migrations import apply_migrations
from app.backend.services import database as sqlite_layer

URL = os.environ["ASTRION_TEST_DATABASE_URL"]

#: Tables the ingestion scripts fill, in foreign-key order. `full_db` clones
#: these from a template schema instead of re-running ingestion per test.
DATA_TABLES = (
    "organizations",
    "dataset_metadata",
    "ingestion_runs",
    "accounts",
    "orders",
    "tickets",
    "source_provenance",
    "document_ingestion_runs",
    "documents",
    "document_chunks",
)
_IDENTITY_TABLES = ("ingestion_runs", "source_provenance", "document_ingestion_runs")

#: Open pools are capped, so a thousand tests do not hold a thousand connections.
MAX_OPEN_POOLS = 12

_lock = threading.RLock()
_databases: "OrderedDict[str, PostgresDatabase]" = OrderedDict()
_created: set[str] = set()


#: Schemas belong to one test process, so two suites can share a database
#: without dropping each other's tables.
PREFIX = f"t{os.getpid()}_"


def schema_for(path: Path | str) -> str:
    digest = hashlib.sha1(str(Path(path)).encode("utf-8")).hexdigest()[:16]
    return f"{PREFIX}{digest}"


def _admin():
    return psycopg.connect(URL, autocommit=True)


def _ensure_schema(schema: str) -> PostgresDatabase:
    with _lock:
        database = _databases.get(schema)
        if database is None:
            if schema not in _created:
                with _admin() as admin:
                    admin.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                _created.add(schema)
            database = PostgresDatabase(URL, min_size=1, max_size=16, schema=schema)
            if schema not in _migrated:
                conn = database.connect()
                try:
                    apply_migrations(conn, "postgres")
                finally:
                    conn.close()
                _migrated.add(schema)
            database._ready = True
            _databases[schema] = database
            while len(_databases) > MAX_OPEN_POOLS:
                _, oldest = _databases.popitem(last=False)
                oldest.close()
        else:
            _databases.move_to_end(schema)
        return database


_migrated: set[str] = set()


def database_for(path: Path | str) -> PostgresDatabase:
    return _ensure_schema(schema_for(path))


def _connection_factory(path: Path):
    return database_for(path).connect()


def _database_factory(settings):
    return database_for(settings.database_path)


def drop_schema(path: Path | str) -> None:
    schema = schema_for(path)
    with _lock:
        database = _databases.pop(schema, None)
        if database is not None:
            database.close()
        _created.discard(schema)
        _migrated.discard(schema)
    with _admin() as admin:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def clone_data(src: Path | str, dst: Path | str) -> None:
    """Give `dst` a copy of the ingested data in `src`, as `shutil.copy` did."""
    source, target = schema_for(src), schema_for(dst)
    _ensure_schema(target)
    with _admin() as admin:
        for table in DATA_TABLES:
            admin.execute(
                f'INSERT INTO "{target}"."{table}" SELECT * FROM "{source}"."{table}"'
            )
        for table in _IDENTITY_TABLES:
            admin.execute(
                f"SELECT setval(pg_get_serial_sequence('\"{target}\".\"{table}\"', 'id'), "
                f'(SELECT COALESCE(MAX(id), 0) + 1 FROM "{target}"."{table}"), false)'
            )


def drop_all() -> None:
    with _lock:
        for database in _databases.values():
            database.close()
        _databases.clear()
    with _admin() as admin:
        rows = admin.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE %s",
            (PREFIX.replace("_", "\\_") + "%",),
        ).fetchall()
        for (name,) in rows:
            admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    _created.clear()
    _migrated.clear()


def install() -> None:
    sqlite_layer.set_connection_factory(_connection_factory)
    set_database_factory(_database_factory)
