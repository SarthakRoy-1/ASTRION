"""Helpers for tests that must look at a database as a whole, on either engine."""

from __future__ import annotations

import os
from pathlib import Path


def copy_full_db(template: Path, destination: Path) -> None:
    """A private copy of the ingested template, on whichever engine runs."""
    if os.environ.get("ASTRION_TEST_DATABASE_URL"):
        from tests import pg_backend

        pg_backend.clone_data(template, destination)
    else:
        import shutil

        shutil.copy(template, destination)


def db_bytes(path: Path) -> bytes:
    """Everything stored in the database `path` names, as one blob.

    SQLite: the file. PostgreSQL: every text-ish value of every table in the
    schema that path maps to (see tests/pg_backend.py). Used by the "this secret
    is nowhere in the database" assertions, which are only as strong as the
    thoroughness of what they search.
    """
    if not os.environ.get("ASTRION_TEST_DATABASE_URL"):
        return Path(path).read_bytes()

    import psycopg

    from tests import pg_backend

    schema = pg_backend.schema_for(path)
    chunks: list[str] = []
    with psycopg.connect(os.environ["ASTRION_TEST_DATABASE_URL"], autocommit=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            ).fetchall()
        ]
        for table in tables:
            for row in conn.execute(f'SELECT t::text FROM "{schema}"."{table}" AS t').fetchall():
                chunks.append(row[0])
    return "\n".join(chunks).encode("utf-8")
